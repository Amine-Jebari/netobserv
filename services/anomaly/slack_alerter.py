"""Direct Slack-webhook alerter for CRITICAL anomalies.

This is the engine's "guaranteed alert path": it fires before the LangChain
agent has had a chance to draft a runbook, so the on-call surface stays useful
even if the agent service is offline. The webhook URL comes from
``SLACK_WEBHOOK_URL`` — when blank, the alerter no-ops silently so dev
environments don't accidentally spam a real channel.
"""

from __future__ import annotations

from typing import Any

import httpx

from .logging_setup import get_logger

log = get_logger(__name__)


class SlackAlerter:
    """Posts a small Slack-formatted message per CRITICAL anomaly."""

    def __init__(self, *, webhook_url: str, timeout_seconds: float) -> None:
        self._webhook_url = webhook_url
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    async def start(self) -> None:
        if self.enabled:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def alert(self, event: dict[str, Any]) -> bool:
        """Send one alert. Returns ``True`` on HTTP 2xx, ``False`` otherwise."""

        if not self.enabled or self._client is None:
            return False
        payload = _build_payload(event)
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning(
                "slack.post_failed",
                anomaly_id=event.get("anomaly_id"),
                error=str(exc),
            )
            return False


def _build_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Format an anomaly event as a Slack ``blocks``-style message."""

    severity = event.get("severity", "CRITICAL")
    score = event.get("score", 0.0)
    anomaly_type = event.get("anomaly_type", "unknown")
    nodes = event.get("affected_nodes", []) or []
    prefixes = event.get("affected_prefixes", []) or []
    top = event.get("top_features", []) or []

    feature_lines = [
        f"• `{f.get('feature')}` value={f.get('value')} z={f.get('zscore')}"
        for f in top[:3]
    ]

    summary = (
        f":rotating_light: *{severity}* anomaly detected — "
        f"`{anomaly_type}` score={score:.3f}"
    )
    fields_text = (
        f"*Anomaly ID:* {event.get('anomaly_id')}\n"
        f"*Window:* {event.get('context_window_start')} → {event.get('context_window_end')}\n"
        f"*Affected nodes:* {', '.join(nodes) if nodes else '—'}\n"
        f"*Affected prefixes:* {', '.join(prefixes) if prefixes else '—'}\n"
        f"*Top features:*\n" + ("\n".join(feature_lines) if feature_lines else "—")
    )

    return {
        "text": summary,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
            {"type": "section", "text": {"type": "mrkdwn", "text": fields_text}},
        ],
    }
