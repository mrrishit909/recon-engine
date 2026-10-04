"""Database access: migrations and SERIALIZABLE transactions with retry."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

T = TypeVar("T")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
Conn = psycopg.Connection[dict[str, object]]


def dsn() -> str:
    return os.environ["DATABASE_URL"]


def connect(url: str | None = None) -> Conn:
    return psycopg.connect(url or dsn(), row_factory=dict_row)


def migrate(url: str | None = None) -> list[str]:
    """Apply migrations/*.sql in name order, once each."""
    applied: list[str] = []
    with connect(url) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, at timestamptz DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name not in done:
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
    return applied


def serializable(fn: Callable[[Conn], T], url: str | None = None, retries: int = 8) -> T:
    """Run fn in one SERIALIZABLE transaction. If Postgres detects a conflict with a concurrent transaction it aborts
    one of them; we simply run it again, and the second run sees what the winner committed.
    A unique violation is retried too: under SERIALIZABLE it is how two racers inserting the same key surface."""
    for attempt in range(retries):
        # ponytail: a new connection per attempt; put a pool in front when request volume calls for it
        with connect(url) as conn:
            conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
            try:
                with conn.transaction():
                    return fn(conn)
            except (errors.SerializationFailure, errors.UniqueViolation, errors.DeadlockDetected):
                if attempt == retries - 1:
                    raise
    raise AssertionError("unreachable")


if __name__ == "__main__":
    print("applied:", migrate() or "nothing new")
