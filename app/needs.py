"""Needs parsing. A model may judge fit and write an explanation.

It cannot turn a BLOCK into an ALLOW, and it cannot call PayPal.
When no API key is set, the deterministic matcher is the needs check.
If a configured model is unavailable, the caller turns the needs check
into NEED_APPROVAL while hard blocks stay blocks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .engine import Verdict
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


def _blob(listed: dict | None, claimed: dict | None) -> str:
    parts: list[str] = []
    for source in (listed, claimed):
        if not source:
            continue
        for key in ("title", "sku", "category", "brand"):
            value = source.get(key)
            if value:
                parts.append(str(value))
    return " ".join(parts).lower()


class DeterministicNeeds:
    """Used when NEEDS_MODEL_API_KEY is empty. No network. No PayPal."""

    def judge(self, policy: dict, claimed: dict, listed: dict | None) -> tuple[str, str]:
        if listed is None:
            return "unfit", "The catalog has no listing to compare with the request."
        category = (policy.get("category") or "").strip().lower().replace(" ", "_")
        if not category:
            return "unsure", "The request does not name a category Till can freeze."
        listed_category = str(listed.get("category") or "").strip().lower().replace(" ", "_")
        title = str(listed.get("title") or "").lower()
        if not listed_category:
            return "unsure", "The listing has no category, so the needs check waits."
        if listed_category != category and category not in title.replace(" ", "_"):
            asked = category.replace("_", " ")
            found = listed_category.replace("_", " ")
            return "unfit", f"The shopper asked for {asked}. The listing is {found}."
        size = policy.get("size")
        if size:
            listed_size = listed.get("size")
            if not listed_size:
                return "unsure", f"The shopper asked for size {size}, and the listing has no size."
            if str(listed_size).strip() != str(size).strip():
                return "unfit", f"The shopper asked for size {size}. The listing is size {listed_size}."
        brand = policy.get("brand")
        if brand:
            listed_brand = listed.get("brand")
            if not listed_brand:
                return "unsure", f"The shopper asked for brand {brand}, and the listing has no brand."
            if str(listed_brand).strip().lower() != str(brand).strip().lower():
                return "unfit", f"The shopper asked for brand {brand}. The listing is {listed_brand}."
        exclusions = [str(item).lower() for item in (policy.get("exclusions") or [])]
        if policy.get("no_subscription") and "subscription" not in exclusions:
            exclusions.append("subscription")
        blob = _blob(listed, claimed)
        for word in exclusions:
            if word and (word in blob or (word == "subscription" and listed.get("subscription"))):
                return "unfit", f"The shopper excluded {word}, and this listing matches that exclusion."
        return "fit", "The listing matches the frozen category, size, brand, and exclusions."


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
            response = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Judge whether the listing fits the shopper need. "
                                "Reply with JSON only: "
                                '{"judgment":"fit|unfit|unsure","explanation":"short"}. '
                                "You cannot approve a payment, lift a hard block, or call PayPal."
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt)},
                    ],
                },
                timeout=8.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception as exc:
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
    if api_key:
        return ModelNeeds(url, api_key, model)
    return DeterministicNeeds()
