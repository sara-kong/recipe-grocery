from dotenv import load_dotenv
load_dotenv()

import json
import os
import traceback
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from db import get_conn, init_db
from extract import (AISLES, fetch_thumbnail, fetch_tiktok, parse_receipt, parse_recipe,
                     prepare_images, thumbnail_for_video)
from ingredients import Pantry, clean, ensure_classified, lookup

API_KEY = os.getenv("API_KEY")
APP_HTML = Path(__file__).with_name("app.html")
ICON = Path(__file__).with_name("icon.png")
PUBLIC_PATHS = {"/icon.png", "/health"}  # icon fetch and uptime pings come without your key


def check_key(request: Request,
              x_api_key: str | None = Header(default=None),
              key: str | None = Query(default=None)):
    """Header for Shortcuts, ?key= for the web page (browsers can't set headers on a link)."""
    if request.url.path in PUBLIC_PATHS:
        return
    if API_KEY and API_KEY not in (x_api_key, key):
        raise HTTPException(401, "Bad or missing API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Recipe -> Grocery", lifespan=lifespan, dependencies=[Depends(check_key)])


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


@app.get("/health", include_in_schema=False)
def health():
    """Cheap endpoint for an uptime pinger, so the free server stays awake."""
    return {"ok": True}


@app.get("/icon.png", include_in_schema=False)
def icon():
    """Home screen icon. Replace icon.png with any square PNG."""
    if not ICON.exists():
        raise HTTPException(404, "No icon.png in the project folder")
    return FileResponse(ICON, media_type="image/png")


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
    ensure_classified(i["name"] for i in data["ingredients"])
    try:
        cover = fetch_thumbnail(src.get("thumbnail"))
        if cover:  # if there isn't one now, the image route tries again later
            save_image(rid, cover)
    except Exception:
        traceback.print_exc()  # a missing cover never blocks saving the recipe
    with get_conn() as conn:
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
    """The recipe, with each ingredient marked as have / kitchen basic / need."""
    with get_conn() as conn:
        recipe = load_recipe(conn, recipe_id)
        pantry_names = [r["name"] for r in conn.execute("SELECT name FROM pantry").fetchall()]
    ensure_classified([i["name"] for i in recipe["ingredients"]] + pantry_names)
    with get_conn() as conn:
        info = lookup(conn, [i["name"] for i in recipe["ingredients"]])
        pantry = Pantry(conn)
    for i in recipe["ingredients"]:
        c = info[clean(i["name"])]
        i["canonical"], i["basic"], i["have"] = c["canonical"], c["basic"], pantry.has(c)
    return recipe


def save_image(recipe_id: int, data: bytes | None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO recipe_images (recipe_id, data, failed) VALUES (?, ?, ?) "
            "ON CONFLICT(recipe_id) DO UPDATE SET data = excluded.data, failed = excluded.failed",
            (recipe_id, data, 0 if data else 1))


@app.get("/recipes/{recipe_id}/image")
def recipe_image(recipe_id: int):
    """The video's cover image. Older recipes get theirs looked up the first time they're shown."""
    with get_conn() as conn:
        rec = conn.execute("SELECT source_url FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        row = conn.execute("SELECT data, failed FROM recipe_images WHERE recipe_id = ?",
                           (recipe_id,)).fetchone()
    if not rec:
        raise HTTPException(404, "Recipe not found")
    data = bytes(row["data"]) if row and row["data"] else None
    if data is None and not (row and row["failed"]):
        try:
            data = thumbnail_for_video(rec["source_url"])
        except Exception:
            traceback.print_exc()
        save_image(recipe_id, data)
    if not data:
        raise HTTPException(404, "No image for this recipe")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return {"deleted": recipe_id}


