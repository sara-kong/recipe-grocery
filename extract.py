import glob
import os
import tempfile
import uuid
from pathlib import Path

import base64
import io

import anthropic
import httpx
import yt_dlp
from PIL import Image, ImageOps

try:  # lets the server read iPhone HEIC photos too
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
LOCUS_API_KEY = os.getenv("LOCUS_API_KEY")  # if set, Claude calls are paid from Locus credits
LOCUS_ANTHROPIC_URL = "https://api.paywithlocus.com/api/wrapped/credits/anthropic/chat"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")  # tiny/base/small

AISLES = ["produce", "meat & seafood", "dairy & eggs", "bakery", "pantry",
          "spices & condiments", "frozen", "other"]


# ---------- 1. Fetch ----------

def fetch_tiktok(url: str) -> dict:
    """Download caption, subtitles if TikTok has them, else transcribe the audio."""
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "outtmpl": f"{tmp}/video.%(ext)s",
            "format": "bestaudio/best",
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en.*", "eng.*"],
            "subtitlesformat": "vtt",
            "noplaylist": True,
            # Local dev only: set YTDLP_INSECURE=1 if your network intercepts HTTPS.
            "nocheckcertificate": os.getenv("YTDLP_INSECURE") == "1",
            "quiet": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        transcript = _read_subs(tmp)
        if not transcript:
            audio = next(iter(glob.glob(f"{tmp}/*.mp3")), None)
            if audio:
                transcript = transcribe(audio)

        return {
            "caption": info.get("description") or "",
            "transcript": transcript,
            "uploader": info.get("uploader"),
            "canonical_url": info.get("webpage_url") or url,
        }


def _read_subs(folder: str) -> str:
    files = glob.glob(f"{folder}/*.vtt")
    if not files:
        return ""
    lines = []
    for line in Path(files[0]).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if (not line or line == "WEBVTT" or "-->" in line or line.isdigit()
                or line.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return " ".join(lines)


_whisper = None

def transcribe(audio_path: str) -> str:
    global _whisper
    from faster_whisper import WhisperModel  # lazy: only load if subs are missing
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(audio_path, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segments)


# ---------- 2. Parse ----------

RECIPE_TOOL = {
    "name": "save_recipe",
    "description": "Save the structured recipe extracted from a cooking video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_recipe": {"type": "boolean",
                          "description": "False if the video isn't actually a recipe."},
            "title": {"type": "string"},
            "servings": {"type": "integer", "minimum": 1},
            "ingredients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Generic grocery name, lowercase, no prep words "
                                                "or brands. e.g. 'chicken thighs', 'greek yogurt', 'garlic'."},
                        "original": {"type": "string", "description": "As stated in the video."},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "aisle": {"type": "string", "enum": AISLES},
                        "protein_g": {"type": "number",
                                      "description": "Estimated total protein (g) for this quantity."},
                    },
                    "required": ["name", "aisle", "protein_g"],
                },
            },
            "steps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["is_recipe", "title", "servings", "ingredients", "steps"],
    },
}

PROMPT = """Extract the recipe from this TikTok. The caption, the spoken transcript, or both may
contain the ingredients. Merge them, dedupe, and use stated quantities. If a quantity
isn't stated, estimate a typical amount for the servings. Include oils, salt and spices.
If servings aren't stated, estimate.

CAPTION:
{caption}

TRANSCRIPT:
{transcript}"""


def parse_recipe(caption: str, transcript: str) -> dict:
    return call_claude(RECIPE_TOOL, PROMPT.format(
        caption=caption or "(none)", transcript=transcript or "(none)"))


# ---------- 3. Receipts and delivery-order screenshots ----------

RECEIPT_TOOL = {
    "name": "save_purchases",
    "description": "Save the grocery items bought on this receipt or delivery order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["store receipt", "delivery order", "other"]},
            "store": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Generic grocery name, lowercase, no brand or size, "
                                                "the way it would appear in a recipe. e.g. 'chicken thighs'."},
                        "original": {"type": "string", "description": "As printed on the receipt or order."},
                    },
                    "required": ["name", "original"],
                },
            },
        },
        "required": ["source", "items"],
    },
}

RECEIPT_PROMPT = """These images show one grocery purchase: a store receipt or a delivery order
screenshot (Uber Eats, DoorDash, Instacart, etc.). A long screenshot may be split into
consecutive pieces that overlap slightly, so don't count an item twice.

List the food and cooking items that were actually bought.
- Expand store abbreviations ("BNLS CHKN THGH" -> "chicken thighs").
- Skip fees, tax, tips, bags, discounts, deposits and non-food items.
- For delivery orders, skip items marked refunded, out of stock or not found, and list
  substitutions as the item actually delivered.
- If an item is the same thing as one of these ingredient names already in the app, use that
  exact name: {known}"""


