"""Image normalization + Claude vision extraction.

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
from dataclasses import dataclass, field

from .models import CATEGORIES
from .util import audit, retry

MAX_EDGE = 2000

def _nullable(schema: dict) -> dict:
    """anyOf-with-null, not `type: [x, "null"]` — the structured-outputs schema
    subset documents anyOf but not type arrays."""
    return {"anyOf": [schema, {"type": "null"}]}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": _nullable({"type": "string"}),
        "service_date": _nullable(
            {
                "type": "string",
                "description": "Date of service or purchase in YYYY-MM-DD. NOT today's date.",
            }
        ),
        "total_amount": _nullable(
            {
                "type": "string",
                "description": "Total paid, as a decimal string like '42.18'. No currency symbol.",
            }
        ),
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount": _nullable({"type": "string"}),
                },
                "required": ["description", "amount"],
                "additionalProperties": False,
            },
        },
        "category": _nullable({"type": "string", "enum": CATEGORIES}),
        "description": _nullable({"type": "string"}),
        "patient_name_if_visible": _nullable({"type": "string"}),
        "is_medical_expense": _nullable({"type": "boolean"}),
        "eligibility_confidence": _nullable(
            {"type": "string", "enum": ["certain", "likely", "review"]}
        ),
        "ambiguities": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "provider",
        "service_date",
        "total_amount",
        "line_items",
        "category",
        "description",
        "patient_name_if_visible",
        "is_medical_expense",
        "eligibility_confidence",
        "ambiguities",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read medical receipts for a personal HSA record-keeping system.

Extract only what you can actually see on the receipt. Rules:
- Return null for any field you cannot read with confidence. Never guess, never
  infer a plausible value, never substitute today's date.
- service_date is the date of service or purchase printed on the receipt. If the
  receipt shows only a transaction date, use that and say so in ambiguities.
- total_amount is what was actually paid, after any insurance adjustment or
  discount. If several totals appear, pick the amount due/paid by the patient and
  note the ambiguity.
- category must be one of the provided values, or null if none clearly fits.
- eligibility_confidence: "certain" if this is unambiguously a qualified medical
  expense, "likely" if it probably is, "review" if it is unclear, non-medical, or
  a mixed-purchase receipt.
- List every unclear or unreadable field in ambiguities, in plain English.

You are extracting data, not giving tax advice."""


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
        watched = ("provider", "service_date", "total_amount", "category", "description")
        return {f for f in watched if not self.data.get(f)}


# --- normalization ---------------------------------------------------------


def _register_heif() -> None:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass


def normalize(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Original bytes -> list of (media_type, bytes) blocks to send to the model.

    Auto-rotates via EXIF, converts HEIC to JPEG, downscales to MAX_EDGE, and
    splits multi-page PDFs into per-page images. Falls back to handing the PDF to
    the model directly when poppler is not installed.
    """
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()

    if ext == "pdf":
        pages = _pdf_to_images(data)
        if pages:
            return pages
        return [("application/pdf", data)]

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
        # ponytail: poppler missing or PDF unreadable -> native PDF path handles it.
        return []
    return [("image/jpeg", _to_jpeg(page)) for page in pages]


# --- extraction ------------------------------------------------------------


def _content_block(media_type: str, payload: bytes) -> dict:
    encoded = base64.standard_b64encode(payload).decode("utf-8")
    block_type = "document" if media_type == "application/pdf" else "image"
    return {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": encoded},
    }


@retry(tries=4)
def _call(client, model: str, blocks: list[dict]):
    return client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": blocks
                + [
                    {
                        "type": "text",
                        "text": (
                            "Extract the receipt data. Every page above belongs to a "
                            "single receipt. Return null for anything you cannot read."
                        ),
                    }
                ],
            }
        ],
    )


def extract(pages: list[tuple[str, bytes]], api_key: str, model: str) -> Extraction:
    """Never raises. On any failure the app falls back to fully manual entry."""
    if not api_key:
        return Extraction(error="no ANTHROPIC_API_KEY configured")
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = _call(client, model, [_content_block(m, b) for m, b in pages])
        if response.stop_reason == "refusal":
            return Extraction(error="model declined to process this image")
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        audit("extraction.ok", model=model, provider=data.get("provider"))
        return Extraction(data=data, raw=text)
    except Exception as exc:  # noqa: BLE001 - graceful degradation is the point
        audit("extraction.failed", model=model, error=str(exc)[:400])
        return Extraction(error=f"{type(exc).__name__}: {exc}"[:400])
