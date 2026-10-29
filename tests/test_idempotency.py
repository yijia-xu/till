from app.agent import proposal

from tests.helpers import submit


def test_same_key_pays_once_and_a_different_payload_is_refused(client, app, agent_headers):
    body = proposal("honest")
    first = submit(client, agent_headers, body)
    second = submit(client, agent_headers, body)
    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert [call[0] for call in app.state.paypal.calls].count("create") == 1

    changed = dict(body)
    changed["total_minor"] = 7300
    changed["unit_price_minor"] = 7300
    conflict = submit(client, agent_headers, changed)
    assert conflict.status_code == 409
    assert [call[0] for call in app.state.paypal.calls].count("create") == 1


def test_missing_total_is_rejected_even_if_agent_says_approved(client, agent_headers):
    body = proposal("honest")
    del body["total_minor"]
    response = submit(client, agent_headers, body)
    assert response.status_code == 400


def test_agent_token_is_required(client):
    response = client.post("/api/orders", json=proposal("honest"))
    assert response.status_code == 401
