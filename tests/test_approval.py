from app.agent import proposal
from app.db import connect

from tests.helpers import login, set_caps, submit


def test_shopper_approval_pays_and_a_changed_payload_expires(client, app, agent_headers):
    set_caps(client, purchase="80.00", ask="50.00")
    waiting = submit(client, agent_headers).json()
    assert waiting["decision"] == "NEED_APPROVAL"
    assert waiting["approval_status"] == "pending"
    assert app.state.paypal.calls == []

    approved = client.post(f"/orders/{waiting['id']}/approve", follow_redirects=False)
    assert approved.status_code == 303
    assert any(call[0] == "capture" for call in app.state.paypal.calls)
    assert any(call[0] == "capture" and call[2] == waiting["id"] for call in app.state.paypal.calls)

    other = submit(client, agent_headers, proposal("honest")).json()
    assert other["decision"] == "BLOCK"
    assert other["problem"] == "duplicate"


def test_edited_approval_does_not_capture(client, app, agent_headers):
    set_caps(client, ask="50.00")
    waiting = submit(client, agent_headers).json()
    conn = connect(app.state.settings.db_path)
    conn.execute("UPDATE orders SET address = ? WHERE id = ?", ("9 Other St", waiting["id"]))
    conn.close()
    client.post(f"/orders/{waiting['id']}/approve", follow_redirects=False)
    row = connect(app.state.settings.db_path).execute("SELECT * FROM orders WHERE id = ?", (waiting["id"],)).fetchone()
    assert row["approval_status"] == "expired"
    assert row["malicious"] == 1
    assert app.state.paypal.calls == []


def test_expired_approval_does_not_capture(client, app, agent_headers):
    set_caps(client, ask="50.00")
    waiting = submit(client, agent_headers).json()
    conn = connect(app.state.settings.db_path)
    conn.execute(
        "UPDATE orders SET approval_expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", waiting["id"]),
    )
    conn.close()
    client.post(f"/orders/{waiting['id']}/approve", follow_redirects=False)
    conn = connect(app.state.settings.db_path)
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (waiting["id"],)).fetchone()
    conn.close()
    assert row["approval_status"] == "expired"
    assert row["malicious"] == 0
    assert app.state.paypal.calls == []


def test_reject_does_not_call_paypal(client, app, agent_headers):
    set_caps(client, ask="50.00")
    waiting = submit(client, agent_headers).json()
    client.post(f"/orders/{waiting['id']}/reject", follow_redirects=False)
    status = client.get(f"/api/orders/{waiting['id']}", headers=agent_headers).json()
    assert status["status"] == "Rejected"
    assert status["success"] is False
    assert app.state.paypal.calls == []


def test_agent_cannot_approve(client, app, agent_headers):
    set_caps(client, ask="50.00")
    waiting = submit(client, agent_headers).json()
    client.post("/logout", follow_redirects=False)
    response = client.post(
        f"/orders/{waiting['id']}/approve",
        headers=agent_headers,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert app.state.paypal.calls == []


def test_agent_cannot_change_the_shopper_saved_shipping_address(client, app, agent_headers):
    set_caps(client)
    altered = proposal("honest") | {"address": "9 Other St, Springfield, IL, 62701, US"}
    response = submit(client, agent_headers, altered).json()
    assert response["decision"] == "BLOCK"
    assert response["problem"] == "tamper"
    assert response["malicious"] is True
    assert app.state.paypal.calls == []
