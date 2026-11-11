import json as jsonlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class _ModelResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _fake_gemini_post(url, headers=None, json=None, timeout=None):
    """Stand in for Gemini. Tests never call a live model."""
    prompt = jsonlib.loads(json["contents"][0]["parts"][0]["text"])
    listing = prompt.get("listing") or {}
    asked = str(prompt.get("category") or "").strip().lower().replace(" ", "_")
    found = str(listing.get("category") or "").strip().lower().replace(" ", "_")
    title = str(listing.get("title") or "").lower().replace(" ", "_")
    if listing and asked and found and asked != found and asked not in title:
        judgment = "unfit"
        explanation = f"The shopper asked for {asked.replace('_', ' ')}. The listing is {found.replace('_', ' ')}."
    elif prompt.get("size") and not listing.get("size"):
        judgment = "unsure"
        explanation = f"The shopper asked for size {prompt['size']}, and the listing has no size."
    elif not asked:
        judgment = "unsure"
        explanation = "The request does not name a category Till can freeze."
    elif listing and not found:
        judgment = "unsure"
        explanation = "The listing has no category, so the needs check waits."
    else:
        judgment = "fit"
        explanation = "The listing matches the shopper's request."
    return _ModelResponse(
        {"candidates": [{"content": {"parts": [{"text": jsonlib.dumps({"judgment": judgment, "explanation": explanation})}]}}]}
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        paypal_mode="simulate",
        paypal_client_id="",
        paypal_client_secret="",
        paypal_webhook_id="wh_test",
        paypal_base="https://api-m.sandbox.paypal.com",
        public_base_url="http://127.0.0.1:8000",
        db_path=str(tmp_path / "till.db"),
        agent_token="test-agent",
        simulator_token="test-sim",
        duplicate_window_seconds=86400,
        approval_ttl_seconds=900,
        intent_ttl_seconds=604800,
        needs_model_api_key="test-key",
        needs_model_url="https://generativelanguage.googleapis.com/v1beta/models",
        needs_model_name="gemini-3.5-flash-lite",
    )


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setattr("app.gemini.httpx.post", _fake_gemini_post)
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def agent_headers():
    return {"Authorization": "Bearer test-agent"}
