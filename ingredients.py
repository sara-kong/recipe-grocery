"""What counts as what: scallions = green onions, egg yolks come from eggs, water is free.

Every ingredient and pantry name gets one row in ingredient_map, filled in by Claude the
first time the app sees that name, then reused forever (and fixable by hand).
"""
import traceback

from db import get_conn
from extract import classify_ingredients


def clean(name: str) -> str:
    return name.lower().strip()


def _fallback(name: str) -> dict:
    n = clean(name)
    return {"name": n, "canonical": n, "family": n, "basic": False}


def lookup(conn, names) -> dict[str, dict]:
    """Map every name to its classification, falling back to the name itself."""
    names = {clean(n) for n in names if n and n.strip()}
    if not names:
        return {}
    found = {}
    rows = conn.execute("SELECT name, canonical, family, basic FROM ingredient_map").fetchall()
    for r in rows:
        if r["name"] in names:
            found[r["name"]] = {"name": r["name"], "canonical": r["canonical"],
                                "family": r["family"], "basic": bool(r["basic"])}
    return {n: found.get(n) or _fallback(n) for n in names}


def ensure_classified(names) -> None:
    """Classify any names the app hasn't seen yet. Never raises: worst case, a name matches itself."""
    names = sorted({clean(n) for n in names if n and n.strip()})
    if not names:
        return
    with get_conn() as conn:
        known = {r["name"] for r in conn.execute("SELECT name FROM ingredient_map").fetchall()}
        vocab = sorted({r["canonical"] for r in conn.execute("SELECT canonical FROM ingredient_map").fetchall()})
    missing = [n for n in names if n not in known]
    if not missing:
        return
    try:
        items = classify_ingredients(missing, vocab)
    except Exception:
        traceback.print_exc()
        return
    rows = []
    for it in items:
        n = clean(it.get("name", ""))
        if n in missing and it.get("canonical"):
            canonical = clean(it["canonical"])
            rows.append((n, canonical, clean(it.get("family") or canonical), int(bool(it.get("basic")))))
    if rows:
        with get_conn() as conn:
            conn.executemany(
                "INSERT INTO ingredient_map (name, canonical, family, basic) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO NOTHING", rows)


class Pantry:
    """What you have, in a form that answers 'do I have this ingredient?'"""

    def __init__(self, conn):
        names = [r["name"] for r in conn.execute("SELECT name FROM pantry").fetchall()]
        info = lookup(conn, names)
        self.canonicals = {i["canonical"] for i in info.values()}
        self.families = {i["family"] for i in info.values()}

    def has(self, ing: dict) -> bool:
        if ing["basic"]:
            return True
        if ing["canonical"] in self.canonicals:
            return True
        # A generic request ("cheese") is covered by anything in that group (mozzarella).
        return ing["canonical"] == ing["family"] and ing["family"] in self.families
