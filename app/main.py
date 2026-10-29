"""HTTP routes. The agent can submit and read. A shopper approves. Capture is not a route."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .checkout import (
    CheckoutError,
    agent_view,
    apply_webhook,
    approve_order,
    fetch_order,
    learn_rule,
    list_orders,
    open_db,
    present_order,
    reject_order,
    save_policy,
    submit_proposal,
)
from .config import Settings, load_settings
from .db import SHOPPER_EMAIL, init_db, utcnow, verify_password
from .money import dollars_to_minor, from_minor
from .needs import build_needs, parse_sentence
from .paypal import build_paypal, fetch_paypal_cert, verify_webhook_signature

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["money"] = lambda value: "—" if value is None else from_minor(int(value))


def render(request: Request, name: str, context: dict, status_code: int = 200):
    body = {"request": request, **context}
    return TEMPLATES.TemplateResponse(request, name, body, status_code=status_code)

PROBLEM_LABELS = {
    "budget": "Budget",
    "needs": "Needs",
    "tamper": "Listing versus PayPal",
    "duplicate": "Duplicate",
    "webhook": "Webhook mismatch",
    "approval": "Approval",
    "intent": "Intent expired",
    "arithmetic": "Amounts do not add up",
    "paypal": "PayPal refused the order",
    "paypal_read": "PayPal read failed",
    "capture_denied": "Capture denied",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.db_path)
    app = FastAPI(title="Till")
    app.state.settings = settings
    app.state.paypal = build_paypal(
        settings.paypal_mode,
        settings.paypal_base,
        settings.paypal_client_id,
        settings.paypal_client_secret,
        settings.paypal_webhook_id,
    )
    app.state.needs = build_needs(settings.needs_model_api_key, settings.needs_model_url, settings.needs_model_name)
    app.state.fetch_cert = fetch_paypal_cert

    @app.get("/health")
    def health():
        return {"ok": True, "paypal_mode": app.state.paypal.mode}

    @app.get("/")
    def root():
        return RedirectResponse("/orders", status_code=303)

    @app.post("/api/orders")
    async def create_order(request: Request):
        _require_agent(request)
        body = await request.json()
        conn = open_db(settings.db_path)
        try:
            order = submit_proposal(
                conn,
                app.state.paypal,
                app.state.needs,
                body,
                window_seconds=settings.duplicate_window_seconds,
                approval_ttl_seconds=settings.approval_ttl_seconds,
            )
            return agent_view(present_order(conn, order))
        except CheckoutError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        finally:
            conn.close()

    @app.get("/api/orders/{order_id}")
    def read_order(order_id: str, request: Request):
        _require_agent(request)
        conn = open_db(settings.db_path)
        try:
            order = fetch_order(conn, order_id)
            if order is None:
                raise HTTPException(404, "Order not found.")
            return agent_view(present_order(conn, order))
        finally:
            conn.close()

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        raw = await request.body()
        ok = verify_webhook_signature(
            dict(request.headers),
            raw,
            settings.paypal_webhook_id,
            app.state.fetch_cert,
        )
        if not ok:
            raise HTTPException(400, "Webhook signature check failed.")
        return _accept_event(raw, verified=True)

    @app.post("/webhooks/simulator")
    async def simulator_webhook(request: Request):
        token = request.headers.get("x-till-simulator-token") or ""
        if not token or not secrets.compare_digest(token, settings.simulator_token):
            raise HTTPException(401, "Simulator token required.")
        raw = await request.body()
        return _accept_event(raw, verified=True)

    def _accept_event(raw: bytes, *, verified: bool):
        import json

        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "Webhook body must be JSON.") from exc
        conn = open_db(settings.db_path)
        try:
            return apply_webhook(conn, app.state.paypal, event, verified=verified)
        except CheckoutError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        finally:
            conn.close()

    @app.get("/login")
    def login_form(request: Request, error: str = ""):
        return render(request, "login.html", {"error": error, "prefill": SHOPPER_EMAIL, "signed_in": False, "email": ""})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        password = str(form.get("password") or "")
        conn = open_db(settings.db_path)
        try:
            row = conn.execute("SELECT * FROM shoppers WHERE email = ?", (email,)).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                return render(
                    request,
                    "login.html",
                    {"error": "That email and password do not match.", "prefill": email, "signed_in": False, "email": ""},
                    status_code=401,
                )
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(hours=12)).replace(microsecond=0).isoformat()
            conn.execute(
                "INSERT INTO sessions (token, shopper_id, expires_at) VALUES (?, ?, ?)",
                (token, row["id"], expires),
            )
        finally:
            conn.close()
        response = RedirectResponse("/orders", status_code=303)
        response.set_cookie("till_session", token, httponly=True, samesite="lax", path="/")
        return response

    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get("till_session")
        if token:
            conn = open_db(settings.db_path)
            try:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            finally:
                conn.close()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("till_session", path="/")
        return response

    @app.get("/orders")
    def orders_page(request: Request):
        conn = open_db(settings.db_path)
        try:
            shopper = _shopper(conn, request)
            if shopper is None:
                return RedirectResponse("/login", status_code=303)
            return render(
                request,
                "orders.html",
                {
                    "orders": list_orders(conn),
                    "paypal_mode": app.state.paypal.mode,
                    "email": SHOPPER_EMAIL,
                    "signed_in": True,
                },
            )
        finally:
            conn.close()

    @app.get("/orders/{order_id}")
    def order_page(order_id: str, request: Request, error: str = "", learned: str = ""):
        conn = open_db(settings.db_path)
        try:
            shopper = _shopper(conn, request)
            if shopper is None:
                return RedirectResponse(f"/login?next=/orders/{order_id}", status_code=303)
            order = fetch_order(conn, order_id)
            if order is None or order["shopper_id"] != shopper:
                raise HTTPException(404, "Order not found.")
            view = present_order(conn, order)
            view["problem_label"] = _problem_label(order, view)
            view["where"] = _where(view)
            return render(
                request,
                "order.html",
                {
                    "order": view,
                    "error": error,
                    "learned": learned,
                    "email": SHOPPER_EMAIL,
                    "signed_in": True,
                    "paypal_mode": app.state.paypal.mode,
                },
            )
        finally:
            conn.close()

    @app.post("/orders/{order_id}/approve")
    def approve(order_id: str, request: Request):
        return _shopper_action(request, order_id, "approve")

    @app.post("/orders/{order_id}/reject")
    def reject(order_id: str, request: Request):
        return _shopper_action(request, order_id, "reject")

    @app.post("/orders/{order_id}/learn")
    async def learn(order_id: str, request: Request):
        form = await request.form()
        kind = str(form.get("kind") or "")
        return _shopper_action(request, order_id, "learn", kind=kind)

    @app.get("/policy")
    def policy_page(request: Request, error: str = "", saved: str = ""):
        conn = open_db(settings.db_path)
        try:
            shopper = _shopper(conn, request)
            if shopper is None:
                return RedirectResponse("/login", status_code=303)
            row = conn.execute(
                "SELECT version, rules_json, raw_text FROM policies WHERE active = 1 ORDER BY version DESC LIMIT 1"
            ).fetchone()
            import json

            rules = json.loads(row["rules_json"]) if row else {}
            return render(
                request,
                "policy.html",
                {
                    "rules": rules,
                    "version": row["version"] if row else 0,
                    "error": error,
                    "saved": saved,
                    "email": SHOPPER_EMAIL,
                    "signed_in": True,
                },
            )
        finally:
            conn.close()

    @app.post("/policy")
    async def update_policy(request: Request):
        form = await request.form()
        sentence = str(form.get("sentence") or "").strip()
        if not sentence:
            return RedirectResponse("/policy?error=" + quote("Write the shopper's sentence."), status_code=303)
        try:
            parsed = parse_sentence(sentence)
            rules = {
                "category": parsed.category,
                "size": parsed.size,
                "brand": parsed.brand,
                "exclusions": parsed.exclusions,
                "max_purchase_minor": dollars_to_minor(str(form.get("max_purchase") or ""))
                if str(form.get("max_purchase") or "").strip()
                else parsed.max_purchase_minor,
                "daily_max_minor": dollars_to_minor(str(form.get("daily_max") or "")),
                "category_max_minor": dollars_to_minor(str(form.get("category_max") or "")),
                "ask_above_minor": dollars_to_minor(str(form.get("ask_above") or "")),
                "currency": "USD",
                "no_subscription": any("subscription" in item for item in parsed.exclusions),
                "include_shipping": True,
                "shipping_counts_toward_cap": True,
                "intent_quantity": 1,
            }
            if rules["max_purchase_minor"] is None:
                raise ValueError("missing cap")
        except (ValueError, ArithmeticError):
            return RedirectResponse(
                "/policy?error=" + quote("Use dollar amounts like 80.00, and name a cap or say at most $80."),
                status_code=303,
            )
        conn = open_db(settings.db_path)
        try:
            if _shopper(conn, request) is None:
                return RedirectResponse("/login", status_code=303)
            save_policy(conn, sentence, rules, intent_ttl_seconds=settings.intent_ttl_seconds)
        finally:
            conn.close()
        return RedirectResponse("/policy?saved=1", status_code=303)

    def _shopper_action(request: Request, order_id: str, action: str, kind: str = ""):
        conn = open_db(settings.db_path)
        try:
            shopper = _shopper(conn, request)
            if shopper is None:
                return RedirectResponse("/login", status_code=303)
            try:
                if action == "approve":
                    approve_order(conn, app.state.paypal, order_id, shopper)
                elif action == "reject":
                    reject_order(conn, order_id, shopper)
                else:
                    learn_rule(conn, order_id, shopper, kind, intent_ttl_seconds=settings.intent_ttl_seconds)
            except CheckoutError as exc:
                return RedirectResponse(f"/orders/{order_id}?error={quote(exc.detail)}", status_code=303)
        finally:
            conn.close()
        if action == "learn":
            return RedirectResponse(f"/orders/{order_id}?learned={quote(kind)}", status_code=303)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)

    def _require_agent(request: Request) -> None:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not secrets.compare_digest(token, settings.agent_token):
            raise HTTPException(401, "Agent token required.")

    def _shopper(conn, request: Request) -> str | None:
        token = request.cookies.get("till_session")
        if not token:
            return None
        row = conn.execute("SELECT shopper_id, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
        if row is None or row["expires_at"] < utcnow():
            return None
        return row["shopper_id"]

    return app


def _problem_label(order: dict, view: dict) -> str:
    if view["problem"] == "tamper" and not order.get("paypal_snapshot_json"):
        return "Claim versus listing"
    return PROBLEM_LABELS.get(view["problem"] or "", "Check")


def _where(view: dict) -> str:
    if view["status"] == "Complete":
        return "It did not fail. A verified webhook matched the currency and amount."
    if view["status"] == "CaptureSubmitted":
        return "Till submitted the capture. This is not complete until a verified webhook matches."
    if view["status"] == "NeedsApproval":
        return "Till is waiting for you to approve this exact amount, SKU, quantity, currency, and address."
    if view["status"] == "Rejected":
        return "You rejected it. Till did not talk to PayPal."
    return view["explanation"]


app = create_app()
