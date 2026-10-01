"""Intent contract and deterministic rule-based classifier.

The LLM (route ``intent_classification``) is the primary classifier; these rules are its
deterministic fallback (used by the mock provider and whenever every provider is down), and a
cross-check for entity extraction - entities are always validated against strict patterns.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

IntentName = Literal[
    "get_recommendations", "explain_recommendation", "investigate_risk", "policy_question", "propose_order",
    "confirm", "reject", "order_status", "retry_payment", "cancel_order", "greeting", "unsupported",
]


class Entities(BaseModel):
    sku: str | None = None
    location_code: str | None = None
    quantity: int | None = None
    order_number: str | None = None
    payment_token: str | None = None
    recommendation_index: int | None = None
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    category: str | None = None


class Intent(BaseModel):
    intent: IntentName
    confidence: float = Field(..., ge=0, le=1)
    entities: Entities = Field(default_factory=Entities)


SKU_RE = re.compile(r"\b((?:SKU|GLX)-\d{4,6})\b", re.I)
LOC_RE = re.compile(r"\b(STORE-G?\d{2,3}|DC-G?\d{2})\b", re.I)
ORDER_RE = re.compile(r"\b([A-Z]{2,4}-\d{6,12})\b")
TOKEN_RE = re.compile(r"\b(tok_[a-z_]{2,32})\b")
QTY_RE = re.compile(r"\b(\d{1,6})\s*(?:units?|pcs|pieces|cases?|x\b)|\b(?:qty|quantity|order)\s*(?:of\s*)?(\d{1,6})\b", re.I)
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
            "top one": 1, "top item": 1}
CATEGORIES = ["dairy", "produce", "bakery", "beverages", "frozen", "household", "personal care", "snacks", "pantry", "meat"]

_RULES: list[tuple[IntentName, re.Pattern[str]]] = [
    ("retry_payment", re.compile(r"\b(retry|try again|re-?try|reattempt)\b.*\b(pay|payment|charge)|\bpay again\b", re.I)),
    ("cancel_order", re.compile(r"\bcancel\b.*\b(order|it|po)\b|\bcancel\s+[A-Z]{2,4}-\d+", re.I)),
    ("confirm", re.compile(r"^\s*(please\s+|ok\s*,?\s*)?(yes|yep|yeah|confirm(ed)?|approve[d]?|go ahead|proceed|do it|place (the|it|order)|sounds good|ok(ay)?)\b", re.I)),
    ("reject", re.compile(r"^\s*(no|nope|reject|decline|don'?t|do not|stop|abort)\b", re.I)),
    ("get_recommendations", re.compile(r"\bwhat (should|do) (i|we) (need to )?(order|buy|replenish)\b", re.I)),
    ("order_status", re.compile(r"\b(status|where is|track|verify|check)\b.*\b(order|payment|po|notification|it)\b|\b[A-Z]{2,4}-\d{6,12}\b", re.I)),
    ("propose_order", re.compile(r"\b(order|buy|purchase|replenish|raise a po for)\b.*\b(it|this|that|them|sku-\d+|glx-\d+|first|second|third|top|recommendation|\d+\s*(units?|cases?|pcs))\b", re.I)),
    ("explain_recommendation", re.compile(r"\b(why|explain|reason|how come|justify|what drives|tell me more)\b", re.I)),
    ("get_recommendations", re.compile(r"\b(recommend\w*|suggest\w*|replenishment needs?)\b", re.I)),
    ("investigate_risk", re.compile(r"\b(risk|stock-?out|running (low|out)|below safety|low stock|out of stock|shortage)\b", re.I)),
    ("policy_question", re.compile(r"\b(policy|policies|rule|rules|threshold|allowed|can i|may i|must|required|guideline|lead time|cut-?off|payment terms|approval|refund|procedure|how many|what happens|preferred vendor|temperature|case packs?)\b", re.I)),
    ("greeting", re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b", re.I)),
]


def extract_entities(text: str) -> Entities:
    e = Entities()
    if m := SKU_RE.search(text):
        e.sku = m.group(1).upper()
    if m := LOC_RE.search(text):
        e.location_code = m.group(1).upper()
    if m := ORDER_RE.search(text):
        if not SKU_RE.fullmatch(m.group(1)) and not m.group(1).upper().startswith(("SKU-", "GLX-")):
            e.order_number = m.group(1)
    if m := TOKEN_RE.search(text):
        e.payment_token = m.group(1)
    if m := QTY_RE.search(text):
        qty = int(m.group(1) or m.group(2))
        if 0 < qty <= 100_000:
            e.quantity = qty
    lowered = text.lower()
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", lowered):
            e.recommendation_index = idx
            break
    if m := re.search(r"\b(critical|high|medium|low)\b(?:[- ]risk)?", lowered):
        if "risk" in lowered or m.group(1) == "critical":
            e.risk_level = m.group(1)  # type: ignore[assignment]
    for cat in CATEGORIES:
        if cat in lowered:
            e.category = cat.title()
            break
    return e


def classify_rules(text: str, has_pending_approval: bool = False, has_recommendations: bool = False) -> Intent:
    entities = extract_entities(text)
    for intent, pattern in _RULES:
        if pattern.search(text):
            if intent in ("confirm", "reject") and not has_pending_approval:
                continue
            if intent == "order_status" and entities.sku and not entities.order_number:
                continue
            if intent == "propose_order" and not (entities.sku or entities.recommendation_index or has_recommendations):
                continue
            return Intent(intent=intent, confidence=0.75, entities=entities)
    if entities.sku and has_recommendations:
        return Intent(intent="explain_recommendation", confidence=0.5, entities=entities)
    return Intent(intent="unsupported", confidence=0.3, entities=entities)
