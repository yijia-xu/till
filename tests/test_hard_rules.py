from app.agent import proposal
from app.db import connect
from app.needs import NeedsUnavailable

from tests.helpers import login, order_row, set_caps, submit


def test_budget_under_cap_allows_and_over_cap_blocks(client, app, agent_headers):
    set_caps(client, purchase="50.00", daily="200.00", category="200.00", ask="50.00")
    blocked = submit(client, agent_headers, proposal("honest") | {"approved": True, "user_id": "root", "amount": 1}).json()
    assert blocked["decision"] == "BLOCK"
    assert blocked["problem"] == "budget"
    assert blocked["malicious"] is False
    assert order_row(app, blocked["id"])["shopper_id"] != "root"

    set_caps(client, purchase="80.00", daily="200.00", category="200.00", ask="80.00")
    allowed = submit(client, agent_headers).json()
    assert allowed["decision"] == "ALLOW"
    assert allowed["malicious"] is False
    assert allowed["payable_minor"] == 7400
    assert any(call[0] == "capture" for call in app.state.paypal.calls)


def test_daily_and_category_caps(client, app, agent_headers):
    conn = connect(app.state.settings.db_path)
    conn.execute(
        """
        INSERT INTO catalog (
          sku, merchant_id, merchant_name, title, category, size, brand,
          unit_price_minor, currency, tax_minor, shipping_minor, addons_minor, subscription
        ) VALUES ('SHOE-ALT', 'merchant_footlocker', 'Foot Locker', 'Nike Pegasus running shoes',
                  'running_shoes', '9', 'Nike', 7400, 'USD', 0, 0, 0, 0)
        """
    )
    conn.close()
    set_caps(client, purchase="80.00", daily="100.00", category="200.00", ask="80.00")
    first = submit(client, agent_headers).json()
    assert first["decision"] == "ALLOW"
    second = submit(client, agent_headers, proposal("honest") | {"sku": "SHOE-ALT", "idempotency_key": "alt-day"}).json()
    assert second["decision"] == "BLOCK"
    assert second["problem"] == "budget"
    assert any(reason["code"] == "over_daily" for reason in second["reasons"])

    set_caps(client, purchase="80.00", daily="200.00", category="100.00", ask="80.00")
    third = submit(client, agent_headers, proposal("honest") | {"sku": "SHOE-ALT", "idempotency_key": "alt-cat"}).json()
    assert third["decision"] == "BLOCK"
    assert any(reason["code"] == "over_category" for reason in third["reasons"])


def test_needs_match_blocks_and_uncertain(client, app, agent_headers):
    matched = submit(client, agent_headers).json()
    assert matched["decision"] == "ALLOW"

    necklace = proposal("honest") | {"sku": "NECK-01", "title": "Gold necklace"}
    blocked = submit(client, agent_headers, necklace).json()
    assert blocked["decision"] == "BLOCK"
    assert blocked["problem"] == "needs"
    assert blocked["malicious"] is False

    conn = connect(app.state.settings.db_path)
    conn.execute(
        """
        INSERT INTO catalog (
          sku, merchant_id, merchant_name, title, category, size, brand,
          unit_price_minor, currency, tax_minor, shipping_minor, addons_minor, subscription
        ) VALUES ('SHOE-NOSIZE', 'merchant_footlocker', 'Foot Locker', 'Nike Pegasus running shoes',
                  'running_shoes', NULL, 'Nike', 7400, 'USD', 0, 0, 0, 0)
        """
    )
    conn.close()
    unsure = submit(
        client,
        agent_headers,
        proposal("honest") | {"sku": "SHOE-NOSIZE", "idempotency_key": "nosize"},
    ).json()
    assert unsure["decision"] == "NEED_APPROVAL"
    assert unsure["problem"] == "needs"
    assert app.state.paypal.calls == [] or all(call[1] != unsure["id"] for call in app.state.paypal.calls if call[0] == "create")


def test_tamper_price_swap_and_quantity(client, app, agent_headers):
    before = len(app.state.paypal.calls)
    price = submit(client, agent_headers, proposal("price")).json()
    swap = submit(client, agent_headers, proposal("swap")).json()
    quantity = submit(client, agent_headers, proposal("quantity")).json()
    assert price["decision"] == "BLOCK" and price["malicious"] and price["problem"] == "tamper"
    assert any(reason["code"] == "price_mismatch" for reason in price["reasons"])
    assert swap["decision"] == "BLOCK" and swap["malicious"] and swap["problem"] == "tamper"
    assert quantity["decision"] == "BLOCK" and quantity["malicious"]
    assert any(reason["code"] == "quantity_mismatch" for reason in quantity["reasons"])
    assert len(app.state.paypal.calls) == before


