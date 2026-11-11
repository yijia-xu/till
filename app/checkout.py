"""Order decisions. The agent can submit and read. Only Till talks to PayPal."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .db import PAID_STATUSES, SHOPPER_ID, connect, start_of_utc_day, utcnow
from .engine import Verdict, approval_hash, claim_fingerprint, evaluate
from .needs import NeedsUnavailable, combine_needs
from .paypal import (
    PayPalError,
    amount_minor,
    custom_id_of,
    paypal_line,
    paypal_order_id_of,
)

OPEN_STATES = {"Approved", "OrderCreated", "PayerAction", "CaptureSubmitted"}


class CheckoutError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@contextmanager
def immediate(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _row(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def parse_proposal(body: dict) -> tuple[dict, list[str]]:
    """Build a claim. approved, user_id, and amount from the agent grant nothing."""
    if not isinstance(body, dict):
        raise CheckoutError(400, "The proposal must be an object.")
    ignored = [name for name in ("approved", "user_id", "amount") if name in body]

    def text(name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise CheckoutError(400, f"{name} is required.")
        return value.strip()

    def minor(name: str, default: int | None = None) -> int:
        if name not in body or body[name] is None:
            if default is None:
                raise CheckoutError(400, f"{name} is required.")
            return default
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise CheckoutError(400, f"{name} must be an integer number of minor units.")
        if value < 0:
            raise CheckoutError(400, f"{name} cannot be negative.")
        return value

    quantity = body.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity > 100:
        raise CheckoutError(400, "quantity must be a whole number from 1 to 100.")
    currency = text("currency").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise CheckoutError(400, "currency must be a 3-letter code.")
    claim = {
        "idempotency_key": text("idempotency_key"),
        "merchant_id": text("merchant_id"),
        "sku": text("sku"),
        "title": text("title"),
        "quantity": quantity,
        "unit_price_minor": minor("unit_price_minor"),
        "tax_minor": minor("tax_minor", 0),
        "shipping_minor": minor("shipping_minor", 0),
        "addons_minor": minor("addons_minor", 0),
        "total_minor": minor("total_minor"),
        "currency": currency,
        "address": text("address"),
    }
    if "@" in claim["merchant_id"]:
        raise CheckoutError(400, "merchant_id is a catalog id, not a payee.")
    return claim, ignored


def active_policy(conn: sqlite3.Connection) -> tuple[int, dict]:
    row = conn.execute(
        "SELECT version, raw_text, rules_json FROM policies WHERE active = 1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise CheckoutError(409, "The shopper has no policy.")
    policy = json.loads(row["rules_json"])
    policy["sentence"] = row["raw_text"]
    return int(row["version"]), policy


def find_listing(conn: sqlite3.Connection, merchant_id: str, sku: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM catalog WHERE merchant_id = ? AND sku = ?",
        (merchant_id, sku),
    ).fetchone()
    return _row(row)


def spent_since(conn: sqlite3.Connection, shopper_id: str, category: str | None) -> int:
    rows = conn.execute(
        f"""
        SELECT capped_minor, listed_json FROM orders
        WHERE shopper_id = ? AND created_at >= ?
          AND status IN ({",".join("?" for _ in PAID_STATUSES)})
        """,
        (shopper_id, start_of_utc_day(), *PAID_STATUSES),
    ).fetchall()
    total = 0
    for row in rows:
        if category is not None:
            listed = json.loads(row["listed_json"] or "null")
            if not listed or listed.get("category") != category:
                continue
        total += int(row["capped_minor"])
    return total


def duplicate_payment(conn: sqlite3.Connection, shopper_id: str, merchant_id: str, sku: str, cutoff: str) -> bool:
    row = conn.execute(
        f"""
        SELECT id FROM orders
        WHERE shopper_id = ? AND merchant_id = ? AND sku = ? AND created_at >= ?
          AND status IN ({",".join("?" for _ in PAID_STATUSES)})
        LIMIT 1
        """,
        (shopper_id, merchant_id, sku, cutoff, *PAID_STATUSES),
    ).fetchone()
    return row is not None


def fetch_order(conn: sqlite3.Connection, order_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def fetch_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM orders WHERE idempotency_key = ?", (key,)).fetchone())


def save_order(conn: sqlite3.Connection, order_id: str, **fields) -> None:
    fields["updated_at"] = utcnow()
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE orders SET {columns} WHERE id = ?",
        (*fields.values(), order_id),
    )


def audit(conn: sqlite3.Connection, order_id: str | None, event_type: str, message: str, **extra) -> None:
    payload = {"message": message, **extra}
    conn.execute(
        "INSERT INTO audit (order_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (order_id, event_type, json.dumps(payload), utcnow()),
    )


def audits_for(conn: sqlite3.Connection, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_type, payload_json, created_at FROM audit WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    found = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        found.append(item)
    return found


def _intent_expired(policy: dict) -> bool:
    expires = policy.get("intent_expires_at")
    return bool(expires) and expires < utcnow()


def _citations(policy: dict, merchant_id: str) -> tuple[str | None, str | None]:
    merchant_citation = None
    shipping_citation = None
    for rule in policy.get("rule_citations") or []:
        if rule.get("kind") == "allow_merchant" and rule.get("merchant_id") == merchant_id:
            merchant_citation = f"Cites decision {rule['decision_id']}, which allowlisted this merchant."
        if rule.get("kind") == "shipping_toward_cap":
            shipping_citation = f"Cites decision {rule['decision_id']}, which counts shipping toward the cap."
    return merchant_citation, shipping_citation


def _matching_allow_once(conn: sqlite3.Connection, digest: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM learned_rules
        WHERE kind = 'allow_once' AND approval_hash = ? AND consumed_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (digest,),
    ).fetchone()
    return _row(row)


def decide(conn, needs, claim: dict, *, shopper_id: str, window_seconds: int) -> tuple[int, dict, Verdict, str]:
    version, policy = active_policy(conn)
    listed = find_listing(conn, claim["merchant_id"], claim["sku"])
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).replace(microsecond=0).isoformat()
    duplicate = duplicate_payment(conn, shopper_id, claim["merchant_id"], claim["sku"], cutoff)
    category = listed.get("category") if listed else None
    merchant_citation, shipping_citation = _citations(policy, claim["merchant_id"])
    if _intent_expired(policy):
        verdict = Verdict(
            decision="BLOCK",
            malicious=False,
            problem="intent",
            reasons=[
                {
                    "code": "intent_expired",
                    "message": "The shopper's intent has expired.",
                    "hard": True,
                }
            ],
            explanation="The shopper's intent has expired, so Till will not pay.",
            payable_minor=int(claim["total_minor"]),
            capped_minor=int(claim["total_minor"]),
        )
    else:
        verdict = evaluate(
            policy=policy,
            claimed=claim,
            listed=listed,
            spent_today_minor=spent_since(conn, shopper_id, None),
            spent_category_minor=spent_since(conn, shopper_id, category),
            duplicate=duplicate,
            merchant_citation=merchant_citation,
            shipping_citation=shipping_citation,
        )
        try:
            judgment, note = needs.judge(policy, claim, listed)
        except NeedsUnavailable as exc:
            judgment, note = "unavailable", "The needs model is unavailable, so Till waits for a person."
            note = str(exc) or note
        verdict = combine_needs(verdict, judgment, note)
        if verdict.decision == "NEED_APPROVAL":
            digest = approval_hash(
                amount=verdict.payable_minor,
                sku=claim["sku"],
                quantity=claim["quantity"],
                currency=claim["currency"],
                address=claim["address"],
            )
            rule = _matching_allow_once(conn, digest)
            if rule is not None:
                source = rule["source_order_id"]
                once = f"Cites decision {source}, which allowed this order once."
                verdict.decision = "ALLOW"
                verdict.problem = None
                verdict.explanation = f"{verdict.explanation} {once}".strip()
                verdict.citation = " ".join(bit for bit in (verdict.citation, once) if bit)
                conn.execute(
                    "UPDATE learned_rules SET consumed_at = ? WHERE id = ?",
                    (utcnow(), rule["id"]),
                )
    return version, policy, verdict, json.dumps(listed) if listed else ""


def insert_decision(
    conn,
    *,
    claim: dict,
    shopper_id: str,
    version: int,
    policy: dict,
    verdict: Verdict,
    listed_json: str,
    approval_ttl_seconds: int,
    ignored: list[str],
) -> str:
    order_id = "dec_" + uuid.uuid4().hex
    now = utcnow()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=approval_ttl_seconds)).replace(microsecond=0).isoformat()
    digest = approval_hash(
        amount=verdict.payable_minor,
        sku=claim["sku"],
        quantity=claim["quantity"],
        currency=claim["currency"],
        address=claim["address"],
    )
    if verdict.decision == "ALLOW":
        status = "Approved"
        approval_status = "approved"
        approval_source = "policy"
        approved_hash = digest
    elif verdict.decision == "NEED_APPROVAL":
        status = "NeedsApproval"
        approval_status = "pending"
        approval_source = ""
        approved_hash = None
    else:
        status = "Blocked"
        approval_status = "blocked"
        approval_source = ""
        approved_hash = None
    conn.execute(
        """
        INSERT INTO orders (
          id, shopper_id, idempotency_key, policy_version, policy_snapshot_json,
          merchant_id, sku, claimed_json, listed_json, canonical_json, payload_hash,
          approval_hash, approved_hash, approval_status, approval_source, approval_expires_at,
          status, decision, malicious, problem, reasons_json, explanation, citation,
          payable_minor, capped_minor, currency, address, created_at, updated_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            order_id,
            shopper_id,
            claim["idempotency_key"],
            version,
            json.dumps(policy),
            claim["merchant_id"],
            claim["sku"],
            json.dumps(claim),
            listed_json or None,
            json.dumps(verdict.canonical),
            claim_fingerprint(claim),
            digest,
            approved_hash,
            approval_status,
            approval_source,
            expires,
            status,
            verdict.decision,
            1 if verdict.malicious else 0,
            verdict.problem,
            json.dumps(verdict.reasons),
            verdict.explanation,
            verdict.citation,
            verdict.payable_minor,
            verdict.capped_minor,
            claim["currency"],
            claim["address"],
            now,
            now,
        ),
    )
    audit(conn, order_id, "received", "Till received the proposal.")
    audit(conn, order_id, "validating", "Till checked budget, needs, the listing snapshot, and duplicates.")
    audit(conn, order_id, status.lower(), verdict.explanation)
    if ignored:
        audit(
            conn,
            order_id,
            "agent_ignored",
            "Till ignored agent fields that cannot grant approval: " + ", ".join(ignored) + ".",
        )
    return order_id


