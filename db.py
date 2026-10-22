"""SQLite file locally, Postgres (e.g. Neon) when DATABASE_URL is set."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "grocery.db")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT UNIQUE NOT NULL,
    title TEXT,
    servings INTEGER,
    protein_per_serving_g REAL,
    steps TEXT,               -- JSON array
    raw_text TEXT,            -- caption + transcript, kept for re-parsing later
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    original TEXT,
    quantity REAL,
    unit TEXT,
    aisle TEXT,
    protein_g REAL
);
CREATE TABLE IF NOT EXISTS pantry (
    name TEXT PRIMARY KEY,
    is_staple INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS recipes (
        id SERIAL PRIMARY KEY,
        source_url TEXT UNIQUE NOT NULL,
        title TEXT,
        servings INTEGER,
        protein_per_serving_g REAL,
        steps TEXT,
        raw_text TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS ingredients (
        id SERIAL PRIMARY KEY,
        recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        original TEXT,
        quantity REAL,
        unit TEXT,
        aisle TEXT,
        protein_g REAL)""",
    """CREATE TABLE IF NOT EXISTS pantry (
        name TEXT PRIMARY KEY,
        is_staple INTEGER DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT now())""",
]


def _to_pg(sql: str) -> str:
    """The app's SQL is written for SQLite; these are the only differences it hits."""
    return (sql.replace("?", "%s")
               .replace("MAX(pantry.is_staple", "GREATEST(pantry.is_staple"))


class PgConn:
    """Gives a psycopg connection the same execute/executemany shape as sqlite3."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(_to_pg(sql), params)

    def executemany(self, sql, seq):
        with self._conn.cursor() as cur:
            cur.executemany(_to_pg(sql), list(seq))


def init_db():
    if DATABASE_URL:
        with get_conn() as conn:
            for stmt in PG_SCHEMA:
                conn.execute(stmt)
        return
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SQLITE_SCHEMA)


@contextmanager
def get_conn():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:  # commits on exit
            yield PgConn(conn)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
