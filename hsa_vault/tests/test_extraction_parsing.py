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


# --- fake openai SDK pointed at NVIDIA -------------------------------------


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, payload):
        text = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
        self.choices = [FakeChoice(text)]


@pytest.fixture
def fake_openai(monkeypatch):
    """Installs a stub `openai` module and returns a dict to configure it."""
    state = {"response": None, "raise": None, "calls": [], "init": []}

    class FakeCompletions:
        def create(self, **kwargs):
            state["calls"].append(kwargs)
            if state["raise"]:
                raise state["raise"]
            return state["response"]

    class FakeClient:
        def __init__(self, api_key=None, base_url=None, **kwargs):
            state["init"].append({"api_key": api_key, "base_url": base_url, **kwargs})
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    module = types.ModuleType("openai")
    module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", module)
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

MODEL = "meta/llama-3.2-90b-vision-instruct"


# --- happy path ------------------------------------------------------------


def test_parses_a_well_formed_response(fake_openai):
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)
    result = extraction.extract([("image/jpeg", b"fake")], "nvapi-x", MODEL)

    assert result.error == ""
    assert result.data["provider"] == "CVS Pharmacy"
    assert result.data["total_amount"] == "42.18"
    assert result.uncertain_fields == set()
    assert result.ambiguities == []


def test_client_targets_the_nvidia_endpoint(fake_openai):
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("image/jpeg", b"fake")], "nvapi-x", MODEL)
    assert fake_openai["init"][0]["base_url"] == extraction.DEFAULT_BASE_URL
    assert fake_openai["init"][0]["api_key"] == "nvapi-x"


def test_custom_base_url_is_honoured(fake_openai):
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL, "https://self.hosted/v1")
    assert fake_openai["init"][0]["base_url"] == "https://self.hosted/v1"


def test_request_sends_the_image_as_a_data_uri(fake_openai):
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("image/jpeg", b"fake")], "nvapi-x", MODEL)

    call = fake_openai["calls"][0]
    assert call["model"] == MODEL
    assert call["temperature"] == 0.0
    blocks = call["messages"][1]["content"]
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_multiple_pages_are_sent_as_one_receipt(fake_openai):
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)
    extraction.extract([("image/jpeg", b"p1"), ("image/jpeg", b"p2")], "nvapi-x", MODEL)
    blocks = fake_openai["calls"][0]["messages"][1]["content"]
    assert sum(1 for b in blocks if b["type"] == "image_url") == 2
    assert len(fake_openai["calls"]) == 1


# --- tolerant parsing (NIM models are looser than a schema-enforced API) ----


def test_json_wrapped_in_markdown_fences_is_parsed(fake_openai):
    fake_openai["response"] = FakeResponse(f"```json\n{json.dumps(GOOD_PAYLOAD)}\n```")
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["provider"] == "CVS Pharmacy"


def test_json_with_a_chatty_preamble_is_parsed(fake_openai):
    fake_openai["response"] = FakeResponse(
        "Sure! Here is the receipt data:\n" + json.dumps(GOOD_PAYLOAD) + "\nHope that helps."
    )
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["total_amount"] == "42.18"


def test_currency_symbols_are_stripped_from_the_amount(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, total_amount="$1,042.18"))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["total_amount"] == "1042.18"


def test_an_invented_category_is_discarded(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, category="Pharmacy Stuff"))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["category"] is None
    assert "category" in result.uncertain_fields


def test_an_invented_confidence_is_discarded(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, eligibility_confidence="very sure"))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["eligibility_confidence"] is None


def test_wrong_typed_collections_are_coerced(fake_openai):
    fake_openai["response"] = FakeResponse(
        dict(GOOD_PAYLOAD, line_items="none visible", ambiguities=None)
    )
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["line_items"] == []
    assert result.data["ambiguities"] == []


def test_blank_strings_count_as_unread(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, provider="   "))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["provider"] is None
    assert "provider" in result.uncertain_fields


# --- nulls and ambiguity ---------------------------------------------------


def test_nulls_are_reported_as_uncertain_fields(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, provider=None, total_amount=None))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.uncertain_fields == {"provider", "total_amount"}


def test_ambiguities_are_surfaced(fake_openai):
    fake_openai["response"] = FakeResponse(
        dict(GOOD_PAYLOAD, ambiguities=["Two totals printed; used the patient-due one"])
    )
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert len(result.ambiguities) == 1


def test_non_medical_flag_is_preserved(fake_openai):
    fake_openai["response"] = FakeResponse(dict(GOOD_PAYLOAD, is_medical_expense=False))
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data["is_medical_expense"] is False


# --- graceful degradation --------------------------------------------------


def test_missing_api_key_degrades_instead_of_raising():
    result = extraction.extract([("image/jpeg", b"x")], "", MODEL)
    assert result.data == {}
    assert "NVIDIA_API_KEY" in result.error
    assert result.ambiguities  # the reason is surfaced to the UI


