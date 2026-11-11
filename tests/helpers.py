from app.agent import proposal
from app.db import connect


def login(client):
    response = client.post(
        "/login",
        data={"email": "demo@till.local", "password": "demo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def set_caps(client, *, purchase="80.00", daily="200.00", category="200.00", ask="80.00", sentence="Running shoes, size 9, at most $80, no subscription."):
    login(client)
    response = client.post(
        "/policy",
        data={
            "sentence": sentence,
            "max_purchase": purchase,
            "daily_max": daily,
            "category_max": category,
            "ask_above": ask,
            "shipping_address": "100 Demo Ave, Springfield, IL, 62701, US",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def submit(client, headers, body=None, scenario="honest"):
    payload = body if body is not None else proposal(scenario)
    return client.post("/api/orders", json=payload, headers=headers)


def order_row(app, order_id):
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
