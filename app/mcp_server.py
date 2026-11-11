"""MCP tools for shopping agents to propose and inspect Till decisions.

The MCP process is intentionally a thin client of Till's authenticated API.
It does not contain PayPal credentials and exposes no approval/capture tools.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


load_dotenv()


SERVER_NAME = "Till payment safety"
DEFAULT_API_URL = "http://127.0.0.1:8000"

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Use submit_order_proposal to ask Till to evaluate a proposed purchase against the shopper's "
        "intent and spending rules. Use get_order_decision to retrieve its current decision. Till alone "
        "controls PayPal; these tools cannot approve, capture, or void a payment."
    ),
)


def _api_settings() -> tuple[str, str]:
    base_url = os.environ.get("TILL_MCP_API_URL", DEFAULT_API_URL).strip().rstrip("/")
    token = os.environ.get("TILL_AGENT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set TILL_AGENT_TOKEN for the MCP server process.")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("TILL_MCP_API_URL must start with http:// or https://.")
    return base_url, token


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url, token = _api_settings()
    try:
        response = httpx.request(
            method,
            f"{base_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Till API at {base_url}: {exc}") from exc

    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Till API returned HTTP {response.status_code}: {detail}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Till API returned an invalid JSON response.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Till API returned an unexpected response.")
    return result


@mcp.tool()
def submit_order_proposal(
    idempotency_key: str,
    merchant_id: str,
    sku: str,
    title: str,
    quantity: int,
    unit_price_minor: int,
    total_minor: int,
    currency: str,
    address: str,
    tax_minor: int = 0,
    shipping_minor: int = 0,
    addons_minor: int = 0,
) -> dict[str, Any]:
    """Submit a proposed purchase for Till's policy and integrity checks.

    All monetary amounts use the currency's minor unit (for example, 7400 means
    USD 74.00). Till verifies the proposal against its own catalog and rules.
    Submitting a proposal does not authorize a PayPal payment.
    """
    body = {
        "idempotency_key": idempotency_key,
        "merchant_id": merchant_id,
        "sku": sku,
        "title": title,
        "quantity": quantity,
        "unit_price_minor": unit_price_minor,
        "tax_minor": tax_minor,
        "shipping_minor": shipping_minor,
        "addons_minor": addons_minor,
        "total_minor": total_minor,
        "currency": currency.upper(),
        "address": address,
    }
    return _request("POST", "/api/orders", body)


@mcp.tool()
def get_order_decision(order_id: str) -> dict[str, Any]:
    """Read the current Till decision and status for a previously submitted proposal."""
    return _request("GET", f"/api/orders/{quote(order_id, safe='')}")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
