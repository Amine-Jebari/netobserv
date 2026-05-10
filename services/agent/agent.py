"""Investigation agent — LangChain + Google Gemini.

The agent is the keystone of the platform: given a scored anomaly event, it
forms hypotheses, calls tools to verify them, cites runbook chunks, and emits
a structured verdict the dashboard can render.

This module contains:

* :data:`SYSTEM_PROMPT` - the persona and protocol the LLM must follow.
* :func:`build_chat_model` - constructs the ``ChatGoogleGenerativeAI`` with the
  4 tools bound for function-calling.
* :class:`InvestigationResult` - the typed return shape.
* :func:`run_investigation` - the bounded tool-call loop with timeout, cost
  accounting, and structured trace capture.

The same module is callable in two ways:

* :func:`run_investigation_for_anomaly` is the high-level entry point used by
  the Kafka consumer and the ``POST /investigate`` HTTP endpoint. It
  constructs a fresh trace, persists ``status='running'`` row, runs the loop,
  and persists the final verdict.
* :func:`run_investigation` is the lower-level engine used by tests. It
  accepts a pre-built chat model + toolbox so tests can swap in a mock LLM.

Per project mandate, the LLM provider is **Google Gemini** (``gemini-2.0-flash``
default) via ``langchain-google-genai``. The metric name ``claude_tokens_used_total``
is preserved for dashboard compatibility regardless of provider.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_google_genai import ChatGoogleGenerativeAI

from .config import Settings, get_settings
from .investigations_store import insert_started, update_completed
from .logging_setup import get_logger
from .metrics import AgentMetrics
from .tools import Toolbox, build_tools, invoke_tool

log = get_logger(__name__)


# ── System prompt ────────────────────────────────────────────────────────────


SYSTEM_PROMPT = """\
You are a senior network engineer specializing in cloud network operations
(BGP, VPC peering, hybrid connectivity, DDoS mitigation). You are paged when
the anomaly detector flags a WARN or CRITICAL event. Your job is to produce a
short, evidence-backed investigation report and a remediation runbook.

Available tools (call them with structured JSON arguments):

  • query_topology(node_id?, prefix?)
      - Resolve the network neighborhood of a node OR the subgraph affected
        by a BGP prefix. Use it once early to understand blast radius.

  • get_metrics(target, metric, lookback_minutes=30)
      - Pull a recent time-series from InfluxDB. ``target`` is an IP, device
        id, or interface; ``metric`` is one of bytes, packets, utilization,
        latency_ms, or any field present in your environment. Use it to
        confirm or refute a hypothesis with numerical evidence.

  • search_runbooks(query, k=3)
      - Semantic search over the curated runbook corpus. Use it to look up
        diagnostic steps for the anomaly type, then reuse the wording in
        your final remediation plan. EVERY runbook claim you make MUST cite
        a returned ``citation`` value in the format ``filename.md#Section``.

  • get_recent_events(prefix?, node_id?, lookback_minutes=30)
      - Recent BGP and anomaly events. Use it to build a timeline (e.g. did
        a flap precede this traffic spike?).

Investigation protocol — follow it strictly:

  1. UNDERSTAND. Read the anomaly event. State its type (traffic / bgp /
     snmp), severity, top features, and the target node/prefix in one
     sentence.

  2. HYPOTHESIZE. Propose 1-3 candidate root causes ordered by likelihood.

  3. VERIFY. Call tools to test each hypothesis. Prefer the cheapest tool
     that disambiguates: search_runbooks first for known patterns, then
     get_recent_events for timeline, then get_metrics for numerical
     confirmation, then query_topology for blast-radius framing.

  4. CONCLUDE. Pick the most likely root cause and assign confidence
     (low / medium / high).

Hard rules:

  • You have a budget of at most 8 tool calls. Stop early if you have enough.
  • Every claim about the network or remediation MUST be backed by either
    a tool result or a runbook citation. No hallucinations.
  • If a tool errors, do not retry the same call with the same args — try a
    different tool or a different argument.
  • Never invent IPs, device IDs, prefixes, or metric values.

Output format — your FINAL message MUST be a single JSON object enclosed in
a ```json ... ``` fenced code block, with EXACTLY these keys:

```json
{
  "summary": "one-sentence operator-friendly summary of what happened",
  "hypothesis": "the candidate root cause(s) you considered, briefly",
  "root_cause": "the most likely root cause in plain language",
  "confidence": "low" | "medium" | "high",
  "blast_radius": "which nodes/prefixes/services are affected",
  "evidence": [
    {"source": "tool:get_metrics", "finding": "..."},
    {"source": "runbook:bgp_flap_troubleshooting.md#Common Root Causes",
     "finding": "..."}
  ],
  "citations": ["bgp_flap_troubleshooting.md#Common Root Causes", "..."],
  "remediation_steps": [
    "1. ...",
    "2. ..."
  ]
}
```

