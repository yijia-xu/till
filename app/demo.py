"""Run the controlled store and shopping agent without live PayPal keys."""

from __future__ import annotations

import os
import uuid

from .agent import proposal
from .checkout import apply_webhook, fetch_order, open_db, submit_proposal
from .config import load_settings
from .db import init_db, reset_activity
from .money import from_minor
from .needs import build_needs
from .paypal import PayPalClient


def capture_event(order: dict, *, amount_minor: int | None = None, currency: str | None = None, event_id: str | None = None, event_type: str = "PAYMENT.CAPTURE.COMPLETED") -> dict:
    minor = order["payable_minor"] if amount_minor is None else amount_minor
    code = currency or order["currency"]
    return {
        "id": event_id or "wh_" + uuid.uuid4().hex,
        "event_type": event_type,
        "resource": {
            "id": "CAPDEMO",
            "custom_id": order["id"],
            "amount": {"currency_code": code, "value": from_minor(minor)},
            "supplementary_data": {"related_ids": {"order_id": order.get("paypal_order_id")}},
        },
    }


def run_scenarios(db_path: str, paypal: PayPalClient, needs, settings) -> list[dict]:
    conn = open_db(db_path)
    try:
        honest_body = proposal("honest")
        honest = submit_proposal(
            conn,
            paypal,
            needs,
            honest_body,
            window_seconds=settings.duplicate_window_seconds,
            approval_ttl_seconds=settings.approval_ttl_seconds,
        )
        if honest["decision"] != "ALLOW" or honest["status"] != "CaptureSubmitted":
            raise AssertionError(f"honest order should capture, got {honest['decision']} {honest['status']} {honest['explanation']}")
        if honest["shopper_id"] == "root":
            raise AssertionError("agent user_id must not choose the shopper")
        names = [call[0] for call in paypal.calls]
        if names != ["create", "read", "capture"]:
            raise AssertionError(f"honest PayPal calls should be create, read, capture: {paypal.calls}")
        if paypal.calls[0][1] != honest["id"] or paypal.calls[2][2] != honest["id"]:
            raise AssertionError("custom_id and PayPal-Request-Id must be the decision id")
        apply_webhook(conn, paypal, capture_event(honest), verified=True)
        honest = fetch_order(conn, honest["id"])
        if honest["status"] != "Complete":
            raise AssertionError("honest order should complete only after the webhook matches")
        results = [
            {
                "scenario": "honest",
                "decision": honest["decision"],
                "status": honest["status"],
                "malicious": bool(honest["malicious"]),
                "problem": honest["problem"],
            }
        ]
        expected = {
            "price": ("tamper", True),
            "swap": ("tamper", True),
            "quantity": ("tamper", True),
            "duplicate": ("duplicate", True),
        }
        for name, (problem, malicious) in expected.items():
            before = len(paypal.calls)
            order = submit_proposal(
                conn,
                paypal,
                needs,
                proposal(name),
                window_seconds=settings.duplicate_window_seconds,
                approval_ttl_seconds=settings.approval_ttl_seconds,
            )
            if order["decision"] != "BLOCK" or order["status"] != "Blocked":
                raise AssertionError(f"{name} should block, got {order['decision']} {order['status']} {order['explanation']}")
            if bool(order["malicious"]) is not malicious or order["problem"] != problem:
                raise AssertionError(f"{name} should be malicious={malicious} problem={problem}, got {order['malicious']} {order['problem']}")
            if len(paypal.calls) != before:
                raise AssertionError(f"{name} must not call PayPal")
            results.append(
                {
                    "scenario": name,
                    "decision": order["decision"],
                    "status": order["status"],
                    "malicious": bool(order["malicious"]),
                    "problem": order["problem"],
                }
            )
        return results
    finally:
        conn.close()


def main() -> None:
    path = os.environ.get("TILL_DB", "till.db")
    settings = load_settings()
    init_db(path)
    reset_activity(path, settings.intent_ttl_seconds)
    if not settings.needs_model_api_key:
        print("Set NEEDS_MODEL_API_KEY. Needs judgment is an LLM call, not a string compare.")
        raise SystemExit(1)
    paypal = PayPalClient("simulate", settings.paypal_base, "", "", "")
    needs = build_needs(settings.needs_model_api_key, settings.needs_model_url, settings.needs_model_name)
    try:
        rows = run_scenarios(path, paypal, needs, settings)
    except AssertionError as exc:
        print(exc)
        raise SystemExit(1) from exc
    print("Till demo (fake PayPal, no live charge)")
    for row in rows:
        kind = "malicious" if row["malicious"] else "policy" if row["decision"] == "BLOCK" else "allowed"
        print(f"{row['scenario']:12} {row['decision']:14} {row['status']:18} {kind}")


if __name__ == "__main__":
    main()
