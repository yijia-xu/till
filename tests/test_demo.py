from app.demo import run_scenarios

from tests.helpers import login, submit
from app.agent import proposal


def test_demo_scenarios(app):
    rows = run_scenarios(app.state.settings.db_path, app.state.paypal, app.state.needs, app.state.settings)
    by_name = {row["scenario"]: row for row in rows}
    assert by_name["honest"]["decision"] == "ALLOW"
    assert by_name["honest"]["status"] == "Complete"
    assert by_name["honest"]["malicious"] is False
    for name in ("price", "swap", "quantity"):
        assert by_name[name]["decision"] == "BLOCK"
        assert by_name[name]["malicious"] is True
        assert by_name[name]["problem"] == "tamper"
    assert by_name["duplicate"]["problem"] == "duplicate"
    assert by_name["duplicate"]["malicious"] is True


def test_order_pages_show_the_four_views(client, app, agent_headers):
    run_scenarios(app.state.settings.db_path, app.state.paypal, app.state.needs, app.state.settings)
    login(client)
    page = client.get("/orders")
    assert page.status_code == 200
    assert "Nike Pegasus running shoes" in page.text
    assert "Malicious" in page.text
    honest = client.get("/orders").text
    assert "Complete" in honest

    listing = client.get("/orders")
    # Open the first detail link.
    start = listing.text.find('href="/orders/dec_')
    assert start != -1
    href = listing.text[start + 6 : listing.text.find('"', start + 6)]
    detail = client.get(href)
    assert detail.status_code == 200
    for label in ("Ask", "Agent claim", "Listed price", "PayPal amount", "Item", "Tax", "Shipping", "Add-ons"):
        assert label in detail.text

    needs = submit(client, agent_headers, proposal("honest") | {"sku": "NECK-01", "title": "Gold necklace"}).json()
    needs_page = client.get(f"/orders/{needs['id']}")
    assert "Policy" in needs_page.text
    price = submit(client, agent_headers, proposal("price")).json()
    price_page = client.get(f"/orders/{price['id']}")
    assert "Malicious" in price_page.text
    assert "approved, user_id, or amount" in price_page.text


def test_capture_is_not_an_http_route(client):
    paths = " ".join(client.get("/openapi.json").json()["paths"])
    assert "capture" not in paths
    assert "/api/orders" in paths