Do not include any prose outside the fenced JSON block in the final message.
Earlier reasoning steps may be free text; only the LAST message is parsed.
"""


# ── Result dataclass ─────────────────────────────────────────────────────────


@dataclass
class TraceStep:
    """One entry in the agent's reasoning trace."""

    step: int
    kind: str  # "thought" | "tool_call" | "tool_result" | "final"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    name: str | None = None
    args: dict[str, Any] | None = None
    content: str | None = None
    result: Any | None = None
    duration_ms: float | None = None
    error: str | None = None


@dataclass
class InvestigationResult:
    """Outcome of a single agent run."""

    status: str  # "completed" | "timed_out" | "error"
    verdict: dict[str, Any] | None
    trace: list[dict[str, Any]]
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    raw_final_text: str | None = None
    error: str | None = None


# ── Chat model factory ───────────────────────────────────────────────────────


def build_chat_model(settings: Settings | None = None) -> ChatGoogleGenerativeAI:
    """Construct a Gemini chat model from Settings.

    The agent expects the caller to bind tools via ``.bind_tools(...)`` after
    construction. We don't do that here so tests can build the bare model.
    """
    settings = settings or get_settings()
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=settings.agent_temperature,
        max_output_tokens=settings.agent_max_tokens,
        google_api_key=settings.google_api_key or None,
    )


# ── Anomaly → human prompt ───────────────────────────────────────────────────


def render_anomaly_prompt(anomaly: dict[str, Any]) -> str:
    """Render the scored-anomaly event as a structured prompt block."""
    keep = (
        "anomaly_id",
        "anomaly_type",
        "severity",
        "score",
        "timestamp",
        "affected_nodes",
        "affected_prefixes",
        "top_features",
        "feature_vector",
        "context_window_start",
        "context_window_end",
    )
    summary = {k: anomaly.get(k) for k in keep if k in anomaly}
    return (
        "A new anomaly has been detected. Investigate it now.\n\n"
        "Anomaly event:\n"
        "```json\n"
        f"{json.dumps(summary, indent=2, default=str)}\n"
        "```\n\n"
        "Begin with step 1 (UNDERSTAND), then proceed through the protocol. "
        "End your final message with the JSON verdict block."
    )


# ── Verdict parsing ──────────────────────────────────────────────────────────


_FENCED_JSON = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON = re.compile(r"(\{[^{}]*\"root_cause\"[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", re.DOTALL)


def parse_verdict(text: str) -> dict[str, Any] | None:
    """Extract the JSON verdict from the agent's final message.

    Tries a fenced ``json ...`` block first, falls back to the largest
    ``{...}`` substring containing ``root_cause``. Returns ``None`` if no
    parseable JSON is found.
    """
    if not text:
        return None
    match = _FENCED_JSON.search(text)
    candidate = match.group(1) if match else None
    if candidate is None:
        bare = _BARE_JSON.search(text)
        candidate = bare.group(1) if bare else None
    if candidate is None:
        # Last-ditch: assume the whole text is JSON.
        candidate = text.strip()
        if not candidate.startswith("{"):
            return None
    try:
        data = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ── Token usage extraction ───────────────────────────────────────────────────


