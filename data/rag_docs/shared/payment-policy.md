---
title: Supplier Payment Policy
doc_type: payment_policy
version: "1.3"
effective_date: 2026-02-01
owner: Finance - Accounts Payable
---
# Supplier Payment Policy

## Payment initiation
Payments for purchase orders are initiated only after the order has been confirmed by an authorized user.
The ordering assistant must never initiate a payment without explicit confirmation of the amount and the
payment method.

## Payment limits
Each tenant has a per-order payment limit. Orders exceeding the payment limit cannot be paid automatically
and must be split or paid by Accounts Payable through the treasury process.

## Failed payments
- When a payment is declined, the order is marked *payment failed* and no goods are released.
- A failed payment may be retried with the same or a different payment method, which requires a new
  confirmation from the user.
- After **three** failed attempts on the same order, Accounts Payable must review the order before any further
  retry.

## Double-charge prevention
Every payment request carries an idempotency key. Systems must never capture more than one successful
payment for the same order.

## Refunds
Cancelling a paid order triggers a refund to the original payment method within 5 business days.

## Card data
Card numbers, CVV codes and bank credentials must never be typed into chat or stored in the ordering
system. Only tokenized payment methods may be used.
