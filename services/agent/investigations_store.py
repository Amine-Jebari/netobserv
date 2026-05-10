"""Postgres CRUD for the ``investigations`` table.

The agent writes one row per anomaly it processes: the originating event, the
reasoning trace, the final verdict, and the rendered runbook. The FastAPI
``GET /investigations`` endpoints read back from here.

All timestamps are stored as ``timestamptz`` in UTC. The trace and verdict
columns are JSONB and are read/written through ``psycopg.types.json.Jsonb`` so
nested structures (lists of dicts, etc.) round-trip cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import pooled_connection


# ── Public dataclass returned to API callers ─────────────────────────────────


@dataclass
class InvestigationRow:
    id: str
    anomaly_id: str
    anomaly_type: str
    severity: str
    started_at: datetime
    completed_at: datetime | None
    duration_seconds: float | None
    status: str
    model: str
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    anomaly_event: dict[str, Any] = field(default_factory=dict)
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    runbook_markdown: str | None = None
    error: str | None = None

    def to_summary(self) -> dict[str, Any]:
        """Compact projection for the GET /investigations list endpoint."""
        return {
            "id": self.id,
            "anomaly_id": self.anomaly_id,
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "duration_seconds": self.duration_seconds,
            "tool_call_count": self.tool_call_count,
            "model": self.model,
            "verdict_summary": (
                {
                    "root_cause": (self.verdict or {}).get("root_cause"),
                    "confidence": (self.verdict or {}).get("confidence"),
                }
                if self.verdict
                else None
            ),
        }

    def to_full(self) -> dict[str, Any]:
        return {
            **self.to_summary(),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "anomaly_event": self.anomaly_event,
            "reasoning_trace": self.reasoning_trace,
            "verdict": self.verdict,
            "runbook_markdown": self.runbook_markdown,
            "error": self.error,
        }


# ── CRUD ─────────────────────────────────────────────────────────────────────


def insert_started(
    *,
    anomaly_id: str,
    anomaly_type: str,
    severity: str,
    model: str,
    anomaly_event: dict[str, Any],
    settings: Settings | None = None,
) -> str:
    """Insert a fresh ``status='running'`` row and return its UUID.

    The agent updates the same row with the trace + verdict when the
    investigation finishes.
    """
    settings = settings or get_settings()
    new_id = str(uuid4())
    sql = f"""
        INSERT INTO {settings.investigations_table}
            (id, anomaly_id, anomaly_type, severity, model, anomaly_event, status, started_at)
        VALUES
            (%s, %s, %s, %s, %s, %s, 'running', NOW())
    """  # noqa: S608 - table name is from trusted Settings.
    with pooled_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    new_id,
                    anomaly_id,
                    anomaly_type,
                    severity,
                    model,
                    Jsonb(anomaly_event),
                ),
            )
        conn.commit()
    return new_id


def update_completed(
    *,
    investigation_id: str,
    status: str,
    reasoning_trace: list[dict[str, Any]],
    verdict: dict[str, Any] | None,
    runbook_markdown: str | None,
    tool_call_count: int,
    input_tokens: int,
    output_tokens: int,
    duration_seconds: float,
    error: str | None = None,
    settings: Settings | None = None,
) -> None:
    """Mark an investigation finished with its final results."""
    settings = settings or get_settings()
    sql = f"""
        UPDATE {settings.investigations_table}
           SET status = %s,
               completed_at = NOW(),
               duration_seconds = %s,
               reasoning_trace = %s,
               verdict = %s,
               runbook_markdown = %s,
               tool_call_count = %s,
               input_tokens = %s,
               output_tokens = %s,
               error = %s
         WHERE id = %s
    """  # noqa: S608 - table name is from trusted Settings.
    with pooled_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    status,
                    duration_seconds,
                    Jsonb(reasoning_trace),
                    Jsonb(verdict) if verdict is not None else None,
                    runbook_markdown,
                    tool_call_count,
                    input_tokens,
                    output_tokens,
                    error,
                    investigation_id,
                ),
            )
        conn.commit()


def list_recent(
    *,
    limit: int = 50,
    severity: str | None = None,
    settings: Settings | None = None,
) -> list[InvestigationRow]:
    """Return the most recent investigations, newest first."""
    settings = settings or get_settings()
    limit = max(1, min(int(limit), 500))

    where = ""
    params: list[Any] = []
    if severity:
        where = " WHERE severity = %s"
        params.append(severity.upper())
    params.append(limit)

    sql = f"""
        SELECT *
          FROM {settings.investigations_table}
          {where}
         ORDER BY started_at DESC
         LIMIT %s
    """  # noqa: S608 - table name is from trusted Settings.

    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [_row_to_dataclass(r) for r in rows]


def get_by_id(
    investigation_id: str,
    settings: Settings | None = None,
) -> InvestigationRow | None:
    """Fetch a single investigation by ID, or ``None`` if not found."""
    settings = settings or get_settings()
    try:
        UUID(investigation_id)
    except (TypeError, ValueError):
        return None

    sql = f"SELECT * FROM {settings.investigations_table} WHERE id = %s"  # noqa: S608
    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (investigation_id,))
            row = cur.fetchone()
    return _row_to_dataclass(row) if row else None


def get_by_anomaly_id(
    anomaly_id: str,
    settings: Settings | None = None,
) -> InvestigationRow | None:
    """Fetch the most recent investigation for an anomaly, if any."""
    settings = settings or get_settings()
    sql = f"""
        SELECT *
          FROM {settings.investigations_table}
         WHERE anomaly_id = %s
         ORDER BY started_at DESC
         LIMIT 1
    """  # noqa: S608
    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (anomaly_id,))
            row = cur.fetchone()
    return _row_to_dataclass(row) if row else None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_to_dataclass(row: dict[str, Any]) -> InvestigationRow:
    return InvestigationRow(
        id=str(row["id"]),
        anomaly_id=row["anomaly_id"],
        anomaly_type=row["anomaly_type"],
        severity=row["severity"],
        started_at=_ensure_aware(row["started_at"]),
        completed_at=_ensure_aware(row.get("completed_at")),
        duration_seconds=row.get("duration_seconds"),
        status=row["status"],
        model=row["model"],
        tool_call_count=int(row.get("tool_call_count") or 0),
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        anomaly_event=row.get("anomaly_event") or {},
        reasoning_trace=row.get("reasoning_trace") or [],
        verdict=row.get("verdict"),
        runbook_markdown=row.get("runbook_markdown"),
        error=row.get("error"),
    )
