from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.domain.schemas.payment import Payment

_COLS = """payment_id, order_id, amount, currency, method, method_last4, status, provider_reference, failure_code,
           failure_reason, attempt, created_at"""


async def find_by_idempotency_key(session: AsyncSession, key: str) -> Payment | None:
    row = (await session.execute(text(f"SELECT {_COLS} FROM payments WHERE idempotency_key = :k"), {"k": key})).first()
    return Payment.model_validate(dict(row._mapping)) if row else None


async def list_for_order(session: AsyncSession, order_id: UUID) -> list[Payment]:
    rows = await session.execute(
        text(f"SELECT {_COLS} FROM payments WHERE order_id = :o ORDER BY attempt DESC, created_at DESC"), {"o": order_id}
    )
    return [Payment.model_validate(dict(r._mapping)) for r in rows]


async def insert(
    session: AsyncSession,
    *,
    tenant_id: str,
    order_id: UUID,
    amount: Decimal,
    currency: str,
    method: str,
    last4: str | None,
    status: str,
    provider_reference: str | None,
    failure_code: str | None,
    failure_reason: str | None,
    attempt: int,
    idempotency_key: str,
    approval_id: UUID | None,
) -> Payment:
    row = (
        await session.execute(
            text(
                f"""INSERT INTO payments (payment_id, tenant_id, order_id, amount, currency, method, method_last4, status,
                        provider_reference, failure_code, failure_reason, attempt, idempotency_key, approval_id)
                    VALUES (:id, :t, :o, :a, :c, :m, :l4, :s, :ref, :fc, :fr, :att, :key, :appr) RETURNING {_COLS}"""
            ),
            {"id": uuid4(), "t": tenant_id, "o": order_id, "a": amount, "c": currency, "m": method, "l4": last4,
             "s": status, "ref": provider_reference, "fc": failure_code, "fr": failure_reason, "att": attempt,
             "key": idempotency_key, "appr": approval_id},
        )
    ).one()
    return Payment.model_validate(dict(row._mapping))