def test_unrasterizable_pdf_degrades_instead_of_raising():
    result = extraction.extract([], "nvapi-x", MODEL)
    assert result.data == {}
    assert "image" in result.error


def test_api_failure_degrades_instead_of_raising(monkeypatch, fake_openai):
    monkeypatch.setattr(
        extraction, "_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 overloaded"))
    )
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data == {}
    assert "503 overloaded" in result.error


def test_unparseable_reply_degrades_instead_of_raising(fake_openai):
    fake_openai["response"] = FakeResponse("I'm sorry, I can't read that receipt.")
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data == {}
    assert "no JSON object" in result.error


def test_empty_reply_degrades_instead_of_raising(fake_openai):
    fake_openai["response"] = FakeResponse("")
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data == {}
    assert "empty" in result.error


def test_a_json_array_is_rejected_not_returned(fake_openai):
    fake_openai["response"] = FakeResponse([1, 2, 3])
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.data == {}


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


def test_the_model_call_is_bounded_in_time_and_attempts(fake_openai):
    """An unbounded extraction call is a hang, not a slow feature.

    The openai SDK defaults to a 600s timeout and its own max_retries=2. Combined
    with the retry decorator that wraps _call, one upload could cost a dozen
    requests of up to ten minutes each — measured at over six minutes on a single
    150KB image before being killed by hand. Extraction is optional (manual entry
    always works), so it must fail fast rather than park a spinner.
    """
    fake_openai["response"] = FakeResponse(GOOD_PAYLOAD)

    extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)

    init = fake_openai["init"][0]
    assert init.get("timeout"), "no per-request timeout: the SDK default is 600s"
    assert init["timeout"] <= 120, "a timeout this long is indistinguishable from a hang"
    assert init.get("max_retries") == 0, (
        "the SDK's own retries multiply with retry(), they do not replace it"
    )


def test_a_hanging_endpoint_gives_up_instead_of_retrying_forever(fake_openai):
    """Worst case must stay bounded: attempts x timeout, not attempts x 600s."""
    fake_openai["raise"] = TimeoutError("request timed out")

    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)

    assert result.error, "a timeout must surface, not raise"
    assert len(fake_openai["calls"]) == extraction.ATTEMPTS, (
        f"expected {extraction.ATTEMPTS} attempts, got {len(fake_openai['calls'])}"
    )
    assert extraction.ATTEMPTS * extraction.REQUEST_TIMEOUT <= 180, (
        "worst-case wait before manual entry is longer than anyone will sit through"
    )


def test_a_one_image_model_still_reads_a_multi_page_receipt(fake_openai):
    """Some NIM models cap at one image and 400 the rest.

    Verified against meta/llama-3.2-11b-vision-instruct, which is the only model
    that responded at all in benchmarking:
        400 - "At most 1 image(s) may be provided in one prompt"
    Without a fallback, ticking "these are one receipt" would fail outright on
    exactly the model that works.
    """
    calls = {"n": 0}

    class Refusing:
        def create(self, **kwargs):
            blocks = kwargs["messages"][1]["content"]
            images = [b for b in blocks if b["type"] == "image_url"]
            if len(images) > 1:
                raise ValueError(
                    "Error code: 400 - {'message': 'At most 1 image(s) may be "
                    "provided in one prompt.'}"
                )
            calls["n"] += 1
            payload = dict(GOOD_PAYLOAD, total_amount=None) if calls["n"] == 1 else dict(
                GOOD_PAYLOAD, provider=None, total_amount="275.00"
            )
            return FakeResponse(payload)

    fake_openai_client = sys.modules["openai"].OpenAI

    class Client(fake_openai_client):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.chat = types.SimpleNamespace(completions=Refusing())

    sys.modules["openai"].OpenAI = Client

    result = extraction.extract(
        [("image/jpeg", b"p1"), ("image/jpeg", b"p2")], "nvapi-x", MODEL
    )

    assert not result.error, f"the fallback did not engage: {result.error}"
    assert result.data["provider"] == "CVS Pharmacy", "page 1 identity fields were lost"
    assert result.data["total_amount"] == "275.00", (
        "the total is printed on the LAST page; taking the first non-null loses it"
    )
    assert any("one image at a time" in a for a in result.ambiguities), (
        "reading pages separately is a caveat the user must see"
    )


def test_a_single_page_failure_is_not_retried_page_by_page(fake_openai):
    """The fallback is only for the multi-image refusal, not for every 400."""
    fake_openai["raise"] = ValueError("Error code: 400 - {'message': 'bad request'}")
    result = extraction.extract([("image/jpeg", b"x")], "nvapi-x", MODEL)
    assert result.error, "a genuine bad request must still surface"