def test_arithmetic_must_balance(client, agent_headers):
    body = proposal("honest") | {"total_minor": 7401}
    blocked = submit(client, agent_headers, body).json()
    assert blocked["decision"] == "BLOCK"
    assert blocked["problem"] == "arithmetic"
    assert blocked["malicious"] is False


def test_duplicate_window_and_outside_window(client, app, agent_headers):
    first = submit(client, agent_headers).json()
    assert first["decision"] == "ALLOW"
    again = submit(client, agent_headers, proposal("duplicate")).json()
    assert again["decision"] == "BLOCK"
    assert again["problem"] == "duplicate"
    assert again["malicious"] is True

    conn = connect(app.state.settings.db_path)
    conn.execute(
        "UPDATE orders SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (first["id"],),
    )
    conn.close()
    later = submit(client, agent_headers, proposal("duplicate")).json()
    assert later["decision"] == "ALLOW"


def test_paypal_readback_change_voids(client, app, agent_headers):
    app.state.paypal.tamper_read_value = "10.00"
    body = submit(client, agent_headers).json()
    assert body["decision"] == "BLOCK"
    assert body["malicious"] is True
    assert body["problem"] == "tamper"
    assert body["success"] is False
    kinds = [call[0] for call in app.state.paypal.calls]
    assert "void" in kinds
    assert "capture" not in kinds
    assert app.state.paypal._orders[body["paypal_order_id"]]["status"] == "VOIDED"


def test_model_fit_cannot_override_a_hard_block_and_outage_waits(client, app, agent_headers):
    class FitAll:
        def judge(self, policy, claimed, listed):
            return "fit", "looks fine"

    class Down:
        def judge(self, policy, claimed, listed):
            raise NeedsUnavailable("down")

    set_caps(client, purchase="50.00")
    app.state.needs = FitAll()
    blocked = submit(client, agent_headers).json()
    assert blocked["decision"] == "BLOCK"
    assert "hard rule still blocks" in blocked["explanation"]

    set_caps(client, purchase="80.00")
    app.state.needs = Down()
    waiting = submit(client, agent_headers).json()
    assert waiting["decision"] == "NEED_APPROVAL"
    assert waiting["problem"] == "needs"
    app.state.needs = Down()
    set_caps(client, purchase="10.00")
    still_blocked = submit(client, agent_headers).json()
    assert still_blocked["decision"] == "BLOCK"


def test_captured_paypal_order_cannot_be_paid_again(client, app, agent_headers):
    from app.checkout import start_payment
    from app.db import connect

    first = submit(client, agent_headers).json()
    captures = [call for call in app.state.paypal.calls if call[0] == "capture"]
    conn = connect(app.state.settings.db_path)
    conn.execute(
        """
        INSERT INTO catalog (
          sku, merchant_id, merchant_name, title, category, size, brand,
          unit_price_minor, currency, tax_minor, shipping_minor, addons_minor, subscription
        ) VALUES ('SHOE-ALT', 'merchant_footlocker', 'Foot Locker', 'Nike Pegasus running shoes',
                  'running_shoes', '9', 'Nike', 7400, 'USD', 0, 0, 0, 0)
        """
    )
    conn.close()
    second = submit(client, agent_headers, proposal("honest") | {"sku": "SHOE-ALT", "idempotency_key": "alt-pay"}).json()
    captures = [call for call in app.state.paypal.calls if call[0] == "capture"]
    conn = connect(app.state.settings.db_path)
    conn.execute(
        "UPDATE orders SET status = 'OrderCreated', paypal_order_id = ? WHERE id = ?",
        (first["paypal_order_id"], second["id"]),
    )
    conn.close()
    conn = connect(app.state.settings.db_path)
    try:
        replayed = start_payment(conn, app.state.paypal, second["id"])
    finally:
        conn.close()
    assert replayed["decision"] == "BLOCK"
    assert replayed["problem"] == "duplicate"
    assert replayed["malicious"] == 1
    assert [call for call in app.state.paypal.calls if call[0] == "capture"] == captures


def test_agent_has_no_paypal_credentials():
    import inspect

    import app.agent as agent

    source = inspect.getsource(agent)
    assert "paypal" not in source.lower()
    assert "PAYPAL" not in source
