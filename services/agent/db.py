"""Postgres connection helpers for the agent / RAG layer.

The two consumers (the long-running agent and the one-shot seed script) want
slightly different lifecycles, so this module exposes both:

- ``connect()``     - a single short-lived connection (used by the seeder).
- ``get_pool()``    - a process-level psycopg_pool.ConnectionPool used by the
                      agent to amortize connect cost across many queries.

Either way, the ``vector`` adapter is registered on the connection so that
numpy arrays can be passed to and read from pgvector columns.
"""

from __future__ import annotations

import threading
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from .config import Settings, get_settings


_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def _conninfo(settings: Settings) -> str:
    """Build a libpq conninfo string from Settings."""
    return (
        f"host={settings.postgres_host} "
        f"port={settings.postgres_port} "
        f"dbname={settings.postgres_db} "
        f"user={settings.postgres_user} "
        f"password={settings.postgres_password} "
        f"connect_timeout={int(settings.postgres_connect_timeout_seconds)}"
    )


def _configure_connection(conn: psycopg.Connection) -> None:
    """Register the pgvector adapter on a freshly-opened connection."""
    register_vector(conn)


def connect(settings: Settings | None = None) -> psycopg.Connection:
    """Return a new short-lived psycopg connection with the vector adapter set up."""
    settings = settings or get_settings()
    conn = psycopg.connect(_conninfo(settings))
    _configure_connection(conn)
    return conn


def get_pool(settings: Settings | None = None) -> ConnectionPool:
    """Return a process-level psycopg ConnectionPool, lazily constructed."""
    global _POOL
    settings = settings or get_settings()

    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ConnectionPool(
                conninfo=_conninfo(settings),
                min_size=1,
                max_size=settings.postgres_pool_size,
                configure=_configure_connection,
                open=True,
            )
        return _POOL


def close_pool() -> None:
    """Close the process-level pool. Idempotent."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


def pooled_connection(settings: Settings | None = None) -> Iterator[psycopg.Connection]:
    """Context manager yielding a pooled connection.

    Usage::

        with pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    return get_pool(settings).connection()