def _extract_usage(message: AIMessage) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` from a Gemini AIMessage.

    The langchain-google-genai integration normalizes usage onto
    ``message.usage_metadata`` (input_tokens / output_tokens / total_tokens)
    on recent versions. Older versions stash it on ``response_metadata``.
    """
    usage = getattr(message, "usage_metadata", None) or {}
    if usage:
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    meta = getattr(message, "response_metadata", None) or {}
    raw = meta.get("usage_metadata") or meta.get("token_usage") or {}
    in_tok = int(
        raw.get("prompt_token_count")
        or raw.get("input_tokens")
        or raw.get("prompt_tokens")
        or 0
    )
    out_tok = int(
        raw.get("candidates_token_count")
        or raw.get("output_tokens")
        or raw.get("completion_tokens")
        or 0
    )
    return in_tok, out_tok


# ── Tool-call shape normalization ────────────────────────────────────────────


def _ai_tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    """Return a list of ``{id, name, args}`` from an AIMessage.

    LangChain's normalized form is ``message.tool_calls`` (list of dicts with
    ``name`` / ``args`` / ``id``). Some Gemini-flavored payloads only carry
    them in ``additional_kwargs['function_call']`` — we cover both.
    """
    calls = list(getattr(message, "tool_calls", None) or [])
    if calls:
        return [
            {
                "id": c.get("id") or f"call_{i}",
                "name": c.get("name") or "",
                "args": c.get("args") or {},
            }
            for i, c in enumerate(calls)
        ]
    extra = getattr(message, "additional_kwargs", None) or {}
    fc = extra.get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        args = fc.get("arguments") or fc.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, json.JSONDecodeError):
                args = {}
        return [{"id": "call_0", "name": fc["name"], "args": args}]
    return []


# ── Core agent loop ──────────────────────────────────────────────────────────


def run_investigation(
    *,
    anomaly: dict[str, Any],
    chat_model: Any,
    toolbox: Toolbox,
    metrics: AgentMetrics | None = None,
    max_tool_calls: int = 8,
    timeout_seconds: float = 120.0,
    model_label: str | None = None,
) -> InvestigationResult:
    """Execute one investigation against ``anomaly`` and return its result.

    Parameters
    ----------
    anomaly:
        The Kafka event from ``anomalies.scored``.
    chat_model:
        A LangChain chat model — typically ``ChatGoogleGenerativeAI`` with
        tools already bound via ``.bind_tools(build_tools(toolbox))``. Tests
        pass a mock that mimics ``invoke([messages]) -> AIMessage``.
    toolbox:
        Live Toolbox with HTTP client + runbook searcher.
    metrics:
        Optional :class:`AgentMetrics`; when supplied, tool-call and token
        counters are incremented.
    max_tool_calls:
        Hard cap on tool invocations. Once exceeded, the agent is asked
        once more for its final answer, then we exit.
    timeout_seconds:
        Wall-clock cap on the total investigation.
    model_label:
        Free-form string passed to the metrics' ``model`` label.
    """

    started = time.perf_counter()
    deadline = started + max(timeout_seconds, 1.0)

    messages: list[BaseMessage] = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=render_anomaly_prompt(anomaly)),
    ]
    trace: list[TraceStep] = []
    tool_calls = 0
    in_tokens = 0
    out_tokens = 0
    raw_final: str | None = None
    verdict: dict[str, Any] | None = None
    status = "completed"
    error: str | None = None

    label = model_label or getattr(chat_model, "model", "gemini-2.0-flash")

    try:
        while True:
            if time.perf_counter() > deadline:
                status = "timed_out"
                error = f"investigation exceeded {timeout_seconds:.0f}s budget"
                break

            try:
                ai_msg: AIMessage = chat_model.invoke(messages)
            except Exception as exc:
                status = "error"
                error = f"chat model invocation failed: {exc}"
                log.error("agent.invoke_failed", error=str(exc))
                break

            in_inc, out_inc = _extract_usage(ai_msg)
            in_tokens += in_inc
            out_tokens += out_inc
            if metrics is not None and (in_inc or out_inc):
                metrics.tokens_used.labels(type="input", model=label).inc(in_inc)
                metrics.tokens_used.labels(type="output", model=label).inc(out_inc)
                metrics.tokens_used.labels(type="total", model=label).inc(in_inc + out_inc)

            requested = _ai_tool_calls(ai_msg)
            text_content = _message_text(ai_msg)

            if text_content:
                trace.append(
                    TraceStep(
                        step=len(trace) + 1,
                        kind="thought",
                        content=text_content[:4000],
                    )
                )

            messages.append(ai_msg)

            if not requested:
                # Final answer: parse verdict from the text.
                raw_final = text_content
                verdict = parse_verdict(text_content)
                trace.append(
                    TraceStep(
                        step=len(trace) + 1,
                        kind="final",
                        content=text_content[:4000] if text_content else None,
                        result=verdict,
                    )
                )
                if verdict is None:
                    status = "error"
                    error = "agent did not return parseable JSON verdict"
                break

            # Run every requested tool.
            for call in requested:
                if tool_calls >= max_tool_calls:
                    log.warning(
                        "agent.tool_budget_exceeded", limit=max_tool_calls
                    )
                    # Inject a system note prompting the agent to wrap up.
                    messages.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "error": "tool budget exhausted",
                                    "remaining_calls": 0,
                                    "instruction": (
                                        "Stop calling tools. Emit your final "
                                        "JSON verdict in the next message."
                                    ),
                                }
                            ),
                            tool_call_id=call.get("id") or "budget",
                        )
                    )
                    continue

                tool_calls += 1
                t0 = time.perf_counter()
                trace.append(
                    TraceStep(
                        step=len(trace) + 1,
                        kind="tool_call",
                        name=call["name"],
                        args=call.get("args") or {},
                    )
                )
                try:
                    result = invoke_tool(toolbox, call["name"], call.get("args"))
                except Exception as exc:
                    result = {"error": f"tool raised {type(exc).__name__}: {exc}"}
                duration_ms = (time.perf_counter() - t0) * 1000.0

                outcome = "ok" if "error" not in result else "error"
                if metrics is not None:
                    metrics.tool_calls.labels(tool=call["name"], outcome=outcome).inc()

                trace.append(
                    TraceStep(
                        step=len(trace) + 1,
                        kind="tool_result",
                        name=call["name"],
                        result=_truncate_for_trace(result),
                        duration_ms=round(duration_ms, 2),
                    )
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, default=str)[:8000],
                        tool_call_id=call.get("id") or call["name"],
                    )
                )
            # Continue the loop for the next LLM step.
    finally:
        elapsed = time.perf_counter() - started
        log.info(
            "agent.investigation_complete",
            status=status,
            tool_calls=tool_calls,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            duration_seconds=round(elapsed, 3),
        )

    return InvestigationResult(
        status=status,
        verdict=verdict,
        trace=[_step_to_dict(s) for s in trace],
        tool_call_count=tool_calls,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        duration_seconds=round(time.perf_counter() - started, 3),
        raw_final_text=raw_final,
        error=error,
    )


# ── High-level entry point: run + persist ────────────────────────────────────


def run_investigation_for_anomaly(
    *,
    anomaly: dict[str, Any],
    settings: Settings,
    toolbox: Toolbox,
    chat_model: Any,
    metrics: AgentMetrics,
    runbook_renderer,  # callable: (anomaly, verdict, trace) -> str | None
) -> tuple[str, InvestigationResult]:
    """Run an investigation end-to-end and persist its outcome to Postgres.

    Returns ``(investigation_id, InvestigationResult)``. The id is the UUID
    primary key on the ``investigations`` row so callers (e.g. the FastAPI
    endpoint) can immediately surface it to the user.
    """
    anomaly_id = str(anomaly.get("anomaly_id") or "unknown")
    anomaly_type = str(anomaly.get("anomaly_type") or "unknown")
    severity = str(anomaly.get("severity") or "UNKNOWN").upper()

    investigation_id = insert_started(
        anomaly_id=anomaly_id,
        anomaly_type=anomaly_type,
        severity=severity,
        model=settings.gemini_model,
        anomaly_event=anomaly,
        settings=settings,
    )
    metrics.investigations_started.labels(
        severity=severity, anomaly_type=anomaly_type
    ).inc()

    result = run_investigation(
        anomaly=anomaly,
        chat_model=chat_model,
        toolbox=toolbox,
        metrics=metrics,
        max_tool_calls=settings.max_tool_calls,
        timeout_seconds=settings.investigation_timeout_seconds,
        model_label=settings.gemini_model,
    )

    runbook_md: str | None = None
    if result.verdict is not None:
        try:
            runbook_md = runbook_renderer(anomaly, result.verdict, result.trace)
        except Exception as exc:
            log.warning("agent.runbook_render_failed", error=str(exc))
            runbook_md = None

    update_completed(
        investigation_id=investigation_id,
        status=result.status,
        reasoning_trace=result.trace,
        verdict=result.verdict,
        runbook_markdown=runbook_md,
        tool_call_count=result.tool_call_count,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        duration_seconds=result.duration_seconds,
        error=result.error,
        settings=settings,
    )
    metrics.investigations_completed.labels(
        outcome=result.status if result.status in ("completed", "timed_out") else "error"
    ).inc()
    metrics.investigation_seconds.observe(result.duration_seconds)

    return investigation_id, result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _message_text(msg: BaseMessage) -> str:
    """Best-effort extraction of plain text from a LangChain message."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                t = chunk.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _truncate_for_trace(value: Any, max_chars: int = 4000) -> Any:
    """Cap a tool result before storing it in the trace JSON column."""
    encoded = json.dumps(value, default=str)
    if len(encoded) <= max_chars:
        return value
    return {
        "_truncated": True,
        "preview": encoded[:max_chars],
        "original_size_bytes": len(encoded),
    }


def _step_to_dict(s: TraceStep) -> dict[str, Any]:
    return {
        "step": s.step,
        "kind": s.kind,
        "timestamp": s.timestamp,
        **({"name": s.name} if s.name else {}),
        **({"args": s.args} if s.args is not None else {}),
        **({"content": s.content} if s.content else {}),
        **({"result": s.result} if s.result is not None else {}),
        **({"duration_ms": s.duration_ms} if s.duration_ms is not None else {}),
        **({"error": s.error} if s.error else {}),
    }
