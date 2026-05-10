"""Tests for the bounded agent loop (``services.agent.agent``).

We do not depend on a real Gemini call in these tests. Instead we plug in a
``ScriptedChatModel`` that yields a pre-recorded sequence of ``AIMessage``
responses — first a tool call, then a final JSON verdict. The tool call
exercises the trace + token-counting paths without touching any backend.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.messages import AIMessage

from services.agent.agent import (
    InvestigationResult,
    parse_verdict,
    run_investigation,
)
from services.agent.config import Settings
from services.agent.metrics import AgentMetrics
from services.agent.rag import RunbookChunk
from services.agent.tools import Toolbox


# ── Test doubles ─────────────────────────────────────────────────────────────


class ScriptedChatModel:
    """Mimics ``ChatGoogleGenerativeAI.invoke([messages]) -> AIMessage``."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[Any]] = []
        self.model = "gemini-2.0-flash"

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        if not self._scripted:
            raise AssertionError("ScriptedChatModel exhausted")
        return self._scripted.pop(0)


def _toolbox(handler) -> Toolbox:
    settings = Settings(
        topology_service_url="http://topology:8080",
        influxdb_url="http://influxdb:8086",
        influxdb_token="t",
        influxdb_org="org",
        influxdb_bucket="metrics",
        opensearch_url="http://opensearch:9200",
        google_api_key="dummy",
    )
    return Toolbox(
        settings=settings,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        runbook_searcher=lambda q, k: [
            RunbookChunk(
                id="11111111-1111-1111-1111-111111111111",
                source_file="bgp_flap_troubleshooting.md",
                section_title="Common Root Causes",
                content="A flapping BGP session usually means the peer is unstable.",
                metadata={"anomaly_type": "bgp"},
                score=0.91,
            )
        ],
    )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_parse_verdict_extracts_fenced_json() -> None:
    text = """First the agent thinks aloud.

```json
{
  "summary": "BGP flap on peer 192.0.2.1",
  "root_cause": "Hold-timer mismatch",
  "confidence": "medium",
  "blast_radius": "10.0.0.0/16",
  "evidence": [],
  "citations": ["bgp_flap_troubleshooting.md#Common Root Causes"],
  "remediation_steps": ["Restore symmetric hold-timer", "Open ticket"]
}
```
"""
    verdict = parse_verdict(text)
    assert verdict is not None
    assert verdict["root_cause"] == "Hold-timer mismatch"
    assert verdict["confidence"] == "medium"


def test_parse_verdict_returns_none_for_unparseable() -> None:
    assert parse_verdict("no json here") is None
    assert parse_verdict("") is None


def test_run_investigation_handles_tool_call_then_final_json() -> None:
    """Two-turn flow: tool call → then a fenced JSON final answer."""

    final_json = (
        '```json\n'
        '{"summary": "BGP flap detected.", '
        '"hypothesis": "Hold-timer mismatch most likely.", '
        '"root_cause": "Hold-timer mismatch.", '
        '"confidence": "medium", '
        '"blast_radius": "10.0.0.0/16", '
        '"evidence": [{"source": "runbook:bgp_flap_troubleshooting.md", "finding": "matches"}], '
        '"citations": ["bgp_flap_troubleshooting.md#Common Root Causes"], '
        '"remediation_steps": ["Restore symmetric hold-timer."]}\n'
        '```'
    )

    tool_call_msg = AIMessage(
        content="Looking up the runbook for BGP flaps.",
        tool_calls=[
            {
                "id": "call_1",
                "name": "search_runbooks",
                "args": {"query": "bgp flap", "k": 3},
            }
        ],
        usage_metadata={"input_tokens": 100, "output_tokens": 25},
    )
    final_msg = AIMessage(
        content=final_json,
        usage_metadata={"input_tokens": 200, "output_tokens": 80},
    )

    chat = ScriptedChatModel([tool_call_msg, final_msg])
    metrics = AgentMetrics()

    # The tool call routes to search_runbooks_impl, which uses the stub
    # runbook_searcher rather than the HTTP client — so the handler is unused.
    tb = _toolbox(lambda r: httpx.Response(200, json={}))

    result: InvestigationResult = run_investigation(
        anomaly={
            "anomaly_id": "abc",
            "anomaly_type": "bgp",
            "severity": "WARN",
            "affected_prefixes": ["10.0.0.0/16"],
            "top_features": [],
        },
        chat_model=chat,
        toolbox=tb,
        metrics=metrics,
        max_tool_calls=8,
        timeout_seconds=10.0,
    )

    assert result.status == "completed"
    assert result.tool_call_count == 1
    assert result.input_tokens == 300
    assert result.output_tokens == 105
    assert result.verdict is not None
    assert result.verdict["root_cause"] == "Hold-timer mismatch."
    # Trace contains a thought, a tool_call, a tool_result, and a final entry.
    kinds = [s["kind"] for s in result.trace]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "final"


def test_run_investigation_enforces_tool_call_budget() -> None:
    """Once the cap is hit, the agent is asked to wrap up."""

    # Each scripted message asks for one tool call. We script 5 of them and
    # set the budget to 2; the 3rd (and beyond) should not invoke the tool.
    def call_msg(i: int) -> AIMessage:
        return AIMessage(
            content=f"step {i}",
            tool_calls=[
                {
                    "id": f"call_{i}",
                    "name": "search_runbooks",
                    "args": {"query": f"q{i}"},
                }
            ],
            usage_metadata={"input_tokens": 5, "output_tokens": 2},
        )

    final_msg = AIMessage(
        content=(
            '```json\n'
            '{"summary": "done", "hypothesis": "n/a", '
            '"root_cause": "n/a", "confidence": "low", '
            '"blast_radius": "none", "evidence": [], "citations": [], '
            '"remediation_steps": []}\n'
            '```'
        ),
        usage_metadata={"input_tokens": 1, "output_tokens": 1},
    )

    chat = ScriptedChatModel(
        [call_msg(1), call_msg(2), call_msg(3), call_msg(4), final_msg]
    )
    tb = _toolbox(lambda r: httpx.Response(200, json={}))

    result = run_investigation(
        anomaly={
            "anomaly_id": "x",
            "anomaly_type": "bgp",
            "severity": "WARN",
        },
        chat_model=chat,
        toolbox=tb,
        max_tool_calls=2,
        timeout_seconds=10.0,
    )

    assert result.tool_call_count == 2  # capped
    assert result.status == "completed"


def test_run_investigation_returns_error_when_no_verdict() -> None:
    """If the LLM emits non-JSON in its final message, we record that as an error."""

    bad_final = AIMessage(
        content="I have nothing to say.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1},
    )
    chat = ScriptedChatModel([bad_final])
    tb = _toolbox(lambda r: httpx.Response(200, json={}))

    result = run_investigation(
        anomaly={"anomaly_id": "y", "anomaly_type": "snmp", "severity": "WARN"},
        chat_model=chat,
        toolbox=tb,
        max_tool_calls=4,
        timeout_seconds=10.0,
    )

    assert result.status == "error"
    assert result.verdict is None
    assert result.error and "JSON" in result.error
