# Recipe -> Grocery

Share a TikTok recipe, pick what you're cooking this week, and get one list of only what
you're missing. Snap a receipt or a delivery-order screenshot to update what you have.

## Run locally
```bash
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8001
```

`.env` (never commit it):
```
LOCUS_API_KEY=lcac_...        # or ANTHROPIC_API_KEY=sk-ant-... to pay Anthropic directly
CLAUDE_MODEL=claude-haiku-5-5
YTDLP_INSECURE=1              # local only, if your network intercepts HTTPS
API_KEY=pick-a-secret         # optional locally, required once deployed
```

## On your phone
- **Phone page:** open `http://<laptop-ip>:8001/app?key=<API_KEY>` in Safari, then
  Share -> Add to Home Screen. Pick recipes (sorted by protein), tap **Make list**, and
  check items off in the store. Tap a recipe to see its ingredients and steps, or mark it **Cooked it**. **Pantry** lets you add, remove, and mark staples. **Add receipt** takes a receipt photo or an Uber Eats /
  DoorDash order screenshot and adds what you bought to your pantry.
- **Add Recipe shortcut:** Show in Share Sheet (URLs + Text) -> Get URLs from Input ->
  Get Contents of URL (POST `/recipes`, JSON body `url`, header `X-API-Key`) ->
  Show Notification with the `title`.

Find your laptop's address with `ipconfig getifaddr en0`. Phone and laptop need the same Wi-Fi.

## Endpoints
| Method | Path | What |
|---|---|---|
| POST | /recipes | `{url}` -> fetch, transcribe, parse, save (dedupes by URL) |
| GET | /recipes | list, highest protein per serving first |
| GET/DELETE | /recipes/{id} | one recipe |
| POST | /shopping-list | `{recipe_ids: [...]}` -> merged missing items by aisle |
| GET | /recipes/{id}/missing | same, for one recipe |
| POST | /recipes/{id}/cooked | remove used ingredients from the pantry (staples stay) |
| GET/POST | /pantry | `{items: [...], is_staple}` |
| PUT | /pantry/{name} | `{is_staple}`: mark or unmark a staple |
| DELETE | /pantry/{name} | ran out of something |
| POST | /pantry/import | form field `file` (image) -> items added to the pantry |
| GET | /app | the phone page |

## Notes
- Protein numbers are Claude's estimates.
- Long screenshots are cut into overlapping pieces so small text stays readable.
- Order history comes from screenshots you take, not from scraping Uber Eats or DoorDash.
- If TikTok fetches fail, run `pip install -U yt-dlp` first.
