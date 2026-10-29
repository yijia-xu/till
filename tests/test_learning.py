from app.agent import proposal
from app.db import connect

from tests.helpers import set_caps, submit


def test_allow_once_cites_the_decision_and_does_not_lift_a_block(client, app, agent_headers):
    set_caps(client, purchase="80.00", ask="50.00")
    first = submit(client, agent_headers).json()
    assert first["decision"] == "NEED_APPROVAL"
    learned = client.post(f"/orders/{first['id']}/learn", data={"kind": "allow_once"}, follow_redirects=False)
    assert learned.status_code == 303
    assert app.state.paypal.calls == []
    second = submit(client, agent_headers, proposal("honest")).json()
    assert second["decision"] == "ALLOW"
    assert first["id"] in second["explanation"]
    conn = connect(app.state.settings.db_path)
    rule = conn.execute("SELECT consumed_at FROM learned_rules WHERE kind = 'allow_once'").fetchone()
    conn.close()
    assert rule["consumed_at"]

    set_caps(client, purchase="10.00", ask="10.00")
    blocked = submit(client, agent_headers).json()
    assert blocked["decision"] == "BLOCK"
    client.post(f"/orders/{blocked['id']}/learn", data={"kind": "allow_once"}, follow_redirects=False)
    again = submit(client, agent_headers).json()
    assert again["decision"] == "BLOCK"


def test_merchant_allowlist_is_cited_and_does_not_lift_the_cap(client, app, agent_headers):
    set_caps(client, purchase="80.00", ask="50.00")
    first = submit(client, agent_headers).json()
    client.post(f"/orders/{first['id']}/learn", data={"kind": "allow_merchant"}, follow_redirects=False)
    second = submit(client, agent_headers, proposal("honest")).json()
    assert second["decision"] == "ALLOW"
    assert first["id"] in (second["explanation"] or "")
    assert second["policy_version"] == first["policy_version"] + 1
    old = connect(app.state.settings.db_path).execute("SELECT policy_version FROM orders WHERE id = ?", (first["id"],)).fetchone()
    assert old["policy_version"] == first["policy_version"]

    conn = connect(app.state.settings.db_path)
    conn.execute(
        "UPDATE orders SET created_at = '2020-01-01T00:00:00+00:00' WHERE decision = 'ALLOW'"
    )
    conn.close()
    set_caps(client, purchase="50.00", ask="50.00")
    third = submit(client, agent_headers, proposal("honest")).json()
    assert third["decision"] == "BLOCK"
    assert third["problem"] == "budget"


def test_shipping_rule_is_cited_on_the_next_order(client, agent_headers):
    from tests.helpers import login

    login(client)
    first = submit(client, agent_headers).json()
    client.post(f"/orders/{first['id']}/learn", data={"kind": "shipping_toward_cap"}, follow_redirects=False)
    shipped = submit(client, agent_headers, proposal("shipping")).json()
    assert shipped["decision"] == "BLOCK"
    assert shipped["problem"] == "budget"
    assert shipped["malicious"] is False
    assert shipped["payable_minor"] == 8300
    assert first["id"] in shipped["explanation"]
    assert shipped["policy_version"] == first["policy_version"] + 1
