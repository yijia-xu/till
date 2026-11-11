"""Runtime settings. PayPal secrets stay here, never on the agent."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    paypal_mode: str
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    paypal_base: str
    public_base_url: str
    db_path: str
    agent_token: str
    simulator_token: str
    duplicate_window_seconds: int
    approval_ttl_seconds: int
    intent_ttl_seconds: int
    needs_model_api_key: str
    needs_model_url: str
    needs_model_name: str


def load_settings() -> Settings:
    def text(name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()

    def whole(name: str, default: int) -> int:
        raw = text(name, str(default))
        return int(raw)

    return Settings(
        paypal_mode=text("PAYPAL_MODE", "simulate").lower(),
        paypal_client_id=text("PAYPAL_CLIENT_ID"),
        paypal_client_secret=text("PAYPAL_CLIENT_SECRET"),
        paypal_webhook_id=text("PAYPAL_WEBHOOK_ID"),
        paypal_base=text("PAYPAL_BASE", "https://api-m.sandbox.paypal.com"),
        public_base_url=text("TILL_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        db_path=text("TILL_DB", "till.db"),
        agent_token=text("TILL_AGENT_TOKEN", "demo-agent-token"),
        simulator_token=text("TILL_SIMULATOR_TOKEN", "demo-simulator-token"),
        duplicate_window_seconds=whole("TILL_DUPLICATE_WINDOW_SECONDS", 86400),
        approval_ttl_seconds=whole("TILL_APPROVAL_TTL_SECONDS", 900),
        intent_ttl_seconds=whole("TILL_INTENT_TTL_SECONDS", 604800),
        needs_model_api_key=text("NEEDS_MODEL_API_KEY"),
        needs_model_url=text("NEEDS_MODEL_URL"),
        needs_model_name=text("NEEDS_MODEL_NAME", "gpt-4o-mini"),
    )
