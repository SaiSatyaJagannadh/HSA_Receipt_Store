"""Image normalization + NVIDIA NIM vision extraction.

Uses NVIDIA's OpenAI-compatible endpoint (https://integrate.api.nvidia.com/v1)
via the `openai` SDK, so the vision model is a config value, not a code change.

Two rules this module never breaks:
  1. The bytes uploaded to Drive are the original bytes. Normalization output is
     only ever sent to the model.
  2. If extraction fails for any reason, we return an empty result with the error
     in `ambiguities` — the app must still let me save the receipt manually.
"""

import base64
import io
import json
import mimetypes
import re
from dataclasses import dataclass, field

from .models import CATEGORIES
from .util import audit, retry

MAX_EDGE = 2000
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

# The response contract. NIM models vary in how strictly they honour
# response_format, so this shape is also spelled out in the prompt and the
# reply is parsed defensively.
RESPONSE_SHAPE = """{
  "provider": string or null,
  "service_date": "YYYY-MM-DD" or null,
  "total_amount": decimal string like "42.18" or null,
  "line_items": [{"description": string, "amount": decimal string or null}],
  "category": one of CATEGORIES or null,
  "description": string or null,
  "patient_name_if_visible": string or null,
  "is_medical_expense": true, false or null,
  "eligibility_confidence": "certain", "likely", "review" or null,
  "ambiguities": [string]
}"""

SYSTEM_PROMPT = f"""You read medical receipts for a personal HSA record-keeping system.

Reply with a single JSON object and nothing else. No prose, no markdown fences.

Shape:
{RESPONSE_SHAPE}

CATEGORIES must be exactly one of:
{", ".join(CATEGORIES)}

Rules:
- Return null for any field you cannot read with confidence. Never guess, never
  infer a plausible value, never substitute today's date.
- service_date is the date of service or purchase printed on the receipt. If the
  receipt shows only a transaction date, use that and say so in ambiguities.
- total_amount is what was actually paid, after any insurance adjustment or
  discount. If several totals appear, pick the amount due/paid by the patient and
  note the ambiguity. Digits and a decimal point only — no currency symbol.
- category must be one of the listed values, or null if none clearly fits.
- eligibility_confidence: "certain" if this is unambiguously a qualified medical
  expense, "likely" if it probably is, "review" if it is unclear, non-medical, or
  a mixed-purchase receipt.
- List every unclear or unreadable field in ambiguities, in plain English.

You are extracting data, not giving tax advice."""

WATCHED_FIELDS = ("provider", "service_date", "total_amount", "category", "description")


@dataclass
class Extraction:
    data: dict = field(default_factory=dict)
    raw: str = ""
    error: str = ""

    @property
    def ambiguities(self) -> list[str]:
        found = list(self.data.get("ambiguities") or [])
        if self.error:
            found.append(f"Automatic extraction unavailable: {self.error}")
        return found

    @property
    def uncertain_fields(self) -> set[str]:
        """Fields the model could not read — the UI highlights these."""
        return {f for f in WATCHED_FIELDS if not self.data.get(f)}


# --- normalization ---------------------------------------------------------


def _register_heif() -> None:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass


def normalize(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Original bytes -> list of (media_type, bytes) images to send to the model.

    Auto-rotates via EXIF, converts HEIC to JPEG, downscales to MAX_EDGE, and
    rasterizes multi-page PDFs into per-page images. Vision models take images
    only, so an un-rasterizable PDF yields no pages and extraction reports why.
    """
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()

    if ext == "pdf":
        return _pdf_to_images(data)

    _register_heif()
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
    except Exception:
        # Unreadable as an image — hand the raw bytes over and let the API judge.
        return [(mimetypes.guess_type(filename)[0] or "image/jpeg", data)]

    return [("image/jpeg", _to_jpeg(ImageOps.exif_transpose(image)))]


def _to_jpeg(image) -> bytes:
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if max(image.size) > MAX_EDGE:
        ratio = MAX_EDGE / max(image.size)
        image = image.resize((int(image.width * ratio), int(image.height * ratio)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _pdf_to_images(data: bytes) -> list[tuple[str, bytes]]:
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        return []
    try:
        pages = convert_from_bytes(data, dpi=150)
    except Exception:
        # ponytail: poppler missing or PDF unreadable. Caller degrades to manual.
        return []
    return [("image/jpeg", _to_jpeg(page)) for page in pages]


# --- extraction ------------------------------------------------------------


def _image_block(media_type: str, payload: bytes) -> dict:
    encoded = base64.standard_b64encode(payload).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
    }


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_reply(text: str) -> dict:
    """NIM models sometimes wrap JSON in fences or add a sentence around it.

    Try the whole string, then a fenced block, then the outermost {...}.
    """
    if not text or not text.strip():
        raise ValueError("model returned an empty response")
    candidates = [text.strip()]
    if match := _FENCE.search(text):
        candidates.append(match.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object found in the model response")


def normalize_fields(data: dict) -> dict:
    """Coerce a loosely-typed reply into the shape the UI expects."""
    out = dict(data)
    if out.get("category") not in CATEGORIES:
        out["category"] = None
    if out.get("eligibility_confidence") not in ("certain", "likely", "review"):
        out["eligibility_confidence"] = None
    amount = out.get("total_amount")
    if amount is not None:
        cleaned = str(amount).replace("$", "").replace(",", "").strip()
        out["total_amount"] = cleaned or None
    items = out.get("line_items")
    out["line_items"] = items if isinstance(items, list) else []
    notes = out.get("ambiguities")
    out["ambiguities"] = [str(n) for n in notes] if isinstance(notes, list) else []
    for key in WATCHED_FIELDS + ("patient_name_if_visible",):
        if isinstance(out.get(key), str) and not out[key].strip():
            out[key] = None
    return out


@retry(tries=4)
def _call(client, model: str, blocks: list[dict]):
    return client.chat.completions.create(
        model=model,
        max_tokens=2048,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": blocks
                + [
                    {
                        "type": "text",
                        "text": (
                            "Extract this receipt as JSON. Every image above belongs "
                            "to a single receipt. Use null for anything you cannot read."
                        ),
                    }
                ],
            },
        ],
    )


def extract(
    pages: list[tuple[str, bytes]],
    api_key: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
) -> Extraction:
    """Never raises. On any failure the app falls back to fully manual entry."""
    if not api_key:
        return Extraction(error="no NVIDIA_API_KEY configured")
    if not pages:
        return Extraction(error="file could not be converted to an image")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
        response = _call(client, model, [_image_block(m, b) for m, b in pages])
        text = response.choices[0].message.content or ""
        data = normalize_fields(parse_reply(text))
        audit("extraction.ok", model=model, provider=data.get("provider"))
        return Extraction(data=data, raw=text)
    except Exception as exc:  # noqa: BLE001 - graceful degradation is the point
        audit("extraction.failed", model=model, error=str(exc)[:400])
        return Extraction(error=f"{type(exc).__name__}: {exc}"[:400])
