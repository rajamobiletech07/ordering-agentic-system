from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Money crosses JSON boundaries as a string to avoid float rounding.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json")]
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:\-]+$")]


class StrictModel(BaseModel):
    """Input models reject unknown fields (defends against injected/hallucinated arguments)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel):
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0, le=10_000)
