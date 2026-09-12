from datetime import datetime
from decimal import Decimal
from typing import Optional

from typing import Literal

from pydantic import BaseModel, field_validator
from app.schemas.common import StrictModel

from app.models.accounts import AccountType

BankKind = Literal["bank", "credit_card"]


class AccountCreate(StrictModel):
    name: str
    account_number: Optional[str] = None
    account_type: AccountType
    parent_id: Optional[int] = None
    description: Optional[str] = None
    bank_kind: Optional[BankKind] = None

    @field_validator("account_number")
    @classmethod
    def blank_account_number_to_none(cls, v):
        # account_number is unique; storing "" would collide across accounts
        return v.strip() or None if isinstance(v, str) else v


class AccountUpdate(StrictModel):
    name: Optional[str] = None
    account_number: Optional[str] = None
    account_type: Optional[AccountType] = None
    parent_id: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    bank_kind: Optional[BankKind] = None

    @field_validator("account_number")
    @classmethod
    def blank_account_number_to_none(cls, v):
        return v.strip() or None if isinstance(v, str) else v


class AccountResponse(BaseModel):
    id: int
    name: str
    account_number: Optional[str]
    account_type: AccountType
    parent_id: Optional[int]
    description: Optional[str]
    is_active: bool
    is_system: bool
    bank_kind: Optional[str] = None
    balance: Decimal
    created_at: datetime

    @field_validator("balance", mode="before")
    @classmethod
    def null_balance_to_zero(cls, v):
        # legacy/imported rows can carry NULL balances; don't 500 on read
        return Decimal("0") if v is None else v

    updated_at: datetime

    model_config = {"from_attributes": True}
