from decimal import Decimal
from uuid import uuid4

import pytest

from ordering.agents.contracts import OrderProposal
from ordering.agents.supervisor.intents import Intent, classify_rules, extract_entities
from ordering.llm_client.client import strict_json_schema
from ordering.repositories.approvals import binding_hash


@pytest.mark.parametrize("text,pending,has_recs,expected", [
    ("What should I order for STORE-014?", False, False, "get_recommendations"),
    ("Show me critical recommendations", False, False, "get_recommendations"),
    ("Why is SKU-10042 recommended at STORE-014?", False, True, "explain_recommendation"),
    ("Which items are at risk of stock-out at DC-02?", False, False, "investigate_risk"),
    ("What is the approval threshold?", False, False, "policy_question"),
    ("Order the first one", False, True, "propose_order"),
    ("Order 360 units of SKU-10042", False, True, "propose_order"),
    ("yes, go ahead", True, True, "confirm"),
    ("yes", False, True, "unsupported"),
    ("no, cancel that", True, True, "reject"),
    ("status of AR-200001", False, False, "order_status"),
    ("retry payment with tok_mastercard", False, False, "retry_payment"),
    ("cancel order AR-200001", False, False, "cancel_order"),
    ("hello", False, False, "greeting"),
    ("write me a poem about cats", False, False, "unsupported"),
])
def test_rule_classifier(text, pending, has_recs, expected):
    assert classify_rules(text, pending, has_recs).intent == expected


def test_entity_extraction_is_strict():
    e = extract_entities("order 360 units of sku-10042 at store-014, pay with tok_visa, the first one")
    assert (e.sku, e.location_code, e.quantity, e.payment_token, e.recommendation_index) == (
        "SKU-10042", "STORE-014", 360, "tok_visa", 1)
    assert extract_entities("status of AR-200001").order_number == "AR-200001"


def _proposal(qty=360):
    return OrderProposal(recommendation_id=uuid4(), sku="SKU-10042", product_name="Milk", location_code="STORE-014",
                         vendor_code="VND-003", vendor_name="Hill Country Dairy", quantity=qty, recommended_qty=360,
                         unit_cost=Decimal("3.85"), total=Decimal("3.85") * qty, expected_delivery_date="2026-10-03",
                         risk_level="critical", rationale="low stock")


def test_approval_binding_detects_parameter_tampering():
    p = _proposal()
    payload = p.approval_payload()
    h = binding_hash("create_order", payload)
    assert h == binding_hash("create_order", {**payload, "unit_cost": "0.01", "sku": "OTHER"})  # display-only fields
    assert h != binding_hash("create_order", {**payload, "quantity": 3600})
    assert h != binding_hash("create_order", {**payload, "recommendation_id": str(uuid4())})
    assert h != binding_hash("process_payment", payload)


def test_strict_schema_for_structured_outputs():
    schema = strict_json_schema(Intent)
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    entities = schema["properties"]["entities"]
    assert entities["additionalProperties"] is False and "$ref" not in str(schema)
