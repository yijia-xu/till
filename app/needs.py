"""Needs judgment uses Gemini.

The model says fit, unfit, or unsure, and writes the explanation.
It cannot turn a BLOCK into an ALLOW, and it cannot call PayPal.
If the model is not configured or the call fails, the needs check waits
for a person. Hard blocks stay blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .engine import Verdict
from .gemini import GeminiAPIError, generate_json
from .money import to_minor


class NeedsUnavailable(Exception):
    pass


@dataclass
class FrozenNeeds:
    category: str
    size: str | None
    brand: str | None
    exclusions: list[str]
    max_purchase_minor: int | None


def parse_sentence(sentence: str) -> FrozenNeeds:
    lower = " ".join(sentence.lower().split())
    exclusions = []
    for match in re.finditer(r"\bno\s+([a-z][a-z0-9 -]{0,40}?)(?=,|$)", lower):
        word = match.group(1).strip()
        if word:
            exclusions.append(word)
    size = None
    size_match = re.search(r"\bsize\s+(\d+(?:\.\d+)?)", lower)
    if size_match:
        size = size_match.group(1)
    brand = None
    brand_match = re.search(r"\bbrand\s+([a-z0-9]+)", lower)
    if brand_match:
        brand = brand_match.group(1)
    money = None
    money_match = re.search(r"\b(?:at most|under|up to|<=)\s+\$(\d+(?:\.\d{1,2})?)", lower)
    if money_match:
        raw = money_match.group(1)
        money = to_minor(raw if "." in raw else raw + ".00")
    first = lower.split(",")[0].strip()
    first = re.sub(r"^(please\s+)?(buy|get|find|order)\s+", "", first)
    category = first.replace(" ", "_")
    if category.startswith(("size_", "at_most", "under_", "no_", "brand_")):
        category = ""
    return FrozenNeeds(
        category=category,
        size=size,
        brand=brand,
        exclusions=exclusions,
        max_purchase_minor=money,
    )


class ModelNeeds:
    """HTTP needs judge. This client never calls PayPal."""

    def __init__(self, url: str, api_key: str, model: str):
        self.url = url
        self.api_key = api_key
        self.model = model

    def judge(self, policy: dict, claimed: dict, listed: dict | None) -> tuple[str, str]:
        if not self.url or not self.api_key:
            raise NeedsUnavailable("The needs model is not configured.")
        prompt = {
            "sentence": policy.get("raw") or policy.get("sentence"),
            "category": policy.get("category"),
            "size": policy.get("size"),
            "brand": policy.get("brand"),
            "exclusions": policy.get("exclusions"),
            "claimed_title": claimed.get("title"),
            "claimed_sku": claimed.get("sku"),
            "listing": None
            if listed is None
            else {
                "title": listed.get("title"),
                "sku": listed.get("sku"),
                "category": listed.get("category"),
                "size": listed.get("size"),
                "brand": listed.get("brand"),
            },
        }
        try:
            parsed = generate_json(
                api_key=self.api_key,
                base_url=self.url,
                model=self.model,
                system_instruction=(
                    "Judge whether the listing fits the shopper need. "
                    'Return JSON only: {"judgment":"fit|unfit|unsure","explanation":"short"}. '
                    "You cannot approve a payment, lift a hard block, or call PayPal."
                ),
                payload=prompt,
                timeout=15.0,
            )
        except GeminiAPIError as exc:
            raise NeedsUnavailable("The needs model is unavailable.") from exc
        raw = str(parsed.get("judgment") or "").strip().lower()
        judgment = {"fit": "fit", "unfit": "unfit", "unsure": "unsure"}.get(raw, "unsure")
        explanation = str(parsed.get("explanation") or "The model did not explain the needs check.")
        return judgment, explanation


def combine_needs(verdict: Verdict, judgment: str, note: str) -> Verdict:
    """Apply a needs judgment without ever turning BLOCK into ALLOW."""
    if verdict.decision == "BLOCK":
        if judgment == "fit":
            verdict.explanation += " A needs check would allow this. The hard rule still blocks it."
        return verdict
    if judgment == "unfit":
        verdict.decision = "BLOCK"
        verdict.problem = "needs"
        verdict.malicious = False
        verdict.reasons.append(
            {
                "code": "needs_model",
                "message": note or "The item does not fit the shopper's request.",
                "hard": True,
            }
        )
        verdict.explanation = note or verdict.explanation
        return verdict
    if judgment == "fit" and verdict.decision == "ALLOW":
        if note:
            verdict.explanation = f"{verdict.explanation} {note}".strip()
        return verdict
    if judgment in {"unsure", "unavailable"}:
        verdict.decision = "NEED_APPROVAL"
        verdict.problem = "needs"
        verdict.explanation = note or "The needs check is uncertain, so Till waits for a person."
        verdict.reasons.append(
            {
                "code": "needs_uncertain" if judgment == "unsure" else "needs_unavailable",
                "message": verdict.explanation,
                "hard": False,
            }
        )
        return verdict
    if verdict.ask_wait:
        verdict.decision = "NEED_APPROVAL"
        verdict.problem = "approval"
        return verdict
    verdict.decision = "ALLOW"
    verdict.problem = None
    return verdict


def build_needs(api_key: str, url: str, model: str):
    endpoint = url or "https://generativelanguage.googleapis.com/v1beta/models"
    return ModelNeeds(endpoint, api_key, model or "gemini-3.5-flash-lite")
