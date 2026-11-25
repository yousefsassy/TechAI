from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import String, Integer, Numeric, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.database import Base

def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Timestamps:
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

class Product(Timestamps, Base):
    __tablename__ = 'products'
    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(unique=True)
    category: Mapped[str] = mapped_column(index=True)
    brand: Mapped[str | None]
    model: Mapped[str | None]
    name: Mapped[str]
    processor: Mapped[str | None]
    ram_gb: Mapped[int | None]
    storage_gb: Mapped[int | None]
    storage_type: Mapped[str | None]
    screen_size: Mapped[float | None]
    operating_system: Mapped[str | None]
    raw_specifications: Mapped[dict] = mapped_column(JSON, default=dict)
    offers: Mapped[list['Offer']] = relationship(back_populates='product')

class Offer(Timestamps, Base):
    __tablename__ = 'supplier_offers'
    __table_args__ = (UniqueConstraint('supplier_name', 'supplier_product_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    supplier_name: Mapped[str]
    supplier_product_id: Mapped[str]
    price_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    stock_status: Mapped[str]
    available_quantity: Mapped[int | None]
    product_url: Mapped[str] = mapped_column(unique=True)
    image_url: Mapped[str | None]
    scraped_at: Mapped[datetime] = mapped_column(default=now)
    is_demo: Mapped[bool] = mapped_column(default=False)
    product: Mapped[Product] = relationship(back_populates='offers')

class CatalogRefresh(Base):
    __tablename__ = 'catalog_refresh'
    id: Mapped[str] = mapped_column(primary_key=True)
    last_completed_at: Mapped[datetime | None]
    lease_until: Mapped[datetime | None]
    last_error: Mapped[str | None]

class Proposal(Timestamps, Base):
    __tablename__ = 'purchase_proposals'
    id: Mapped[str] = mapped_column(primary_key=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    proposal_payload: Mapped[dict] = mapped_column(JSON)
    proposal_hash: Mapped[str]
    budget_limit_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    total_amount_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    status: Mapped[str] = mapped_column(default='pending')
    approved_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]

class Order(Timestamps, Base):
    __tablename__ = 'purchase_orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey('purchase_proposals.id'), unique=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    order_reference: Mapped[str] = mapped_column(unique=True)
    user_email: Mapped[str]
    status: Mapped[str] = mapped_column(default='sandbox_confirmed')
    approval_status: Mapped[str] = mapped_column(default='approved')
    approved_at: Mapped[datetime]
    budget_limit_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    total_amount_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    items: Mapped[list['OrderItem']] = relationship()
    notifications: Mapped[list['Notification']] = relationship()

class OrderItem(Base):
    __tablename__ = 'order_items'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('purchase_orders.id'))
    offer_id: Mapped[int] = mapped_column(ForeignKey('supplier_offers.id'))
    product_name_snapshot: Mapped[str]
    supplier_snapshot: Mapped[str]
    unit_price_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))
    quantity: Mapped[int]
    subtotal_tnd: Mapped[Decimal] = mapped_column(Numeric(14,3))

class Notification(Base):
    __tablename__ = 'email_notifications'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('purchase_orders.id'), unique=True)
    recipient: Mapped[str]
    status: Mapped[str] = mapped_column(default='pending')
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    sent_at: Mapped[datetime | None]
    idempotency_key: Mapped[str] = mapped_column(unique=True)

class AgentRun(Base):
    __tablename__ = 'agent_runs'
    id: Mapped[str] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(unique=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(default='running')
    steps_executed: Mapped[int] = mapped_column(default=0)
    stop_reason: Mapped[str | None]
    total_tokens: Mapped[int] = mapped_column(default=0)
    estimated_cost: Mapped[float | None]
    trajectory: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(default=now)
    completed_at: Mapped[datetime | None]


class ShoppingSelection(Timestamps, Base):
    __tablename__ = 'shopping_selections'
    id: Mapped[str] = mapped_column(primary_key=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str]
    status: Mapped[str] = mapped_column(default='pending')
    selected_offer_id: Mapped[int | None] = mapped_column(ForeignKey('supplier_offers.id'))
    selected_quantity: Mapped[int | None]
    selected_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]

