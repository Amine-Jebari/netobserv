"""Runbook synthesis (Phase 4.3 stub used by Phase 4.2).

Once the agent has produced a structured verdict, this module renders a
deterministic markdown runbook. We do this with a template (rather than a
second LLM call) so:

  • the output is reproducible — the same verdict always produces the same
    runbook, which makes review and regression testing tractable, and
  • we don't double the per-investigation token cost.

If a future phase wants an LLM-rewritten ``polish`` pass, it can wrap this
output. For now the structured markdown is what gets stored in
``investigations.runbook_markdown`` and surfaced on the dashboard.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable


def render_runbook(
    anomaly: dict[str, Any],
    verdict: dict[str, Any] | None,
    trace: Iterable[dict[str, Any]] | None = None,
) -> str | None:
    """Render a markdown runbook from the agent's verdict.

    Returns ``None`` if ``verdict`` is missing or unusable; the caller stores
    that value as-is so the API can render a placeholder.
    """
    if not isinstance(verdict, dict):
        return None

    title = _title(anomaly, verdict)
    sections: list[str] = [
        f"# {title}",
        "",
        _front_matter(anomaly, verdict),
        "",
        "## Summary",
        verdict.get("summary") or "_No summary provided._",
        "",
        "## Root cause",
        _root_cause_block(verdict),
        "",
        "## Blast radius",
        verdict.get("blast_radius") or "_Not specified._",
        "",
        "## Evidence",
        _evidence_block(verdict),
        "",
        "## Remediation steps",
        _steps_block(verdict),
        "",
        "## Citations",
        _citations_block(verdict),
        "",
        "## Investigation metadata",
        _trace_block(anomaly, trace),
    ]
    return "\n".join(sections).rstrip() + "\n"


# ── Section helpers ──────────────────────────────────────────────────────────


def _title(anomaly: dict[str, Any], verdict: dict[str, Any]) -> str:
    severity = (anomaly.get("severity") or "UNKNOWN").upper()
    atype = (anomaly.get("anomaly_type") or "unknown").upper()
    cause = verdict.get("root_cause") or "Unspecified root cause"
    return f"[{severity}] {atype} anomaly — {cause}"


def _front_matter(anomaly: dict[str, Any], verdict: dict[str, Any]) -> str:
    aid = anomaly.get("anomaly_id") or "unknown"
    ts = anomaly.get("timestamp") or datetime.now(timezone.utc).isoformat()
    confidence = (verdict.get("confidence") or "unknown").lower()
    return (
        f"- **Anomaly ID:** `{aid}`\n"
        f"- **Detected at:** {ts}\n"
        f"- **Severity:** `{anomaly.get('severity') or 'UNKNOWN'}`\n"
        f"- **Type:** `{anomaly.get('anomaly_type') or 'unknown'}`\n"
        f"- **Confidence:** `{confidence}`\n"
        f"- **Generated:** {datetime.now(timezone.utc).isoformat()}"
    )


def _root_cause_block(verdict: dict[str, Any]) -> str:
    parts: list[str] = []
    rc = verdict.get("root_cause")
    if rc:
        parts.append(str(rc))
    hyp = verdict.get("hypothesis")
    if hyp:
        parts.append("")
        parts.append("**Considered hypotheses:**")
        parts.append(str(hyp))
    return "\n".join(parts) if parts else "_No root cause identified._"


def _evidence_block(verdict: dict[str, Any]) -> str:
    items = verdict.get("evidence") or []
    if not isinstance(items, list) or not items:
        return "_No tool evidence captured._"
    out: list[str] = []
    for ev in items:
        if not isinstance(ev, dict):
            continue
        src = ev.get("source") or "tool"
        finding = ev.get("finding") or ev.get("note") or ""
        out.append(f"- **{src}:** {finding}")
    return "\n".join(out) or "_No tool evidence captured._"


def _steps_block(verdict: dict[str, Any]) -> str:
    steps = verdict.get("remediation_steps") or []
    if not isinstance(steps, list) or not steps:
        return "_No remediation steps were proposed._"
    # Allow either pre-numbered strings or plain strings.
    out: list[str] = []
    for i, raw in enumerate(steps, start=1):
        text = str(raw).strip()
        if not text:
            continue
        # Strip a leading "1. " / "2. " if present so we don't double-number.
        if text[0:1].isdigit():
            head, _, rest = text.partition(".")
            if head.strip().isdigit():
                text = rest.strip() or text
        out.append(f"{i}. {text}")
    return "\n".join(out)


def _citations_block(verdict: dict[str, Any]) -> str:
    citations = verdict.get("citations") or []
    if not isinstance(citations, list) or not citations:
        return "_No runbook citations provided._"
    return "\n".join(f"- `{c}`" for c in citations if c)


def _trace_block(
    anomaly: dict[str, Any],
    trace: Iterable[dict[str, Any]] | None,
) -> str:
    """Render a compact summary of the tool calls the agent made."""
    if not trace:
        return "_No reasoning trace available._"
    tool_calls = [t for t in trace if t.get("kind") == "tool_call"]
    affected_nodes = anomaly.get("affected_nodes") or []
    affected_prefixes = anomaly.get("affected_prefixes") or []
    lines = [
        f"- **Tool calls:** {len(tool_calls)}",
        f"- **Affected nodes:** {', '.join(map(str, affected_nodes)) or '—'}",
        f"- **Affected prefixes:** {', '.join(map(str, affected_prefixes)) or '—'}",
    ]
    if tool_calls:
        lines.append("")
        lines.append("**Tool sequence:**")
        for tc in tool_calls:
            name = tc.get("name") or "unknown"
            args = tc.get("args") or {}
            lines.append(f"  - `{name}({_compact_args(args)})`")
    return "\n".join(lines)


def _compact_args(args: dict[str, Any]) -> str:
    pairs = []
    for k, v in args.items():
        if v is None:
            continue
        s = json.dumps(v, default=str)
        if len(s) > 60:
            s = s[:57] + "..."
        pairs.append(f"{k}={s}")
    return ", ".join(pairs)
