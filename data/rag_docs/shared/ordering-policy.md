---
title: Purchase Ordering Policy
doc_type: ordering_policy
version: "2.0"
effective_date: 2026-07-01
source_uri: policies/ordering-policy.md
owner: Procurement Operations
---
# Purchase Ordering Policy

## 1. Scope
This policy governs every replenishment purchase order raised for stores and distribution centers (DCs),
whether created manually, from a system recommendation, or through the ordering assistant.

## 2. Order creation
- Every order must reference an approved vendor and an active SKU.
- Orders created from a replenishment recommendation must keep the recommended vendor unless a
  buyer documents an exception reason.
- Quantities must be rounded up to the vendor case pack (minimum order quantity).
- The ordering assistant may **propose** orders but must obtain explicit human confirmation before
  creating an order or initiating a payment.

## 3. Approval thresholds
- Orders with a total value **up to the tenant approval threshold** may be confirmed by the buyer who
  requested them.
- Orders **above the approval threshold** require confirmation by a user holding the *approver* role, and
  the approver must be a different person from the requester (four-eyes principle).
- Unless a tenant addendum states otherwise, the default approval threshold is **USD 10,000** per order.

## 4. Changes and cancellations
- An order may be modified (quantity or delivery date) only while it is unpaid.
- Cancelling an order after payment capture triggers a refund and must include a cancellation reason.
- All changes are recorded in the audit trail with the acting user and agent.

## 5. Duplicate orders
- Buyers must check for open orders on the same SKU and location before ordering.
- Systems must use idempotency keys so that retried requests never create duplicate orders.

## 6. Record keeping
Order, payment and notification records in the ordering system are the system of record. Chat transcripts
are not authoritative for order status.
