from dotenv import load_dotenv
load_dotenv()

import json
import os
import traceback
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db import get_conn, init_db
from extract import AISLES, fetch_tiktok, parse_receipt, parse_recipe, prepare_images

API_KEY = os.getenv("API_KEY")
APP_HTML = Path(__file__).with_name("app.html")


def check_key(x_api_key: str | None = Header(default=None),
              key: str | None = Query(default=None)):
    """Header for Shortcuts, ?key= for the web page (browsers can't set headers on a link)."""
    if API_KEY and API_KEY not in (x_api_key, key):
        raise HTTPException(401, "Bad or missing API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Recipe -> Grocery", lifespan=lifespan, dependencies=[Depends(check_key)])


def match_key(name: str) -> str:
    """Loose match so 'eggs' == 'egg'."""
    n = name.lower().strip()
    return n[:-1] if n.endswith("s") else n


def add_to_pantry(conn, names: list[str], is_staple: bool = False):
    conn.executemany(
        "INSERT INTO pantry (name, is_staple) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET is_staple = MAX(pantry.is_staple, excluded.is_staple), "
        "updated_at = CURRENT_TIMESTAMP",
        [(n.lower().strip(), int(is_staple)) for n in names if n.strip()],
    )


# ---------- Web app ----------

@app.get("/app", response_class=HTMLResponse)
def web_app():
    """Phone page: pick recipes, see the merged list, import receipts. Open as /app?key=..."""
    return APP_HTML.read_text()


# ---------- Recipes ----------

class RecipeIn(BaseModel):
    url: str


def load_recipe(conn, recipe_id: int) -> dict:
    r = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not r:
        raise HTTPException(404, f"Recipe {recipe_id} not found")
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
        traceback.print_exc()  # full details in the uvicorn terminal
        raise HTTPException(502, f"Couldn't fetch video: {type(e).__name__}: {e}")

    with get_conn() as conn:  # short links resolve to a canonical URL
        hit = conn.execute("SELECT id FROM recipes WHERE source_url = ?",
                           (src["canonical_url"],)).fetchone()
        if hit:
            return load_recipe(conn, hit["id"])

    try:
        data = parse_recipe(src["caption"], src["transcript"])
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(502, f"Couldn't parse recipe: {type(e).__name__}: {e}")
    if not data.get("is_recipe") or not data.get("ingredients"):
        raise HTTPException(422, "That video doesn't look like a recipe")

    servings = max(int(data.get("servings") or 1), 1)
    protein = round(sum(i.get("protein_g") or 0 for i in data["ingredients"]) / servings, 1)

    with get_conn() as conn:
        rid = conn.execute(
            "INSERT INTO recipes (source_url, title, servings, protein_per_serving_g, steps, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (src["canonical_url"], data["title"], servings, protein, json.dumps(data["steps"]),
             f"CAPTION:\n{src['caption']}\n\nTRANSCRIPT:\n{src['transcript']}"),
        ).fetchone()["id"]
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


@app.post("/recipes/{recipe_id}/cooked")
def cooked(recipe_id: int):
    """Remove this recipe's non-staple ingredients from the pantry."""
    with get_conn() as conn:
        recipe = load_recipe(conn, recipe_id)
        used = {match_key(i["name"]) for i in recipe["ingredients"]}
        removed = []
        for r in conn.execute("SELECT name FROM pantry WHERE is_staple = 0").fetchall():
            if match_key(r["name"]) in used:
                conn.execute("DELETE FROM pantry WHERE name = ?", (r["name"],))
                removed.append(r["name"])
    return {"removed": removed}


# ---------- Shopping list ----------

def fmt_amount(qty: float, unit: str) -> str:
    return f"{round(qty, 2):g} {unit}".strip()


def build_list(recipe_ids: list[int]) -> dict:
    """Merge several recipes, drop what's in the pantry, sum amounts that share a unit."""
    with get_conn() as conn:
        recipes = [load_recipe(conn, rid) for rid in dict.fromkeys(recipe_ids)]
        have = {match_key(r["name"]) for r in conn.execute("SELECT name FROM pantry")}

    items: dict[str, dict] = {}
    for r in recipes:
        for i in r["ingredients"]:
            k = match_key(i["name"])
            if k in have:
                continue
            item = items.setdefault(k, {"name": i["name"], "aisle": i["aisle"] or "other",
                                        "_amounts": defaultdict(float), "recipes": []})
            if i["quantity"] is not None:
                item["_amounts"][(i["unit"] or "").lower().strip()] += i["quantity"]
            if r["title"] not in item["recipes"]:
                item["recipes"].append(r["title"])

    by_aisle = defaultdict(list)
    for item in items.values():
        item["amount"] = " + ".join(fmt_amount(q, u) for u, q in item.pop("_amounts").items())
        by_aisle[item["aisle"]].append(item)

    ordered = {a: sorted(by_aisle[a], key=lambda x: x["name"])
               for a in AISLES + sorted(set(by_aisle) - set(AISLES)) if a in by_aisle}
    lines = []
    for aisle, aisle_items in ordered.items():
        lines.append(aisle.upper())
        lines += [f"- {i['name']}" + (f" ({i['amount']})" if i["amount"] else "") for i in aisle_items]
    return {"recipes": [r["title"] for r in recipes], "count": len(items),
            "by_aisle": ordered, "text": "\n".join(lines)}


class ListIn(BaseModel):
    recipe_ids: list[int]


@app.post("/shopping-list")
def shopping_list(body: ListIn):
    """Everything you're missing for the recipes you picked, merged and grouped by aisle."""
    if not body.recipe_ids:
        raise HTTPException(400, "Pick at least one recipe")
    return build_list(body.recipe_ids)


@app.get("/recipes/{recipe_id}/missing")
def missing(recipe_id: int):
    return build_list([recipe_id])


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
        add_to_pantry(conn, body.items, body.is_staple)
    return get_pantry()


class StapleIn(BaseModel):
    is_staple: bool


@app.put("/pantry/{name}")
def set_staple(name: str, body: StapleIn):
    """Mark or unmark something as a staple (always have it)."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE pantry SET is_staple = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                           (int(body.is_staple), name.lower().strip()))
        if cur.rowcount == 0:
            raise HTTPException(404, f"{name} isn't in your pantry")
    return get_pantry()


@app.delete("/pantry/{name}")
def remove_pantry(name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM pantry WHERE name = ?", (name.lower().strip(),))
    return {"removed": name}


@app.post("/pantry/import")
def import_purchase(file: UploadFile = File(...)):
    """Receipt photo or Uber Eats / DoorDash order screenshot -> items added to the pantry."""
    try:
        images = prepare_images(file.file.read())
    except ValueError as e:
        raise HTTPException(400, str(e))

    with get_conn() as conn:
        known = [r["name"] for r in conn.execute("SELECT DISTINCT name FROM ingredients ORDER BY name")]

    data = parse_receipt(images, known)
    names = sorted({i["name"].lower().strip() for i in data.get("items", []) if i.get("name")})

    with get_conn() as conn:
        add_to_pantry(conn, names)
    return {"source": data.get("source"), "store": data.get("store"), "added": names}
