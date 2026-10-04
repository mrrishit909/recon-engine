"""Tests run against a real Postgres, in a database of their own (recon_test) that is rebuilt for every test."""
from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "postgresql://recon:recon-dev-only@127.0.0.1:54329/recon")
os.environ["RECON_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["RECON_API_TOKEN"] = "test-token"
BASE = os.environ["DATABASE_URL"]
TEST_URL = BASE.rsplit("/", 1)[0] + "/recon_test"      # never the database DATABASE_URL points at
os.environ["DATABASE_URL"] = TEST_URL


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    with psycopg.connect(BASE, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'recon_test'").fetchone():
            conn.execute("CREATE DATABASE recon_test")


@pytest.fixture
def db() -> Iterator[str]:
    from recon.db import migrate
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    migrate()
    yield TEST_URL
