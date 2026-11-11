"""Deterministic checkout checks. The model cannot loosen a BLOCK."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .money import from_minor


def payload_hash(canonical: dict) -> str:
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def approval_hash(*, amount: int, sku: str, quantity: int, currency: str, address: str) -> str:
    """Hash of the payload a person approved. Any change makes a different hash."""
    material = "|".join(
        [
            str(int(amount)),
            sku.strip(),
            str(int(quantity)),
            currency.strip().upper(),
            address.strip(),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def canonical_order(
    *,
    merchant_id: str,
    sku: str,
    title: str,
    quantity: int,
    unit_price_minor: int,
    tax_minor: int,
    shipping_minor: int,
    addons_minor: int,
    currency: str,
) -> dict:
    return {
        "addons_minor": addons_minor,
        "currency": currency,
        "merchant_id": merchant_id,
        "quantity": quantity,
        "shipping_minor": shipping_minor,
        "sku": sku,
        "tax_minor": tax_minor,
        "title": title,
        "unit_price_minor": unit_price_minor,
    }


def claim_fingerprint(claim: dict) -> str:
    return payload_hash(
        {
            "address": claim["address"].strip(),
            "addons_minor": int(claim["addons_minor"]),
            "currency": claim["currency"].strip().upper(),
            "merchant_id": claim["merchant_id"],
            "quantity": int(claim["quantity"]),
            "shipping_minor": int(claim["shipping_minor"]),
            "sku": claim["sku"],
            "tax_minor": int(claim["tax_minor"]),
            "title": claim["title"].strip(),
            "total_minor": int(claim["total_minor"]),
            "unit_price_minor": int(claim["unit_price_minor"]),
        }
    )


@dataclass
class Verdict:
    decision: str
    malicious: bool
    problem: str | None
    reasons: list[dict] = field(default_factory=list)
    explanation: str = ""
    capped_minor: int = 0
    payable_minor: int = 0
    canonical: dict = field(default_factory=dict)
    payload_hash: str = ""
    citation: str | None = None
    ask_wait: bool = False

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "malicious": self.malicious,
            "problem": self.problem,
            "reasons": self.reasons,
            "explanation": self.explanation,
            "capped_minor": self.capped_minor,
            "payable_minor": self.payable_minor,
            "canonical": self.canonical,
            "payload_hash": self.payload_hash,
            "citation": self.citation,
            "ask_wait": self.ask_wait,
        }


def apply_model(verdict: Verdict, model_decision: str | None) -> Verdict:
    """A model may tighten ALLOW to BLOCK. It cannot turn BLOCK into ALLOW."""
    if verdict.decision == "BLOCK":
        if model_decision == "ALLOW":
            verdict.explanation += " The model suggested allowing this. The hard rule still blocks it."
        return verdict
    if model_decision == "BLOCK" and verdict.decision == "ALLOW":
        verdict.decision = "BLOCK"
        verdict.problem = verdict.problem or "needs"
        verdict.malicious = False
        verdict.reasons.append(
            {
                "code": "model_stricter",
                "message": "The model would only make this stricter, so Till will not capture it.",
                "hard": False,
            }
        )
        verdict.explanation = "The model marked this order as not a fit. Till will not capture it."
    return verdict


def evaluate(
    *,
    policy: dict,
    claimed: dict,
    listed: dict | None,
    spent_today_minor: int,
    spent_category_minor: int = 0,
    duplicate: bool,
    merchant_citation: str | None = None,
    shipping_citation: str | None = None,
) -> Verdict:
    reasons: list[dict] = []
    quantity = int(claimed["quantity"])
    unit = int(claimed["unit_price_minor"])
    tax = int(claimed.get("tax_minor") or 0)
    shipping = int(claimed.get("shipping_minor") or 0)
    addons = int(claimed.get("addons_minor") or 0)
    currency = (claimed.get("currency") or "USD").upper()

    listed_unit = int(listed["unit_price_minor"]) if listed else unit
    listed_tax = int(listed.get("tax_minor") or 0) if listed else tax
    listed_shipping = int(listed.get("shipping_minor") or 0) if listed else shipping
    listed_addons = int(listed.get("addons_minor") or 0) if listed else addons
    listed_title = listed["title"] if listed else claimed.get("title") or ""
    listed_currency = listed["currency"] if listed else currency
    merchant_id = listed["merchant_id"] if listed else claimed["merchant_id"]
    sku = listed["sku"] if listed else claimed["sku"]

    # Full total: quantity * unit + tax + shipping + add-ons.
    payable = listed_unit * quantity + listed_tax + listed_shipping + listed_addons
    capped = payable

    canonical = canonical_order(
        merchant_id=merchant_id,
        sku=sku,
        title=listed_title,
        quantity=quantity,
        unit_price_minor=listed_unit,
        tax_minor=listed_tax,
        shipping_minor=listed_shipping,
        addons_minor=listed_addons,
        currency=listed_currency,
    )

    malicious = False
    problem = None

    def add(code: str, message: str, *, hard: bool, kind: str, is_malicious: bool = False) -> None:
        nonlocal malicious, problem
        reasons.append({"code": code, "message": message, "hard": hard})
        if is_malicious:
            malicious = True
        if problem is None or (is_malicious and problem not in {"tamper", "duplicate"}):
            problem = kind

    if listed is None:
        add(
            "unknown_item",
            "The catalog has no such merchant item. Till will not pay an arbitrary payee.",
            hard=True,
            kind="tamper",
            is_malicious=True,
        )
    else:
        if claimed["merchant_id"] != listed["merchant_id"] or claimed["sku"] != listed["sku"]:
            add(
                "sku_swap",
                "The item in the proposal is not the catalog item Till looked up.",
                hard=True,
                kind="tamper",
                is_malicious=True,
            )
        if claimed.get("title", "").strip().lower() != listed["title"].strip().lower():
            add(
                "sku_swap",
                f"The agent calls this {claimed.get('title')!r}, but the listing is {listed['title']!r}.",
                hard=True,
                kind="tamper",
                is_malicious=True,
            )
        if unit != listed_unit or tax != listed_tax or shipping != listed_shipping or addons != listed_addons:
            add(
                "price_mismatch",
                "The price, tax, shipping, or add-ons in the proposal do not match the listing Till snapshotted.",
                hard=True,
                kind="tamper",
                is_malicious=True,
            )
        if currency != listed_currency:
            add(
                "currency_mismatch",
                f"The proposal is {currency}, but the listing is {listed_currency}.",
                hard=True,
                kind="tamper",
                is_malicious=True,
            )
        if quantity != int(policy.get("intent_quantity") or 1):
            add(
                "quantity_mismatch",
                f"The shopper asked for {policy.get('intent_quantity') or 1}. The agent submitted {quantity}.",
                hard=True,
                kind="tamper",
                is_malicious=True,
            )

    claimed_total = claimed.get("total_minor")
    computed_claim = unit * quantity + tax + shipping + addons
    if claimed_total is not None and int(claimed_total) != computed_claim:
        add(
            "arithmetic",
            "Quantity times unit price, plus tax, shipping, and add-ons, does not equal the total.",
            hard=True,
            kind="arithmetic",
        )

    if duplicate:
        add(
            "duplicate_window",
            "This shopper already has a payment for this merchant and SKU inside the time window.",
            hard=True,
            kind="duplicate",
            is_malicious=True,
        )

    max_purchase = int(policy["max_purchase_minor"])
    daily_max = int(policy["daily_max_minor"])
    category_max = int(policy.get("category_max_minor") or daily_max)
    if capped > max_purchase:
        add(
            "over_purchase",
            f"The full total {from_minor(capped)} exceeds the per-purchase cap {from_minor(max_purchase)}.",
            hard=True,
            kind="budget",
        )
    if spent_today_minor + capped > daily_max:
        add(
            "over_daily",
            f"Today's spend plus this full total exceeds the daily cap {from_minor(daily_max)}.",
            hard=True,
            kind="budget",
        )
    if spent_category_minor + capped > category_max:
        add(
            "over_category",
            f"Today's category spend plus this full total exceeds the category cap {from_minor(category_max)}.",
            hard=True,
            kind="budget",
        )

    ask_above = policy.get("ask_above_minor")
    merchant_allowed = merchant_id in set(policy.get("allowed_merchants") or [])
    hard_block = malicious or any(reason["hard"] for reason in reasons)
    ask_wait = (
        not hard_block
        and ask_above is not None
        and capped > int(ask_above)
        and not merchant_allowed
    )
    if ask_wait:
        reasons.append(
            {
                "code": "ask_threshold",
                "message": f"The full total {from_minor(capped)} is above the ask-me line {from_minor(int(ask_above))}.",
                "hard": False,
            }
        )

    if hard_block:
        decision = "BLOCK"
    elif ask_wait:
        decision = "NEED_APPROVAL"
        if problem is None:
            problem = "approval"
    else:
        decision = "ALLOW"
        problem = None

    bits = [reason["message"] for reason in reasons if reason["hard"] or decision != "ALLOW"]
    if decision == "ALLOW":
        explanation = (
            f"The full total {from_minor(payable)} {listed_currency if listed else currency} is inside the caps."
        )
    elif decision == "NEED_APPROVAL":
        explanation = "A person needs to approve this exact amount, SKU, quantity, currency, and address."
        if bits:
            explanation = " ".join(bits)
    else:
        explanation = " ".join(bits) if bits else "Till blocked this order."

    citation_bits = []
    if merchant_allowed and merchant_citation:
        citation_bits.append(merchant_citation)
    if shipping_citation:
        citation_bits.append(shipping_citation)
    citation = " ".join(citation_bits) or None
    if citation:
        explanation = f"{explanation} {citation}".strip()

    return Verdict(
        decision=decision,
        malicious=malicious,
        problem=problem,
        reasons=reasons,
        explanation=explanation,
        capped_minor=capped,
        payable_minor=payable,
        canonical=canonical,
        payload_hash=payload_hash(canonical),
        citation=citation,
        ask_wait=ask_wait,
    )