def submit_proposal(
    conn,
    paypal,
    needs,
    body: dict,
    *,
    shopper_id: str = SHOPPER_ID,
    window_seconds: int,
    approval_ttl_seconds: int,
    public_base_url: str = "",
) -> dict:
    claim, ignored = parse_proposal(body)
    fingerprint = claim_fingerprint(claim)
    replay_id = None
    order_id = None
    with immediate(conn):
        existing = fetch_by_key(conn, claim["idempotency_key"])
        if existing is not None:
            if existing["payload_hash"] != fingerprint:
                raise CheckoutError(409, "This idempotency key was already used for a different order.")
            replay_id = existing["id"]
        else:
            version, policy, verdict, listed_json = decide(
                conn, needs, claim, shopper_id=shopper_id, window_seconds=window_seconds
            )
            try:
                order_id = insert_decision(
                    conn,
                    claim=claim,
                    shopper_id=shopper_id,
                    version=version,
                    policy=policy,
                    verdict=verdict,
                    listed_json=listed_json,
                    approval_ttl_seconds=approval_ttl_seconds,
                    ignored=ignored,
                )
            except sqlite3.IntegrityError as exc:
                raise CheckoutError(409, "This idempotency key was already used.") from exc
    if replay_id is not None:
        order = fetch_order(conn, replay_id)
        if order and order["status"] == "Approved":
            return start_payment(conn, paypal, replay_id, public_base_url=public_base_url)
        return order
    order = fetch_order(conn, order_id)
    if order["status"] == "Approved":
        return start_payment(conn, paypal, order_id, public_base_url=public_base_url)
    return order


