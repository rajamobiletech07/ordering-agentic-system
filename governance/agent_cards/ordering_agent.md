# Agent card - Ordering Agent

| Field | Value |
|---|---|
| Purpose | Create/read/update/cancel orders, run simulated payments, queue notifications, verify final status. |
| Autonomy level | **L2 - executes only approved actions.** Every mutation carries an `approval_id` verified server-side against a hash of the approved parameters. |
| Tools (enforced in code) | `ordering.*`, `payment.process_payment`, `payment.get_payment_status`, `notification.*`, `recommendation.get_recommendation`, `recommendation.get_vendor`. |
| Safety properties | Idempotency keys derived from the approval (`ord-/pay-/ntf-`) → safe re-runs; one successful payment per order (DB constraint); amounts always read from the DB. |
| Partial failures | Reported precisely (order created / payment failed / notification pending) and left retryable - never silently rolled back or repeated. |
| Real-world effects | None: payments and notifications are simulated. |
| Owner | ordering-platform |