def parse_receipt(images_b64: list[str], known_names: list[str]) -> dict:
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in images_b64]
    content.append({"type": "text", "text": RECEIPT_PROMPT.format(
        known=", ".join(known_names[:300]) or "(none yet)")})
    return call_claude(RECEIPT_TOOL, content)


def prepare_images(raw: bytes, width: int = 1000, piece_height: int = 1500,
                   overlap: int = 100, max_pieces: int = 8) -> list[str]:
    """Shrink to a readable width, then cut tall screenshots into pieces so text stays sharp."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        raise ValueError("Couldn't read that image. Try a JPEG or PNG screenshot.")

    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))

    pieces, top = [], 0
    while top < img.height and len(pieces) < max_pieces:
        box = (0, top, img.width, min(top + piece_height, img.height))
        buf = io.BytesIO()
        img.crop(box).save(buf, format="JPEG", quality=85)
        pieces.append(base64.b64encode(buf.getvalue()).decode())
        top += piece_height - overlap
    return pieces


# ---------- 4. Ingredient classification ----------

CLASSIFY_TOOL = {
    "name": "classify_ingredients",
    "description": "Map each ingredient name to what it counts as when shopping.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exactly as given."},
                        "canonical": {"type": "string"},
                        "family": {"type": "string"},
                        "basic": {"type": "boolean"},
                    },
                    "required": ["name", "canonical", "family", "basic"],
                },
            },
        },
        "required": ["items"],
    },
}

CLASSIFY_PROMPT = """Classify these grocery ingredient names for a shopping list app. For each name give:

canonical: the single thing you'd buy that covers it, lowercase, plural where natural.
- Synonyms share one canonical: scallions and green onions -> "green onions".
- Parts, preparations and forms map to what you buy: egg yolks, egg whites -> "eggs";
  grilled, diced or caramelized onions -> "onions"; garlic cloves, minced garlic -> "garlic";
  lemon juice, lemon zest -> "lemons"; cooked rice -> "rice".
- Varieties people swap freely share one canonical: olive, avocado, vegetable or canola oil and
  cooking spray -> "cooking oil"; kosher or sea salt -> "salt"; flour or corn tortillas -> "tortillas";
  any honey -> "honey"; light or regular soy sauce -> "soy sauce".
- Light, low-fat, shredded, sliced or store-brand versions map to the base: light mozzarella,
  shredded mozzarella -> "mozzarella".
- Keep things distinct when the type changes the dish: sesame oil, parmesan, cheddar, mozzarella.
- A generic name stays generic: "cheese" or "shredded cheese" with no type -> "cheese".

family: the broader group, used to match generic requests. cheddar, mozzarella, parmesan and
cheese -> "cheese"; cooking oil and sesame oil -> "oil". If there's no broader group, repeat the canonical.

basic: true only for things every kitchen always has: water of any kind (lukewarm, ice, pasta or
cooking water), ice, salt and black pepper. Everything else is false.

If one of these existing canonical names fits, reuse it exactly: {vocab}

Names:
{names}"""


def classify_ingredients(names: list[str], vocab: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(names), 80):  # keep each request small
        chunk = names[i:i + 80]
        data = call_claude(CLASSIFY_TOOL, CLASSIFY_PROMPT.format(
            vocab=", ".join(vocab[:400]) or "(none yet)",
            names="\n".join(chunk)))
        out += data.get("items", [])
    return out


# ---------- Claude call (Locus credits or Anthropic directly) ----------

def call_claude(tool: dict, content) -> dict:
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": content}],
    }
    if LOCUS_API_KEY:
        return _call_via_locus(body)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(**body)
    block = next(b for b in msg.content if b.type == "tool_use")
    return block.input


def _call_via_locus(body: dict) -> dict:
    """Same Anthropic request body, sent through Locus Pro and paid from credits."""
    resp = httpx.post(
        LOCUS_ANTHROPIC_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {LOCUS_API_KEY}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Locus {resp.status_code}: {resp.text[:500]}")

    print(f"[locus] charged {resp.headers.get('X-Locus-Credits-Charged')}, "
          f"balance {resp.headers.get('X-Locus-Credits-Balance')}")

    data = resp.json()
    data = data.get("data", data) if "content" not in data else data  # tolerate a wrapper
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if not block:
        raise RuntimeError(f"No tool_use block in Locus response: {str(data)[:500]}")
    return block["input"]