class ShoppingConversation(Timestamps, Base):
    __tablename__ = 'shopping_conversations'
    id: Mapped[str] = mapped_column(primary_key=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    title: Mapped[str]
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    last_offer_ids: Mapped[list] = mapped_column(JSON, default=list)
    messages: Mapped[list['ShoppingMessage']] = relationship(back_populates='conversation', cascade='all, delete-orphan')

class ShoppingMessage(Base):
    __tablename__ = 'shopping_messages'
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('shopping_conversations.id'), index=True)
    role: Mapped[str]
    content: Mapped[str]
    products: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=now)
    conversation: Mapped[ShoppingConversation] = relationship(back_populates='messages')


class ShoppingTurn(Base):
    __tablename__ = 'shopping_turns'
    owner_id: Mapped[str] = mapped_column(primary_key=True)
    turn_id: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey('shopping_conversations.id'))
    assistant_message_id: Mapped[int] = mapped_column(ForeignKey('shopping_messages.id'))
    result: Mapped[dict] = mapped_column(JSON)


class CartItem(Timestamps, Base):
    __tablename__ = 'shopping_cart_items'
    __table_args__ = (UniqueConstraint('user_session_id', 'offer_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_session_id: Mapped[str] = mapped_column(index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey('supplier_offers.id'))
    quantity: Mapped[int]
    offer: Mapped[Offer] = relationship()

class ShoppingUser(Timestamps, Base):
    __tablename__ = 'shopping_users'
    id: Mapped[str] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    totp_secret_encrypted: Mapped[str]
    mfa_enabled: Mapped[bool] = mapped_column(default=False)
    last_totp_counter: Mapped[int] = mapped_column(default=-1)
    recovery_hashes: Mapped[list] = mapped_column(JSON, default=list)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]

class ShoppingAuthChallenge(Base):
    __tablename__ = 'shopping_auth_challenges'
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('shopping_users.id'), index=True)
    purpose: Mapped[str]
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)

class ShoppingAuthSession(Base):
    __tablename__ = 'shopping_auth_sessions'
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('shopping_users.id'), index=True)
    expires_at: Mapped[datetime]

class ShoppingEmailChallenge(Base):
    __tablename__ = 'shopping_email_challenges'
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('shopping_users.id'), index=True)
    code_hash: Mapped[str]
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)


class RetailerClock(Base):
    __tablename__ = 'retailer_request_clocks'
    host: Mapped[str] = mapped_column(primary_key=True)
    next_request: Mapped[float] = mapped_column(default=0.0)
    paused_until: Mapped[float] = mapped_column(default=0.0)
    active_until: Mapped[float] = mapped_column(default=0.0, server_default='0')
    owner_token: Mapped[str | None]
    transport_failures: Mapped[int] = mapped_column(default=0, server_default='0')


class DiscoveryIndex(Base):
    __tablename__ = 'retailer_discovery_indexes'
    url: Mapped[str] = mapped_column(primary_key=True)
    entries: Mapped[list] = mapped_column(JSON)
    is_index: Mapped[bool]
    collected_at: Mapped[datetime] = mapped_column(default=now)


class ShoppingLookup(Base):
    __tablename__ = 'shopping_lookup_leases'
    signature: Mapped[str] = mapped_column(primary_key=True)
    retry_at: Mapped[datetime]


class DiscoveryOperation(Base):
    __tablename__ = 'retailer_discovery_operations'
    signature: Mapped[str] = mapped_column(primary_key=True)
    token: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    completed_at: Mapped[datetime | None]
    reports: Mapped[list | None] = mapped_column(JSON)
