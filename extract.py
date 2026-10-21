"""TikTok link -> caption + transcript -> structured recipe via Claude."""
import glob
import os
import tempfile
import uuid
from pathlib import Path

import anthropic
import httpx
import yt_dlp

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
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "tools": [RECIPE_TOOL],
        "tool_choice": {"type": "tool", "name": "save_recipe"},
        "messages": [{"role": "user", "content": PROMPT.format(
            caption=caption or "(none)", transcript=transcript or "(none)")}],
    }
    if LOCUS_API_KEY:
        return _parse_via_locus(body)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(**body)
    block = next(b for b in msg.content if b.type == "tool_use")
    return block.input


def _parse_via_locus(body: dict) -> dict:
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
