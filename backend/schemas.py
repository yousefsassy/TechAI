from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, EmailStr

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Requirements(StrictModel):
    category: Literal['laptop','desktop','tablet'] | None = None
    quantity: int | None = Field(default=1, ge=1, le=100)
    brands: list[str] = Field(default_factory=list, max_length=20)
    min_ram: int | None = Field(default=None, ge=0, le=1024)
    min_storage: int | None = Field(default=None, ge=0, le=100000)
    processor: str | None = Field(default=None, max_length=100)
    storage_type: Literal['SSD','HDD','eMMC'] | None = None
    budget: Decimal | None = Field(default=None, gt=0, le=10000000, decimal_places=3)
    intended_use: list[Literal['general','programming','machine_learning','gaming','study','notes','media','video_editing','work']] = Field(default_factory=list, max_length=9)
    priorities: list[Literal['ram','storage','value','budget_headroom','data_confidence','portability','battery','graphics']] = Field(default_factory=list, max_length=8)
    preferred_brands: list[str] = Field(default_factory=list, max_length=20)
    soft_preferences: list[str] = Field(default_factory=list, max_length=8)
    # Legacy field retained for unsupported HARD requirements that require clarification.
    other_preferences: str = Field(default='', max_length=1000)

class SearchInput(StrictModel):
    requirements: Requirements
    max_unit_price: Decimal | None = Field(default=None, gt=0)

class CompareInput(StrictModel):
    offer_ids: list[int] = Field(min_length=1, max_length=100)
    requirements: Requirements

class BudgetInput(StrictModel):
    offer_id: int = Field(gt=0)
    quantity: int = Field(ge=1, le=100)
    budget: Decimal = Field(gt=0, le=10000000, decimal_places=3)

class OrderInput(StrictModel):
    proposal_id: str = Field(min_length=16, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=128)

class DetailInput(StrictModel):
    offer_id: int = Field(gt=0)

class EmailInput(StrictModel):
    order_id: int = Field(gt=0)

class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=8000)
    email: EmailStr

class ApprovalInput(StrictModel):
    proposal_id: str
    proposal_hash: str
    approve: bool

class StructuredInput(StrictModel):
    requirements: Requirements
    email: EmailStr


class OfferOutput(StrictModel):
    id: int
    name: str
    category: str = Field(min_length=1, max_length=100)
    brand: str | None
    model: str | None
    processor: str | None
    ram_gb: int | None
    storage_gb: int | None
    storage_type: str | None
    screen_size: float | None
    operating_system: str | None
    supplier: str
    price_tnd: str
    stock_status: Literal['in_stock','out_of_stock','unknown']
    available_quantity: int | None
    product_url: str | None
    image_url: str | None
    scraped_at: str
    is_demo: bool
    description: str = ''
    specifications: dict = Field(default_factory=dict)
    graphics: str | None = None
    rating: str | float | None = None
    rating_count: str | int | None = None

class ShoppingChatInput(StrictModel):
    turn_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    language: Literal['en','fr','ar'] = 'en'

class CartUpdateInput(StrictModel):
    offer_id: int = Field(gt=0)
    quantity: int = Field(ge=0, le=100)

class RegisterInput(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

class LoginInput(RegisterInput):
    pass

class MfaInput(StrictModel):
    challenge: str = Field(min_length=32, max_length=128)
    code: str = Field(min_length=6, max_length=32)

class ComparisonOutput(OfferOutput):
    total_tnd: str
    ranking_reason: str
    stock_note: str
    label: str
    suitability_score: float
    score_components: dict[str, float]
    facts: list[str]
    limitations: list[str]
    tradeoffs: list[str]
    remaining_budget_tnd: str
    scoring_version: str

class BudgetOutput(StrictModel):
    total: str
    budget: str
    remaining: str
    valid: bool

class OrderItemOutput(StrictModel):
    name: str
    supplier: str
    quantity: int
    unit_price: str
    subtotal: str

class OrderOutput(StrictModel):
    id: int
    reference: str
    status: str
    total: str
    created_at: str
    email_status: str
    items: list[OrderItemOutput]

class EmailOutput(StrictModel):
    status: Literal['pending','failed','sent','sending','delivery_unknown']
    error: str | None = None


class SelectionInput(StrictModel):
    selection_id: str = Field(min_length=32, max_length=32)
    offer_id: int = Field(gt=0, strict=True)
    quantity: int = Field(ge=1, le=100, strict=True)
