from __future__ import annotations
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric,
    String, Text, UniqueConstraint, JSON
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(30))
    shop_name: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    # Customer segment: "retail" (خرد) or "wholesale" (عمده).
    # Wholesale accounts need management approval before they can buy.
    customer_type: Mapped[str] = mapped_column(String(20), default="retail", index=True)
    address: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(150))


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_perm"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"))


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission_id", name="uq_user_permission"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"))
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True)


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(Text)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id", ondelete="SET NULL"))
    last_import_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    internal_sku: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    factory_code: Mapped[str | None] = mapped_column(String(150), index=True)
    model: Mapped[str | None] = mapped_column(String(150), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id", ondelete="SET NULL"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)
    # Legacy `base_price` is the customer retail selling price. It is kept for
    # backward compatibility with orders, imports and existing deployments.
    base_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    wholesale_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    unit: Mapped[str] = mapped_column(String(50), default="عدد")
    stock_status: Mapped[str] = mapped_column(String(30), default="available")
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class ProductImage(Base):
    __tablename__ = "product_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SupplierProduct(Base):
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "supplier_sku", name="uq_supplier_sku"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    supplier_sku: Mapped[str | None] = mapped_column(String(150), index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(300))
    last_supplier_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))


class SupplierMapping(Base):
    __tablename__ = "supplier_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), unique=True)
    header_mapping: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class PriceTier(Base):
    __tablename__ = "price_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    min_qty: Mapped[int] = mapped_column(Integer)
    max_qty: Mapped[int | None] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    new_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    source: Mapped[str] = mapped_column(String(50), default="manual")
    price_type: Mapped[str] = mapped_column(String(30), default="retail")
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Cart(Base):
    __tablename__ = "carts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(220))
    customer_phone_snapshot: Mapped[str | None] = mapped_column(String(30))
    shop_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(50), default="WAITING_PAYMENT", index=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    # Product may later be deleted/reset; order snapshot fields keep the historical sale intact.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    receipt_file_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150), default="حساب اصلی")
    card_number: Mapped[str | None] = mapped_column(String(32))
    iban: Mapped[str | None] = mapped_column(String(64))
    holder_name: Mapped[str | None] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    original_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="DRY_RUN")
    mapping: Mapped[dict | None] = mapped_column(JSON)
    summary: Mapped[dict | None] = mapped_column(JSON)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), index=True)
    raw_data: Mapped[dict] = mapped_column(JSON)
    matched_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    confidence: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class PublishChannel(Base):
    __tablename__ = "publish_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(180))
    chat_ref: Mapped[str] = mapped_column(String(180), unique=True)
    username: Mapped[str | None] = mapped_column(String(180))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ProductChannelPost(Base):
    __tablename__ = "product_channel_posts"
    __table_args__ = (UniqueConstraint("product_id", "channel_id", name="uq_product_channel_post"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("publish_channels.id", ondelete="CASCADE"))
    message_id: Mapped[int] = mapped_column(BigInteger)
    post_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminInvite(Base):
    __tablename__ = "admin_invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    permissions: Mapped[list] = mapped_column(JSON)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