def current_approval_hash(order: dict) -> str:
    claimed = json.loads(order["claimed_json"])
    return approval_hash(
        amount=int(order["payable_minor"]),
        sku=order["sku"],
        quantity=int(claimed["quantity"]),
        currency=order["currency"],
        address=order["address"],
    )


def _expire_approval(conn, order: dict, *, malicious: bool, message: str) -> dict:
    save_order(
        conn,
        order["id"],
        status="Blocked",
        decision="BLOCK",
        approval_status="expired",
        malicious=1 if malicious else 0,
        problem="approval",
        explanation=message,
    )
    audit(conn, order["id"], "approval_expired", message)
    return fetch_order(conn, order["id"])


def start_payment(conn, paypal, order_id: str, *, public_base_url: str = "") -> dict:
    order = fetch_order(conn, order_id)
    if order is None:
        raise CheckoutError(404, "Order not found.")
    if order["status"] in {"CaptureSubmitted", "Complete", "Blocked", "Rejected", "Incident", "NeedsApproval"}:
        return order
    if order["status"] not in {"Approved", "OrderCreated", "PayerAction"}:
        return order
    if order["approval_expires_at"] and order["approval_expires_at"] < utcnow():
        if order.get("paypal_order_id"):
            try:
                paypal.void(order["paypal_order_id"])
            except PayPalError:
                pass
        return _expire_approval(conn, order, malicious=False, message="The approval expired before Till talked to PayPal.")
    if order["approved_hash"] != order["approval_hash"] or current_approval_hash(order) != order["approval_hash"]:
        return _expire_approval(
            conn,
            order,
            malicious=True,
            message="The amount, SKU, quantity, currency, or address changed after approval, so the approval is void.",
        )
    if order["decision"] != "ALLOW":
        return order

    listed = json.loads(order["listed_json"] or "null")
    claimed = json.loads(order["claimed_json"])
    if listed is None:
        return order
    request_id = order["id"]
    approve_url = order.get("approve_url") or ""
    try:
        if not order["paypal_order_id"]:
            base_url = public_base_url.rstrip("/")
            created = paypal.create_order(
                custom_id=order["id"],
                currency=order["currency"],
                payable_minor=int(order["payable_minor"]),
                item_minor=int(listed["unit_price_minor"]),
                tax_minor=int(listed.get("tax_minor") or 0),
                shipping_minor=int(listed.get("shipping_minor") or 0),
                addons_minor=int(listed.get("addons_minor") or 0),
                title=listed["title"],
                sku=listed["sku"],
                quantity=int(claimed["quantity"]),
                request_id=request_id,
                return_url=f"{base_url}/paypal/return?decision_id={order['id']}" if base_url else None,
                cancel_url=f"{base_url}/paypal/cancel?decision_id={order['id']}" if base_url else None,
            )
            paypal_order_id = created["id"]
            approve_url = next(
                (
                    link.get("href")
                    for link in created.get("links", [])
                    if link.get("rel") in {"payer-action", "approve"} and link.get("href")
                ),
                "",
            )
            save_order(
                conn,
                order["id"],
                paypal_order_id=paypal_order_id,
                approve_url=approve_url,
                status="OrderCreated",
            )
            audit(conn, order["id"], "payment_created", "Till created a PayPal order.", paypal_order_id=paypal_order_id)
        else:
            paypal_order_id = order["paypal_order_id"]
    except PayPalError:
        save_order(
            conn,
            order["id"],
            status="Blocked",
            decision="BLOCK",
            problem="paypal",
            explanation="PayPal did not accept the order. Till did not capture.",
        )
        audit(conn, order["id"], "paypal_error", "Creating the PayPal order failed. Till did not capture.")
        return fetch_order(conn, order["id"])

    existing_capture = _row(
        conn.execute(
            "SELECT order_id FROM captured_orders WHERE paypal_order_id = ?",
            (paypal_order_id,),
        ).fetchone()
    )
    if existing_capture is not None and existing_capture["order_id"] == order["id"]:
        save_order(conn, order["id"], status="CaptureSubmitted")
        return fetch_order(conn, order["id"])
    if existing_capture is not None:
        message = "This PayPal order id was already captured and cannot be paid again."
        save_order(
            conn,
            order["id"],
            status="Blocked",
            decision="BLOCK",
            malicious=1,
            problem="duplicate",
            explanation=message,
        )
        audit(conn, order["id"], "duplicate_capture", message)
        return fetch_order(conn, order["id"])

    try:
        remote = paypal.get_order(paypal_order_id)
    except PayPalError:
        try:
            paypal.void(paypal_order_id)
        except PayPalError:
            pass
        save_order(
            conn,
            order["id"],
            status="Blocked",
            decision="BLOCK",
            problem="paypal_read",
            explanation="Till could not read the PayPal order back, so it did not capture.",
        )
        audit(conn, order["id"], "paypal_read_failed", "The PayPal read failed. Till did not capture.")
        return fetch_order(conn, order["id"])

    line = paypal_line(remote) or {}
    save_order(conn, order["id"], paypal_snapshot_json=json.dumps(line))
    audit(conn, order["id"], "payment_read", "Till read the PayPal order before capture.")
    mismatch = _paypal_mismatch(order, listed, claimed, line)
    if mismatch:
        try:
            paypal.void(paypal_order_id)
        except PayPalError:
            pass
        message = f"PayPal changed the {mismatch} after Till snapshotted the listing. Till voided the order."
        save_order(
            conn,
            order["id"],
            status="Blocked",
            decision="BLOCK",
            malicious=1,
            problem="tamper",
            approval_status="blocked",
            explanation=message,
        )
        audit(conn, order["id"], "voided", message)
        return fetch_order(conn, order["id"])

    if str(remote.get("status") or "").upper() != "APPROVED":
        approve_url = approve_url or next(
            (
                link.get("href")
                for link in remote.get("links", [])
                if link.get("rel") in {"payer-action", "approve"} and link.get("href")
            ),
            "",
        )
        if not approve_url:
            message = "PayPal has not approved this order and returned no buyer approval link. Till did not capture."
            save_order(conn, order["id"], status="Incident", problem="paypal", explanation=message)
            audit(conn, order["id"], "approval_link_missing", message)
            return fetch_order(conn, order["id"])
        was_waiting = order["status"] == "PayerAction"
        save_order(
            conn,
            order["id"],
            status="PayerAction",
            approve_url=approve_url,
            explanation="Till approved this order. PayPal buyer approval is still required before capture.",
        )
        if not was_waiting:
            audit(conn, order["id"], "payer_action_required", "PayPal requires the buyer to approve this order before capture.")
        return fetch_order(conn, order["id"])

    save_order(conn, order["id"], approve_url="")

    try:
        capture = paypal.capture(paypal_order_id, request_id)
    except PayPalError:
        save_order(
            conn,
            order["id"],
            status="Incident",
            problem="paypal",
            explanation="PayPal did not capture the order. Till did not mark it complete.",
        )
        audit(conn, order["id"], "capture_failed", "Capture failed. The order is not complete.")
        return fetch_order(conn, order["id"])

    parsed = amount_minor(capture)
    if parsed is None or parsed[0] != order["currency"] or parsed[1] != int(order["payable_minor"]):
        try:
            paypal.void(paypal_order_id)
        except PayPalError:
            pass
        save_order(
            conn,
            order["id"],
            status="Incident",
            malicious=1,
            problem="tamper",
            explanation="The capture amount did not match the decision. Till did not mark it complete.",
        )
        audit(conn, order["id"], "capture_mismatch", "Capture amount mismatched. The order is not complete.")
        return fetch_order(conn, order["id"])

    with immediate(conn):
        already = _row(
            conn.execute(
                "SELECT order_id FROM captured_orders WHERE paypal_order_id = ?",
                (paypal_order_id,),
            ).fetchone()
        )
        if already is None:
            conn.execute(
                """
                INSERT INTO captured_orders (paypal_order_id, order_id, amount_minor, currency, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (paypal_order_id, order["id"], int(order["payable_minor"]), order["currency"], utcnow()),
            )
        elif already["order_id"] != order["id"]:
            save_order(
                conn,
                order["id"],
                status="Blocked",
                decision="BLOCK",
                malicious=1,
                problem="duplicate",
                explanation="This PayPal order id was already captured and cannot be paid again.",
            )
            return fetch_order(conn, order["id"])
        save_order(
            conn,
            order["id"],
            status="CaptureSubmitted",
            paypal_capture_id=capture.get("id"),
            paypal_snapshot_json=json.dumps(line),
        )
    audit(conn, order["id"], "capture_submitted", "Till captured with PayPal-Request-Id. A verified webhook still has to match.")
    return fetch_order(conn, order["id"])


def _paypal_mismatch(order: dict, listed: dict, claimed: dict, line: dict) -> str | None:
    if not line:
        return "total"
    if line.get("sku") != listed["sku"]:
        return "SKU"
    if line.get("quantity") != int(claimed["quantity"]):
        return "quantity"
    if line.get("unit_price_minor") != int(listed["unit_price_minor"]):
        return "price"
    if line.get("currency") != order["currency"] or line.get("unit_currency") != order["currency"]:
        return "currency"
    if line.get("total_minor") != int(order["payable_minor"]):
        return "total"
    if line.get("custom_id") not in {None, order["id"]}:
        return "total"
    return None


def approve_order(conn, paypal, order_id: str, shopper_id: str, *, public_base_url: str = "") -> dict:
    order = fetch_order(conn, order_id)
    if order is None or order["shopper_id"] != shopper_id:
        raise CheckoutError(404, "Order not found.")
    if order["status"] != "NeedsApproval":
        raise CheckoutError(409, "This order is not waiting for approval.")
    if order["approval_expires_at"] and order["approval_expires_at"] < utcnow():
        return _expire_approval(conn, order, malicious=False, message="The approval expired.")
    if current_approval_hash(order) != order["approval_hash"]:
        return _expire_approval(
            conn,
            order,
            malicious=True,
            message="The amount, SKU, quantity, currency, or address changed, so this approval is void.",
        )
    save_order(
        conn,
        order_id,
        status="Approved",
        decision="ALLOW",
        approval_status="approved",
        approval_source="shopper",
        approved_hash=order["approval_hash"],
    )
    audit(conn, order_id, "shopper_approved", "The shopper approved this exact payload.")
    return start_payment(conn, paypal, order_id, public_base_url=public_base_url)


def resume_paypal_approval(conn, paypal, order_id: str, paypal_order_id: str) -> dict:
    order = fetch_order(conn, order_id)
    if order is None or order.get("paypal_order_id") != paypal_order_id:
        raise CheckoutError(404, "PayPal order not found for this Till decision.")
    if order["status"] in {"CaptureSubmitted", "Complete"}:
        return order
    if order["status"] != "PayerAction":
        raise CheckoutError(409, "This Till order is not waiting for PayPal approval.")
    if order["approval_expires_at"] and order["approval_expires_at"] < utcnow():
        try:
            paypal.void(paypal_order_id)
        except PayPalError:
            pass
        return _expire_approval(conn, order, malicious=False, message="The approval expired before PayPal buyer approval.")
    if order["approved_hash"] != order["approval_hash"] or current_approval_hash(order) != order["approval_hash"]:
        try:
            paypal.void(paypal_order_id)
        except PayPalError:
            pass
        return _expire_approval(
            conn,
            order,
            malicious=True,
            message="The amount, SKU, quantity, currency, or address changed before PayPal buyer approval.",
        )
    return start_payment(conn, paypal, order_id)


def cancel_paypal_approval(conn, paypal, order_id: str, paypal_order_id: str) -> dict:
    order = fetch_order(conn, order_id)
    if order is None or order.get("paypal_order_id") != paypal_order_id:
        raise CheckoutError(404, "PayPal order not found for this Till decision.")
    if order["status"] in {"CaptureSubmitted", "Complete"}:
        return order
    if order["status"] != "PayerAction":
        raise CheckoutError(409, "This Till order is not waiting for PayPal approval.")
    try:
        paypal.void(paypal_order_id)
    except PayPalError:
        pass
    message = "The shopper canceled PayPal checkout. Till did not capture the order."
    save_order(conn, order_id, status="Rejected", approval_status="rejected", approve_url="", explanation=message)
    audit(conn, order_id, "paypal_checkout_canceled", message)
    return fetch_order(conn, order_id)


def reject_order(conn, order_id: str, shopper_id: str) -> dict:
    order = fetch_order(conn, order_id)
    if order is None or order["shopper_id"] != shopper_id:
        raise CheckoutError(404, "Order not found.")
    if order["status"] != "NeedsApproval":
        raise CheckoutError(409, "This order is not waiting for approval.")
    save_order(
        conn,
        order_id,
        status="Rejected",
        approval_status="rejected",
        explanation="The shopper rejected this order. Till did not talk to PayPal.",
    )
    audit(conn, order_id, "rejected", "The shopper rejected this order.")
    return fetch_order(conn, order_id)


def learn_rule(conn, order_id: str, shopper_id: str, kind: str, *, intent_ttl_seconds: int) -> dict:
    if kind not in {"allow_once", "allow_merchant", "shipping_toward_cap"}:
        raise CheckoutError(400, "Unknown rule.")
    with immediate(conn):
        order = fetch_order(conn, order_id)
        if order is None or order["shopper_id"] != shopper_id:
            raise CheckoutError(404, "Order not found.")
        version, policy = active_policy(conn)
        rules = dict(policy)
        rules["allowed_merchants"] = list(policy.get("allowed_merchants") or [])
        rules["rule_citations"] = list(policy.get("rule_citations") or [])
        if kind == "allow_merchant":
            if order["merchant_id"] not in rules["allowed_merchants"]:
                rules["allowed_merchants"].append(order["merchant_id"])
            rules["rule_citations"] = [
                item for item in rules["rule_citations"] if not (item.get("kind") == "allow_merchant" and item.get("merchant_id") == order["merchant_id"])
            ]
            rules["rule_citations"].append(
                {"kind": "allow_merchant", "decision_id": order_id, "merchant_id": order["merchant_id"]}
            )
        elif kind == "shipping_toward_cap":
            rules["include_shipping"] = True
            rules["shipping_counts_toward_cap"] = True
            rules["rule_citations"] = [item for item in rules["rule_citations"] if item.get("kind") != "shipping_toward_cap"]
            rules["rule_citations"].append({"kind": "shipping_toward_cap", "decision_id": order_id})
        expires = datetime.now(timezone.utc) + timedelta(seconds=intent_ttl_seconds)
        rules["intent_expires_at"] = expires.replace(microsecond=0).isoformat()
        next_version = int(conn.execute("SELECT COALESCE(MAX(version), 0) AS n FROM policies").fetchone()["n"]) + 1
        conn.execute("UPDATE policies SET active = 0 WHERE active = 1")
        conn.execute(
            "INSERT INTO policies (version, raw_text, rules_json, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (next_version, rules.get("raw") or "", json.dumps(rules), utcnow()),
        )
        conn.execute(
            """
            INSERT INTO learned_rules (
              policy_version, kind, source_order_id, merchant_id, approval_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                next_version,
                kind,
                order_id,
                order["merchant_id"] if kind == "allow_merchant" else None,
                order["approval_hash"] if kind == "allow_once" else None,
                utcnow(),
            ),
        )
        audit(conn, order_id, "learned", f"The shopper added a {kind} rule from this decision.", policy_version=next_version)
    return fetch_order(conn, order_id)


