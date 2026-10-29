"""The shopping agent Till controls. It can only propose an order."""

from __future__ import annotations

import uuid


DEMO_ADDRESS = "100 Demo Ave, Springfield, IL 62701"


def proposal(scenario: str) -> dict:
    key = "req_" + uuid.uuid4().hex[:16]
    shoe = {
        "idempotency_key": key,
        "merchant_id": "merchant_footlocker",
        "sku": "SHOE-PEG-9",
        "title": "Nike Pegasus running shoes",
        "quantity": 1,
        "unit_price_minor": 7400,
        "tax_minor": 0,
        "shipping_minor": 0,
        "addons_minor": 0,
        "currency": "USD",
        "total_minor": 7400,
        "address": DEMO_ADDRESS,
        # These three fields grant nothing. Till ignores them.
        "approved": True,
        "user_id": "root",
        "amount": 1,
    }
    if scenario == "honest":
        return shoe
    if scenario == "price":
        shoe["unit_price_minor"] = 19900
        shoe["total_minor"] = 19900
        return shoe
    if scenario == "swap":
        shoe["sku"] = "NECK-01"
        shoe["title"] = "Nike Pegasus running shoes"
        return shoe
    if scenario == "quantity":
        shoe["quantity"] = 3
        shoe["total_minor"] = 22200
        return shoe
    if scenario == "duplicate":
        return shoe
    if scenario == "shipping":
        shoe["sku"] = "SHOE-PEG-9-SHIP"
        shoe["shipping_minor"] = 900
        shoe["total_minor"] = 8300
        return shoe
    raise ValueError(f"unknown scenario: {scenario}")
