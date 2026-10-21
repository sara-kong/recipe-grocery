import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "grocery.db")

SCHEMA = """
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
    name TEXT NOT NULL,       -- normalized grocery name, e.g. "chicken thighs"
    original TEXT,            -- as said in the video
    quantity REAL,
    unit TEXT,
    aisle TEXT,
    protein_g REAL
);

-- Presence-only pantry: "do I have it", not "how much".
CREATE TABLE IF NOT EXISTS pantry (
    name TEXT PRIMARY KEY,
    is_staple INTEGER DEFAULT 0,  -- staples never get removed by "cooked it"
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