def save_policy(conn, sentence: str, rules: dict, *, intent_ttl_seconds: int) -> dict:
    with immediate(conn):
        current = conn.execute(
            "SELECT rules_json FROM policies WHERE active = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if current is not None:
            previous = json.loads(current["rules_json"])
            rules.setdefault("allowed_merchants", list(previous.get("allowed_merchants") or []))
            rules.setdefault("rule_citations", list(previous.get("rule_citations") or []))
            if "include_shipping" not in rules:
                rules["include_shipping"] = bool(previous.get("include_shipping", True))
        expires = datetime.now(timezone.utc) + timedelta(seconds=intent_ttl_seconds)
        rules["intent_expires_at"] = expires.replace(microsecond=0).isoformat()
        rules["raw"] = sentence
        next_version = int(conn.execute("SELECT COALESCE(MAX(version), 0) AS n FROM policies").fetchone()["n"]) + 1
        conn.execute("UPDATE policies SET active = 0 WHERE active = 1")
        conn.execute(
            "INSERT INTO policies (version, raw_text, rules_json, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (next_version, sentence, json.dumps(rules), utcnow()),
        )
    return rules


def remove_allowed_merchant(conn, merchant_id: str, *, intent_ttl_seconds: int) -> bool:
    _, rules = active_policy(conn)
    allowed = list(rules.get("allowed_merchants") or [])
    if merchant_id not in allowed:
        return False
    rules["allowed_merchants"] = [item for item in allowed if item != merchant_id]
    rules["rule_citations"] = [
        item
        for item in (rules.get("rule_citations") or [])
        if not (item.get("kind") == "allow_merchant" and item.get("merchant_id") == merchant_id)
    ]
    save_policy(conn, rules.get("raw") or "", rules, intent_ttl_seconds=intent_ttl_seconds)
    audit(conn, None, "merchant_removed", "The shopper removed a merchant ask-above exception.", merchant_id=merchant_id)
    return True


def apply_webhook(conn, paypal, event: dict, *, verified: bool) -> dict:
    if not verified:
        raise CheckoutError(400, "Unverified webhook.")
    event_id = event.get("id")
    event_type = event.get("event_type") or ""
    if not event_id or not isinstance(event_id, str):
        raise CheckoutError(400, "Webhook event id is required.")
    resource = event.get("resource") or {}
    void_id = None
    with immediate(conn):
        existing = conn.execute("SELECT event_id FROM webhook_events WHERE event_id = ?", (event_id,)).fetchone()
        if existing is not None:
            return {"status": "duplicate", "event_id": event_id}
        outcome = _apply_event(conn, event_type, resource)
        void_id = outcome.pop("void_id", None)
        conn.execute(
            "INSERT INTO webhook_events (event_id, event_type, order_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, event_type, outcome.get("order_id"), json.dumps(event), utcnow()),
        )
        result = outcome
    if void_id:
        try:
            paypal.void(void_id)
            if result.get("order_id"):
                audit(conn, result["order_id"], "voided", "Till voided the PayPal order after the webhook.")
        except PayPalError:
            if result.get("order_id"):
                audit(conn, result["order_id"], "void_failed", "Till tried to void the PayPal order and PayPal refused.")
    return result


def _apply_event(conn, event_type: str, resource: dict) -> dict:
    custom_id = custom_id_of(resource)
    paypal_order_id = paypal_order_id_of(resource)
    order = fetch_order(conn, custom_id) if custom_id else None
    if event_type == "CHECKOUT.ORDER.APPROVED":
        if order is not None:
            audit(conn, order["id"], "paypal_approved", "PayPal reported CHECKOUT.ORDER.APPROVED. That is not completion.")
        return {"status": "noted", "order_id": order["id"] if order else None, "complete": False}
    if event_type == "PAYMENT.CAPTURE.DENIED":
        if order is not None and order["status"] != "Complete":
            save_order(
                conn,
                order["id"],
                status="Incident",
                problem="capture_denied",
                malicious=0,
                explanation="PayPal denied the capture. Till did not mark the order complete.",
            )
            audit(conn, order["id"], "capture_denied", "PayPal denied the capture.")
        return {"status": "denied", "order_id": order["id"] if order else None, "complete": False}
    if event_type != "PAYMENT.CAPTURE.COMPLETED":
        return {"status": "ignored", "order_id": order["id"] if order else None, "complete": False}

    parsed = amount_minor(resource)
    approved = (
        order is not None
        and order["decision"] == "ALLOW"
        and order["approval_status"] == "approved"
        and order["status"] in OPEN_STATES
    )
    if not approved or parsed is None:
        return _incident(
            conn,
            order,
            paypal_order_id,
            "A capture arrived with no approved decision. An order id alone is not completion.",
        )
    currency, minor = parsed
    if currency != order["currency"] or minor != int(order["payable_minor"]):
        return _incident(
            conn,
            order,
            paypal_order_id or order.get("paypal_order_id"),
            "The captured currency or amount does not match the decision.",
        )
    save_order(conn, order["id"], status="Complete", paypal_capture_id=resource.get("id") or order.get("paypal_capture_id"))
    audit(conn, order["id"], "complete", "A verified capture webhook matched the currency and amount.")
    return {"status": "complete", "order_id": order["id"], "complete": True}


def _incident(conn, order: dict | None, paypal_order_id: str | None, message: str) -> dict:
    void_id = None
    order_id = None
    if order is not None:
        order_id = order["id"]
        if order["status"] in OPEN_STATES:
            void_id = paypal_order_id or order.get("paypal_order_id")
        if order["status"] != "Complete":
            save_order(
                conn,
                order["id"],
                status="Incident",
                malicious=1,
                problem="webhook",
                explanation=message,
            )
        audit(conn, order["id"], "incident", message)
    elif paypal_order_id:
        linked = _row(
            conn.execute(
                "SELECT * FROM orders WHERE paypal_order_id = ? ORDER BY created_at DESC LIMIT 1",
                (paypal_order_id,),
            ).fetchone()
        )
        if linked is not None and linked["status"] in OPEN_STATES:
            order_id = linked["id"]
            void_id = paypal_order_id
            save_order(
                conn,
                linked["id"],
                status="Incident",
                malicious=1,
                problem="webhook",
                explanation=message,
            )
            audit(conn, linked["id"], "incident", message)
        else:
            void_id = paypal_order_id
            audit(conn, None, "incident", message, paypal_order_id=paypal_order_id)
    else:
        audit(conn, None, "incident", message)
    return {"status": "incident", "order_id": order_id, "complete": False, "void_id": void_id}


def list_orders(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC, id DESC").fetchall()
    return [present_order(conn, dict(row)) for row in rows]


def present_order(conn, order: dict) -> dict:
    claimed = json.loads(order["claimed_json"])
    listed = json.loads(order["listed_json"]) if order["listed_json"] else None
    policy = json.loads(order["policy_snapshot_json"])
    paypal = json.loads(order["paypal_snapshot_json"]) if order["paypal_snapshot_json"] else None
    reasons = json.loads(order["reasons_json"])
    timeline = audits_for(conn, order["id"]) if conn is not None else []
    ignored = any(item["event_type"] == "agent_ignored" for item in timeline)
    if order["malicious"]:
        kind = "malicious"
    elif order["decision"] == "BLOCK":
        kind = "policy"
    else:
        kind = ""
    success = order["status"] == "Complete"
    return {
        "id": order["id"],
        "status": order["status"],
        "decision": order["decision"],
        "malicious": bool(order["malicious"]),
        "kind": kind,
        "problem": order["problem"],
        "explanation": order["explanation"],
        "citation": order["citation"],
        "approval_status": order["approval_status"],
        "approval_source": order["approval_source"],
        "payable_minor": order["payable_minor"],
        "capped_minor": order["capped_minor"],
        "currency": order["currency"],
        "address": order["address"],
        "paypal_order_id": order["paypal_order_id"],
        "approve_url": order.get("approve_url"),
        "policy_version": order["policy_version"],
        "merchant_id": order["merchant_id"],
        "sku": order["sku"],
        "created_at": order["created_at"],
        "success": success,
        "reasons": reasons,
        "claimed": claimed,
        "listed": listed,
        "policy": policy,
        "paypal": paypal,
        "timeline": timeline,
        "agent_fields_ignored": ignored,
        "item_minor": int(claimed["unit_price_minor"]) * int(claimed["quantity"]),
        "tax_minor": int(claimed["tax_minor"]),
        "shipping_minor": int(claimed["shipping_minor"]),
        "addons_minor": int(claimed["addons_minor"]),
    }


def agent_view(order: dict) -> dict:
    view = present_order(None, order) if "claimed" not in order else order
    if "reasons" not in view:
        view = present_order(None, order)
    return {
        "id": view["id"],
        "status": view["status"],
        "decision": view["decision"],
        "malicious": view["malicious"],
        "kind": view["kind"],
        "problem": view["problem"],
        "explanation": view["explanation"],
        "citation": view["citation"],
        "approval_status": view["approval_status"],
        "payable_minor": view["payable_minor"],
        "currency": view["currency"],
        "paypal_order_id": view["paypal_order_id"],
        "approve_url": view["approve_url"],
        "policy_version": view["policy_version"],
        "success": view["success"],
        "reasons": [{"code": reason["code"], "message": reason["message"]} for reason in view["reasons"]],
    }


def open_db(path: str):
    return connect(path)
