import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        paypal_mode="simulate",
        paypal_client_id="",
        paypal_client_secret="",
        paypal_webhook_id="wh_test",
        paypal_base="https://api-m.sandbox.paypal.com",
        db_path=str(tmp_path / "till.db"),
        agent_token="test-agent",
        simulator_token="test-sim",
        duplicate_window_seconds=86400,
        approval_ttl_seconds=900,
        intent_ttl_seconds=604800,
        needs_model_api_key="",
        needs_model_url="",
        needs_model_name="gpt-4o-mini",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def agent_headers():
    return {"Authorization": "Bearer test-agent"}
