# Recipe -> Grocery

TikTok link in, structured high-protein recipe + "what do I need to buy" out.

## Run locally
```bash
brew install ffmpeg          # yt-dlp needs it to pull audio
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export API_KEY=pick-a-secret # optional locally, required once deployed
uvicorn main:app --reload
```
Interactive docs: http://localhost:8000/docs

## Try it
```bash
H='X-API-Key: pick-a-secret'
curl -X POST localhost:8000/recipes -H "$H" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.tiktok.com/@someone/video/123"}'
curl -X POST localhost:8000/pantry -H "$H" -H 'Content-Type: application/json' \
  -d '{"items":["olive oil","salt","garlic"],"is_staple":true}'
curl localhost:8000/recipes/1/missing -H "$H"
```

## Endpoints
| Method | Path | What |
|---|---|---|
| POST | /recipes | `{url}` -> fetch, transcribe, parse, save (dedupes by URL) |
| GET | /recipes?min_protein=30 | list, highest protein/serving first |
| GET/DELETE | /recipes/{id} | one recipe |
| GET | /recipes/{id}/missing | shopping list by aisle + newline `text` |
| POST | /recipes/{id}/cooked | clear used non-staples from pantry |
| GET/POST | /pantry | `{items: [...], is_staple}` |
| DELETE | /pantry/{name} | ran out of something |

## Deploy (Railway)
Deploy from the Dockerfile, set `ANTHROPIC_API_KEY` and `API_KEY`, and attach a
volume at `/data` so SQLite survives redeploys. Give it >=1 GB RAM if Whisper runs.

## iOS Shortcuts
- **Add recipe** (share sheet, accepts URLs): Get Contents of URL -> POST `/recipes`,
  header `X-API-Key`, JSON body `url` = Shortcut Input -> Show `title` + `protein_per_serving_g`.
- **What do I need?**: GET `/recipes` -> Choose from List (titles) -> GET `/recipes/{id}/missing`
  -> Split `text` by New Lines -> Repeat: Add New Reminder to your Groceries list.

## Notes
- Protein is Claude's estimate. Swap in USDA FoodData Central later for real numbers.
- TikTok sometimes blocks scraping. Update yt-dlp first (`pip install -U yt-dlp`) when fetches fail.
- `raw_text` is stored so you can re-parse old recipes if you change the prompt.
