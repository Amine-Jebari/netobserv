"""Tests for the deterministic runbook renderer."""

from __future__ import annotations

from services.agent.runbook import render_runbook


def _anomaly() -> dict:
    return {
        "anomaly_id": "abc-123",
        "anomaly_type": "bgp",
        "severity": "WARN",
        "timestamp": "2026-05-04T00:00:00Z",
        "affected_nodes": ["router-1"],
        "affected_prefixes": ["10.0.0.0/16"],
    }


def _verdict() -> dict:
    return {
        "summary": "BGP session flapping on edge router.",
        "hypothesis": "Hold-timer mismatch or ISP-side maintenance.",
        "root_cause": "Hold-timer mismatch with peer 192.0.2.1.",
        "confidence": "medium",
        "blast_radius": "10.0.0.0/16 reachability degraded.",
        "evidence": [
            {"source": "tool:get_recent_events", "finding": "5 withdraws in 2 minutes."},
            {"source": "runbook:bgp_flap_troubleshooting.md#Common Root Causes",
             "finding": "Symptom matches hold-timer profile."},
        ],
        "citations": ["bgp_flap_troubleshooting.md#Common Root Causes"],
        "remediation_steps": [
            "Confirm hold-timer values on both peers.",
            "Open ticket with upstream provider if mismatch confirmed.",
        ],
    }


def _trace() -> list[dict]:
    return [
        {"step": 1, "kind": "thought", "content": "investigating"},
        {
            "step": 2,
            "kind": "tool_call",
            "name": "get_recent_events",
            "args": {"prefix": "10.0.0.0/16"},
        },
        {"step": 3, "kind": "tool_result", "name": "get_recent_events"},
    ]


def test_render_runbook_returns_markdown_with_required_sections() -> None:
    md = render_runbook(_anomaly(), _verdict(), _trace())
    assert md is not None
    for header in (
        "# [WARN] BGP anomaly",
        "## Summary",
        "## Root cause",
        "## Blast radius",
        "## Evidence",
        "## Remediation steps",
        "## Citations",
        "## Investigation metadata",
    ):
        assert header in md, f"missing section: {header}"


def test_render_runbook_numbers_steps_consistently() -> None:
    md = render_runbook(_anomaly(), _verdict(), _trace())
    assert md is not None
    assert "1. Confirm hold-timer values" in md
    assert "2. Open ticket" in md


def test_render_runbook_renormalizes_pre_numbered_steps() -> None:
    v = _verdict()
    v["remediation_steps"] = [
        "1. already numbered",
        "2. another",
    ]
    md = render_runbook(_anomaly(), v, _trace())
    assert md is not None
    # Should not double-number ("1. 1. ...").
    assert "1. 1." not in md
    assert "1. already numbered" in md
    assert "2. another" in md


def test_render_runbook_returns_none_for_missing_verdict() -> None:
    assert render_runbook(_anomaly(), None) is None
    assert render_runbook(_anomaly(), {}) is not None  # empty dict is rendered, just sparsely


def test_render_runbook_includes_tool_sequence_summary() -> None:
    md = render_runbook(_anomaly(), _verdict(), _trace())
    assert md is not None
    assert "**Tool calls:** 1" in md
    assert "get_recent_events" in md
