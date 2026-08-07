"""Extraction is mocked end to end — no network, no API key, no cost.

The contract under test: extract() never raises, and a failure still produces a
usable Extraction so the app can fall back to manual entry.
"""

import io
import json
import sys
import types

import pytest

from core import extraction


# --- fake anthropic SDK ----------------------------------------------------


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, payload, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [FakeBlock(json.dumps(payload) if isinstance(payload, dict) else payload)]


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Installs a stub `anthropic` module and returns a dict to configure it."""
    state = {"response": None, "raise": None, "calls": []}

    class FakeMessages:
        def create(self, **kwargs):
            state["calls"].append(kwargs)
            if state["raise"]:
                raise state["raise"]
            return state["response"]

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return state


GOOD_PAYLOAD = {
    "provider": "CVS Pharmacy",
    "service_date": "2026-03-14",
    "total_amount": "42.18",
    "line_items": [{"description": "Amoxicillin 500mg", "amount": "42.18"}],
    "category": "Prescription",
    "description": "Antibiotic prescription",
    "patient_name_if_visible": "Jane Doe",
    "is_medical_expense": True,
    "eligibility_confidence": "certain",
    "ambiguities": [],
}


# --- happy path ------------------------------------------------------------


def test_parses_a_well_formed_response(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(GOOD_PAYLOAD)
    result = extraction.extract([("image/jpeg", b"fake")], "key", "claude-opus-5")

    assert result.error == ""
    assert result.data["provider"] == "CVS Pharmacy"
    assert result.data["total_amount"] == "42.18"
    assert result.uncertain_fields == set()
    assert result.ambiguities == []


def test_request_carries_the_schema_and_the_image(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("image/jpeg", b"fake")], "key", "claude-opus-5")

    call = fake_anthropic["calls"][0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"]["format"]["schema"] is extraction.EXTRACTION_SCHEMA
    blocks = call["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"


def test_pdf_pages_are_sent_as_document_blocks(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("application/pdf", b"%PDF-1.4")], "key", "claude-opus-5")
    assert fake_anthropic["calls"][0]["messages"][0]["content"][0]["type"] == "document"


def test_multiple_pages_are_sent_as_one_receipt(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract(
        [("image/jpeg", b"page1"), ("image/jpeg", b"page2")], "key", "claude-opus-5"
    )
    blocks = fake_anthropic["calls"][0]["messages"][0]["content"]
    assert sum(1 for b in blocks if b["type"] == "image") == 2
    assert len(fake_anthropic["calls"]) == 1


# --- nulls and ambiguity ---------------------------------------------------


def test_nulls_are_reported_as_uncertain_fields(fake_anthropic):
    payload = dict(GOOD_PAYLOAD, provider=None, total_amount=None)
    fake_anthropic["response"] = FakeResponse(payload)
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert result.uncertain_fields == {"provider", "total_amount"}


def test_ambiguities_are_surfaced(fake_anthropic):
    payload = dict(GOOD_PAYLOAD, ambiguities=["Two totals printed; used the patient-due one"])
    fake_anthropic["response"] = FakeResponse(payload)
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert len(result.ambiguities) == 1


def test_non_medical_flag_is_preserved(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(dict(GOOD_PAYLOAD, is_medical_expense=False))
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert result.data["is_medical_expense"] is False


# --- graceful degradation --------------------------------------------------


def test_missing_api_key_degrades_instead_of_raising():
    result = extraction.extract([("image/jpeg", b"x")], "", "claude-opus-5")
    assert result.data == {}
    assert "ANTHROPIC_API_KEY" in result.error
    assert result.ambiguities  # the reason is surfaced to the UI


def test_api_failure_degrades_instead_of_raising(monkeypatch, fake_anthropic):
    monkeypatch.setattr(
        extraction, "_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 overloaded"))
    )
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert result.data == {}
    assert "503 overloaded" in result.error


def test_malformed_json_degrades_instead_of_raising(fake_anthropic):
    fake_anthropic["response"] = FakeResponse("{not valid json")
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert result.data == {}
    assert "JSONDecodeError" in result.error


def test_refusal_is_reported_not_raised(fake_anthropic):
    fake_anthropic["response"] = FakeResponse(GOOD_PAYLOAD, stop_reason="refusal")
    result = extraction.extract([("image/jpeg", b"x")], "key", "claude-opus-5")
    assert result.data == {}
    assert "declined" in result.error


# --- normalization ---------------------------------------------------------


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_large_images_are_downscaled_for_the_model():
    from PIL import Image

    pages = extraction.normalize(_png(4000, 3000), "big.png")
    assert len(pages) == 1
    media_type, data = pages[0]
    assert media_type == "image/jpeg"
    assert max(Image.open(io.BytesIO(data)).size) == extraction.MAX_EDGE


def test_small_images_are_not_upscaled():
    from PIL import Image

    _, data = extraction.normalize(_png(800, 600), "small.png")[0]
    assert Image.open(io.BytesIO(data)).size == (800, 600)


def test_normalizing_never_touches_the_caller_bytes():
    """The original bytes are what goes to Drive — normalization must not mutate them."""
    original = _png(1200, 900)
    snapshot = bytes(original)
    extraction.normalize(original, "receipt.png")
    assert original == snapshot


def test_unreadable_file_still_produces_a_block():
    pages = extraction.normalize(b"not an image at all", "weird.jpg")
    assert len(pages) == 1
