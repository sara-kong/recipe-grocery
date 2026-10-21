from dotenv import load_dotenv
load_dotenv()

import json
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from db import get_conn, init_db
from extract import fetch_tiktok, parse_recipe

API_KEY = os.getenv("API_KEY")


def check_key(x_api_key: str | None = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Bad or missing X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Recipe -> Grocery", lifespan=lifespan, dependencies=[Depends(check_key)])


def key(name: str) -> str:
    """Loose match key so 'eggs' == 'egg'."""
    n = name.lower().strip()
    return n[:-1] if n.endswith("s") else n


# ---------- Recipes ----------

class RecipeIn(BaseModel):
    url: str


def load_recipe(conn, recipe_id: int) -> dict:
    r = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Recipe not found")
    ings = conn.execute("SELECT name, original, quantity, unit, aisle, protein_g "
                        "FROM ingredients WHERE recipe_id = ?", (recipe_id,)).fetchall()
    out = dict(r)
    out["steps"] = json.loads(out["steps"] or "[]")
    out.pop("raw_text", None)
    out["ingredients"] = [dict(i) for i in ings]
    return out


@app.post("/recipes")
def add_recipe(body: RecipeIn):
    """Share-sheet target: TikTok URL in, structured recipe out."""
    with get_conn() as conn:
        hit = conn.execute("SELECT id FROM recipes WHERE source_url = ?", (body.url,)).fetchone()
        if hit:
            return load_recipe(conn, hit["id"])

    try:
        src = fetch_tiktok(body.url)
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch video: {e}")

    with get_conn() as conn:  # short links resolve to a canonical URL
        hit = conn.execute("SELECT id FROM recipes WHERE source_url = ?",
                           (src["canonical_url"],)).fetchone()
        if hit:
            return load_recipe(conn, hit["id"])

    data = parse_recipe(src["caption"], src["transcript"])
    if not data.get("is_recipe") or not data.get("ingredients"):
        raise HTTPException(422, "That video doesn't look like a recipe")

    servings = max(int(data.get("servings") or 1), 1)
    protein = round(sum(i.get("protein_g") or 0 for i in data["ingredients"]) / servings, 1)

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recipes (source_url, title, servings, protein_per_serving_g, steps, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src["canonical_url"], data["title"], servings, protein, json.dumps(data["steps"]),
             f"CAPTION:\n{src['caption']}\n\nTRANSCRIPT:\n{src['transcript']}"),
        )
        rid = cur.lastrowid
        conn.executemany(
            "INSERT INTO ingredients (recipe_id, name, original, quantity, unit, aisle, protein_g) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(rid, i["name"].lower().strip(), i.get("original"), i.get("quantity"), i.get("unit"),
              i.get("aisle"), i.get("protein_g")) for i in data["ingredients"]],
        )
        return load_recipe(conn, rid)


@app.get("/recipes")
def list_recipes(min_protein: float = 0):
    """Sorted by protein per serving, highest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, servings, protein_per_serving_g, source_url FROM recipes "
            "WHERE protein_per_serving_g >= ? ORDER BY protein_per_serving_g DESC", (min_protein,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int):
    with get_conn() as conn:
        return load_recipe(conn, recipe_id)


@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return {"deleted": recipe_id}


# ---------- Pantry ----------

class PantryIn(BaseModel):
    items: list[str]
    is_staple: bool = False


@app.get("/pantry")
def get_pantry():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT name, is_staple FROM pantry ORDER BY name")]


@app.post("/pantry")
def add_pantry(body: PantryIn):
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO pantry (name, is_staple) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET is_staple = MAX(is_staple, excluded.is_staple), "
            "updated_at = CURRENT_TIMESTAMP",
            [(n.lower().strip(), int(body.is_staple)) for n in body.items if n.strip()],
        )
    return get_pantry()


@app.delete("/pantry/{name}")
def remove_pantry(name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM pantry WHERE name = ?", (name.lower().strip(),))
    return {"removed": name}


# ---------- Shopping ----------

@app.get("/recipes/{recipe_id}/missing")
def missing(recipe_id: int):
    """What to buy. `text` is newline-separated for an iOS Shortcut -> Reminders."""
    with get_conn() as conn:
        recipe = load_recipe(conn, recipe_id)
        have = {key(r["name"]) for r in conn.execute("SELECT name FROM pantry")}

    by_aisle = defaultdict(list)
    for i in recipe["ingredients"]:
        if key(i["name"]) not in have:
            by_aisle[i["aisle"] or "other"].append(i["name"])

    flat = sorted({n for names in by_aisle.values() for n in names})
    return {"recipe": recipe["title"], "by_aisle": by_aisle, "text": "\n".join(flat)}


@app.post("/recipes/{recipe_id}/cooked")
def cooked(recipe_id: int):
    """Remove this recipe's non-staple ingredients from the pantry."""
    with get_conn() as conn:
        recipe = load_recipe(conn, recipe_id)
        used = {key(i["name"]) for i in recipe["ingredients"]}
        removed = []
        for r in conn.execute("SELECT name FROM pantry WHERE is_staple = 0").fetchall():
            if key(r["name"]) in used:
                conn.execute("DELETE FROM pantry WHERE name = ?", (r["name"],))
                removed.append(r["name"])
    return {"removed": removed}
