from app.engine import apply_model, approval_hash, evaluate
from app.money import from_minor, to_minor
from app.paypal import certificate_url_allowed


def test_money_units():
    assert to_minor("74.00") == 7400
    assert to_minor("74.5") == 7450
    assert from_minor(7400) == "74.00"
    assert from_minor(-900) == "-9.00"


def test_model_cannot_loosen_a_block():
    verdict = evaluate(
        policy=_policy(),
        claimed=_claim(unit_price_minor=19900, total_minor=19900),
        listed=_listed(),
        spent_today_minor=0,
        duplicate=False,
    )
    assert verdict.decision == "BLOCK"
    assert verdict.malicious is True
    tightened = apply_model(verdict, "ALLOW")
    assert tightened.decision == "BLOCK"


def test_model_may_tighten_an_allow():
    verdict = evaluate(
        policy=_policy(),
        claimed=_claim(),
        listed=_listed(),
        spent_today_minor=0,
        duplicate=False,
    )
    assert verdict.decision == "ALLOW"
    tightened = apply_model(verdict, "BLOCK")
    assert tightened.decision == "BLOCK"
    assert tightened.malicious is False


def test_certificate_url_must_be_paypal():
    assert certificate_url_allowed("https://api-m.sandbox.paypal.com/v1/notifications/certs/abc")
    assert certificate_url_allowed("https://api.sandbox.paypal.com/certs/abc")
    assert not certificate_url_allowed("https://evil.example/certs/abc")
    assert not certificate_url_allowed("http://api-m.sandbox.paypal.com/certs/abc")
    assert not certificate_url_allowed("https://user:pass@api.paypal.com/certs/abc")


def test_approval_hash_changes_when_any_bound_field_changes():
    base = dict(amount=7400, sku="SHOE-PEG-9", quantity=1, currency="USD", address="100 Demo Ave")
    original = approval_hash(**base)
    for change in (
        {"amount": 7401},
        {"sku": "OTHER"},
        {"quantity": 2},
        {"currency": "EUR"},
        {"address": "9 Other St"},
    ):
        mutated = dict(base)
        mutated.update(change)
        assert approval_hash(**mutated) != original


def _policy():
    return {
        "raw": "Running shoes, size 9, at most $80, no subscription.",
        "category": "running_shoes",
        "size": "9",
        "max_purchase_minor": 8000,
        "daily_max_minor": 20000,
        "category_max_minor": 20000,
        "ask_above_minor": 8000,
        "currency": "USD",
        "no_subscription": True,
        "include_shipping": True,
        "exclusions": ["subscription"],
        "allowed_merchants": [],
        "intent_quantity": 1,
    }


def _listed():
    return {
        "sku": "SHOE-PEG-9",
        "merchant_id": "merchant_footlocker",
        "title": "Nike Pegasus running shoes",
        "category": "running_shoes",
        "size": "9",
        "brand": "Nike",
        "unit_price_minor": 7400,
        "currency": "USD",
        "tax_minor": 0,
        "shipping_minor": 0,
        "addons_minor": 0,
        "subscription": 0,
    }


def _claim(**overrides):
    body = {
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
    }
    body.update(overrides)
    return body
