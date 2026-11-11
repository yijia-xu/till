import base64
import json
import zlib
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.agent import proposal
from app.demo import capture_event

from tests.helpers import submit


def test_matching_webhook_completes_and_mismatch_or_missing_decision_does_not(client, app, agent_headers):
    order = submit(client, agent_headers).json()
    assert order["status"] == "CaptureSubmitted"
    assert order["success"] is False

    noted = client.post(
        "/webhooks/simulator",
        json={
            "id": "wh-approved",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {"id": order["paypal_order_id"], "purchase_units": [{"custom_id": order["id"]}]},
        },
        headers={"X-Till-Simulator-Token": "test-sim"},
    )
    assert noted.status_code == 200
    assert client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()["status"] == "CaptureSubmitted"

    lone = {
        "id": "wh-order-id-only",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-ALONE",
            "amount": {"currency_code": "USD", "value": "74.00"},
            "supplementary_data": {"related_ids": {"order_id": order["paypal_order_id"]}},
        },
    }
    alone = client.post("/webhooks/simulator", json=lone, headers={"X-Till-Simulator-Token": "test-sim"})
    assert alone.status_code == 200
    assert client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()["success"] is False

    bad = capture_event(order_row(app, order["id"]), amount_minor=100, event_id="wh-mismatch")
    mismatch = client.post("/webhooks/simulator", json=bad, headers={"X-Till-Simulator-Token": "test-sim"})
    assert mismatch.json()["status"] == "incident"
    current = client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()
    assert current["status"] == "Incident"
    assert current["success"] is False
    assert current["malicious"] is True
    assert current["problem"] == "webhook"
    assert any(call[0] == "void" for call in app.state.paypal.calls)

    denied_order = submit(client, agent_headers, proposal("honest") | {"sku": "SHOE-PEG-9", "idempotency_key": "denied-1"})
    # The first order is an incident, not a captured payment, so a new honest order can proceed
    # only if the incident status is outside the paid window. Incident is not a paid status.
    assert denied_order.status_code == 200
    denied_body = denied_order.json()
    if denied_body["status"] == "CaptureSubmitted":
        denied = client.post(
            "/webhooks/simulator",
            json=capture_event(order_row(app, denied_body["id"]), event_id="wh-denied", event_type="PAYMENT.CAPTURE.DENIED"),
            headers={"X-Till-Simulator-Token": "test-sim"},
        )
        assert denied.status_code == 200
        assert client.get(f"/api/orders/{denied_body['id']}", headers=agent_headers).json()["status"] == "Incident"


def test_simulator_match_dedupes_and_real_path_checks_the_signature(client, app, agent_headers):
    order = submit(client, agent_headers).json()
    event = capture_event(order_row(app, order["id"]), event_id="wh-match")
    missing = client.post("/webhooks/simulator", json=event)
    assert missing.status_code == 401
    unsigned = client.post("/webhooks/paypal", json=event, headers={"X-Till-Simulate": "1"})
    assert unsigned.status_code == 400
    assert client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()["success"] is False

    ok = client.post("/webhooks/simulator", json=event, headers={"X-Till-Simulator-Token": "test-sim"})
    assert ok.json()["status"] == "complete"
    again = client.post("/webhooks/simulator", json=event, headers={"X-Till-Simulator-Token": "test-sim"})
    assert again.json()["status"] == "duplicate"
    assert client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()["success"] is True

    evil_called = []

    def boom(url):
        evil_called.append(url)
        raise AssertionError(url)

    app.state.fetch_cert = boom
    rejected = client.post(
        "/webhooks/paypal",
        content=b"{}",
        headers={"paypal-cert-url": "https://evil.example/certs/abc", "paypal-transmission-id": "x", "paypal-transmission-time": "y", "paypal-transmission-sig": "aa", "paypal-auth-algo": "SHA256withRSA"},
    )
    assert rejected.status_code == 400
    assert evil_called == []


