---
title: Replenishment Planning Guidelines
doc_type: replenishment_guideline
version: "2.4"
effective_date: 2026-05-10
owner: Supply Chain Planning
---
# Replenishment Planning Guidelines

## How recommendations are calculated
The replenishment engine recommends an order when projected stock falls below the safety stock during the
vendor lead time. The recommended quantity is:

`recommended quantity = forecast demand over the horizon + safety stock - on-hand - on-order`

The result is rounded up to the vendor case pack.

## Safety stock
Safety stock protects against demand and lead-time variability. It is set per SKU and location, typically
between **2 and 4 days** of average daily demand. High-velocity perishable items at stores use the lower end
to limit spoilage; DCs use the upper end.

## Risk levels
- **Critical**: on-hand below 50% of safety stock - order immediately; expedite if lead time exceeds cover.
- **High**: on-hand below safety stock - order today.
- **Medium**: on-hand within 150% of safety stock - review within 48 hours.
- **Low**: adequately stocked - no action required.

## Days of cover
Days of cover equals on-hand units divided by average daily demand. When days of cover is shorter than the
vendor lead time, a stock-out is expected before the next delivery.

## Promotions and seasonality
Promotional uplift and seasonal peaks are included in the forecast. Buyers may increase a recommended
quantity by up to 20% for known local events; larger overrides need planner review.

## Perishables
Do not order perishable items beyond **7 days of forecast demand** plus safety stock, to avoid spoilage.