def cooked_matches(recipe_id: int) -> list[dict]:
    """Pantry items (not staples) that this recipe drew from."""
    with get_conn() as conn:
        recipe = load_recipe(conn, recipe_id)
        pantry_names = [r["name"] for r in conn.execute(
            "SELECT name FROM pantry WHERE is_staple = 0 ORDER BY name").fetchall()]
    ensure_classified([i["name"] for i in recipe["ingredients"]] + pantry_names)
    with get_conn() as conn:
        ing_info = lookup(conn, [i["name"] for i in recipe["ingredients"]])
        pantry_info = lookup(conn, pantry_names)
    used_for, generic_for = {}, {}
    for i in recipe["ingredients"]:
        c = ing_info[clean(i["name"])]
        if c["basic"]:
            continue
        used_for.setdefault(c["canonical"], i["name"])
        if c["canonical"] == c["family"]:  # e.g. "shredded cheese" could have come from your mozzarella
            generic_for.setdefault(c["family"], i["name"])
    matches = []
    for n in pantry_names:
        p = pantry_info[clean(n)]
        used = used_for.get(p["canonical"]) or generic_for.get(p["family"])
        if used:
            matches.append({"name": n, "counts_as": p["canonical"], "used_for": used})
    return matches


@app.get("/recipes/{recipe_id}/cooked")
def cooked_check(recipe_id: int):
    """What to ask about after cooking: the pantry items this recipe used some of."""
    return {"items": cooked_matches(recipe_id)}


class CookedIn(BaseModel):
    used_up: list[str] | None = None  # None = assume everything was used up


@app.post("/recipes/{recipe_id}/cooked")
def cooked(recipe_id: int, body: CookedIn | None = None):
    """Remove what you finished. Items you still have some of stay in the pantry."""
    names = [m["name"] for m in cooked_matches(recipe_id)]
    if body is not None and body.used_up is not None:
        finished = {clean(n) for n in body.used_up}
        removed = [n for n in names if clean(n) in finished]
    else:
        removed = names
    with get_conn() as conn:
        for n in removed:
            conn.execute("DELETE FROM pantry WHERE name = ?", (n,))
    return {"removed": removed, "kept": [n for n in names if n not in removed]}


# ---------- Shopping list ----------

def fmt_amount(qty: float, unit: str) -> str:
    return f"{round(qty, 2):g} {unit}".strip()


def build_list(recipe_ids: list[int]) -> dict:
    """Merge several recipes, drop what's in the pantry, sum amounts that share a unit."""
    with get_conn() as conn:
        recipes = [load_recipe(conn, rid) for rid in dict.fromkeys(recipe_ids)]
        pantry_names = [r["name"] for r in conn.execute("SELECT name FROM pantry").fetchall()]
    all_names = [i["name"] for r in recipes for i in r["ingredients"]]
    ensure_classified(all_names + pantry_names)
    with get_conn() as conn:
        info = lookup(conn, all_names)
        pantry = Pantry(conn)

    items: dict[str, dict] = {}
    for r in recipes:
        for i in r["ingredients"]:
            c = info[clean(i["name"])]
            if pantry.has(c):
                continue
            k = c["canonical"]
            item = items.setdefault(k, {"name": k, "aisle": i["aisle"] or "other",
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
        rows = [dict(r) for r in conn.execute("SELECT name, is_staple FROM pantry ORDER BY name")]
        info = lookup(conn, [r["name"] for r in rows])
    for r in rows:
        r["counts_as"] = info[clean(r["name"])]["canonical"]
    return rows


@app.post("/pantry")
def add_pantry(body: PantryIn):
    with get_conn() as conn:
        add_to_pantry(conn, body.items, body.is_staple)
    ensure_classified(body.items)
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
    ensure_classified(names)
    return {"source": data.get("source"), "store": data.get("store"), "added": names}


# ---------- Ingredient dictionary ----------

@app.get("/ingredients/map")
def ingredient_map():
    """Everything the app has learned: name -> what it counts as."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT name, canonical, family, basic FROM ingredient_map ORDER BY canonical, name")]


class MapIn(BaseModel):
    canonical: str
    family: str | None = None
    basic: bool = False


@app.put("/ingredients/map/{name}")
def fix_mapping(name: str, body: MapIn):
    """Correct one classification by hand, e.g. make 'shredded cheese' count as 'mozzarella'."""
    canonical = clean(body.canonical)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingredient_map (name, canonical, family, basic) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET canonical = excluded.canonical, "
            "family = excluded.family, basic = excluded.basic",
            (clean(name), canonical, clean(body.family or canonical), int(body.basic)))
    return {"name": clean(name), "canonical": canonical}
