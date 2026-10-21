"""One-time copy of your local recipes and pantry to the deployed app.

Usage:
    python migrate.py https://your-app.up.railway.app your-deployed-api-key

Recipes are re-added from their TikTok links (each costs a few cents of credits again).
"""
import sqlite3
import sys

import httpx

if len(sys.argv) != 3:
    sys.exit(__doc__)

server, key = sys.argv[1].rstrip("/"), sys.argv[2]
headers = {"X-API-Key": key}
db = sqlite3.connect("grocery.db")
db.row_factory = sqlite3.Row

urls = [r["source_url"] for r in db.execute("SELECT source_url FROM recipes ORDER BY id")]
print(f"Copying {len(urls)} recipes...")
for url in urls:
    try:
        r = httpx.post(f"{server}/recipes", json={"url": url}, headers=headers, timeout=180)
        r.raise_for_status()
        print("  saved:", r.json()["title"])
    except Exception as e:
        print("  failed:", url, "->", getattr(e, "response", None) and e.response.text or e)

for staple in (1, 0):
    items = [r["name"] for r in db.execute("SELECT name FROM pantry WHERE is_staple = ?", (staple,))]
    if items:
        httpx.post(f"{server}/pantry", json={"items": items, "is_staple": bool(staple)},
                   headers=headers, timeout=30).raise_for_status()
        print(f"Copied {len(items)} {'staples' if staple else 'pantry items'}.")
print("Done.")