def test_real_signature_completes_when_the_amount_matches(client, app, agent_headers):
    order = submit(client, agent_headers).json()
    event = capture_event(order_row(app, order["id"]), event_id="wh-signed")
    raw, headers, pem = _sign(event)
    app.state.fetch_cert = lambda url: pem
    response = client.post("/webhooks/paypal", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert client.get(f"/api/orders/{order['id']}", headers=agent_headers).json()["success"] is True

    raw_bad, headers_bad, pem_bad = _sign(event, webhook_id="wh_test", tamper=True)
    app.state.fetch_cert = lambda url: pem_bad
    forged = client.post("/webhooks/paypal", content=raw_bad, headers=headers_bad)
    assert forged.status_code == 400


def test_real_client_is_selected_only_with_sandbox_credentials():
    import pytest
    from app.config import Settings
    from app.paypal import PayPalError, build_paypal

    simulated = build_paypal("simulate", "https://api-m.sandbox.paypal.com", "id", "secret", "wh")
    assert simulated.mode == "simulate"
    live = build_paypal("sandbox", "https://api-m.sandbox.paypal.com", "id", "secret", "wh")
    assert live.mode == "sandbox"
    with pytest.raises(PayPalError, match="requires both"):
        build_paypal("sandbox", "https://api-m.sandbox.paypal.com", "", "", "wh")
    with pytest.raises(PayPalError, match="Sandbox API host"):
        build_paypal("sandbox", "https://api-m.paypal.com", "id", "secret", "wh")
    assert Settings  # settings object is the other selector used by the app


def test_capture_request_sends_paypal_request_id(monkeypatch):
    from app.paypal import PayPalClient

    captured = {}

    class Response:
        status_code = 200
        content = b'{"id":"ORDER"}'

        def json(self):
            return {"id": "ORDER"}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["method"] = method
        captured["url"] = url
        return Response()

    monkeypatch.setattr("app.paypal.httpx.request", fake_request)
    client = PayPalClient("sandbox", "https://api-m.sandbox.paypal.com", "id", "secret", "wh")
    monkeypatch.setattr(client, "_token", lambda: "token")
    client.capture("ORDER1", "dec_123")
    assert captured["headers"]["PayPal-Request-Id"] == "dec_123"
    assert captured["url"].endswith("/v2/checkout/orders/ORDER1/capture")


def test_sandbox_order_includes_the_saved_shipping_address(monkeypatch):
    from app.paypal import PayPalClient

    captured = {}

    class Response:
        status_code = 201
        content = b'{"id":"ORDER","status":"CREATED","links":[]}'

        def json(self):
            return {"id": "ORDER", "status": "CREATED", "links": []}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return Response()

    monkeypatch.setattr("app.paypal.httpx.request", fake_request)
    client = PayPalClient("sandbox", "https://api-m.sandbox.paypal.com", "id", "secret", "wh")
    monkeypatch.setattr(client, "_token", lambda: "token")
    client.create_order(
        custom_id="decision-1", currency="USD", payable_minor=1, item_minor=1,
        tax_minor=0, shipping_minor=0, title="Test item", sku="TEST-1", quantity=1,
        request_id="decision-1",
        shipping_address="2211 N First St, San Jose, CA, 95131, US",
        return_url="https://example.test/paypal/return?decision_id=decision-1",
        cancel_url="https://example.test/paypal/cancel?decision_id=decision-1",
    )
    address = captured["body"]["purchase_units"][0]["shipping"]["address"]
    assert address == {
        "address_line_1": "2211 N First St",
        "admin_area_2": "San Jose",
        "admin_area_1": "CA",
        "postal_code": "95131",
        "country_code": "US",
    }


def test_capture_returns_the_nested_paypal_capture(monkeypatch):
    from app.paypal import PayPalClient, amount_minor, paypal_order_id_of

    class Response:
        status_code = 201
        content = b'{"id":"ORDER1","status":"COMPLETED"}'

        def json(self):
            return {
                "id": "ORDER1",
                "status": "COMPLETED",
                "purchase_units": [{
                    "payments": {"captures": [{
                        "id": "CAPTURE1",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "74.00"},
                        "custom_id": "decision-1",
                    }]}
                }],
            }

    monkeypatch.setattr("app.paypal.httpx.request", lambda *args, **kwargs: Response())
    client = PayPalClient("sandbox", "https://api-m.sandbox.paypal.com", "id", "secret", "wh")
    monkeypatch.setattr(client, "_token", lambda: "token")
    capture = client.capture("ORDER1", "decision-1")
    assert capture["id"] == "CAPTURE1"
    assert amount_minor(capture) == ("USD", 7400)
    assert paypal_order_id_of(capture) == "ORDER1"


def order_row(app, order_id):
    from app.db import connect

    conn = connect(app.state.settings.db_path)
    try:
        return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    finally:
        conn.close()


def _sign(event, webhook_id="wh_test", tamper=False):
    raw = json.dumps(event, separators=(",", ":")).encode()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.sandbox.paypal.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    signed = raw + b" " if tamper else raw
    crc = zlib.crc32(raw if not tamper else signed) & 0xFFFFFFFF
    # Tamper the body after signing the original so verification fails.
    if tamper:
        crc = zlib.crc32(raw) & 0xFFFFFFFF
    message = f"tid|{now.isoformat()}|{webhook_id}|{crc}".encode()
    signature = base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {
        "paypal-transmission-id": "tid",
        "paypal-transmission-time": now.isoformat(),
        "paypal-transmission-sig": signature,
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT",
        "content-type": "application/json",
    }
    return (raw + b" ") if tamper else raw, headers, pem
