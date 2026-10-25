from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, PriceTier, PriceHistory

PRICE_ATTRS = {
    "purchase": "purchase_price",
    "retail": "base_price",
    "wholesale": "wholesale_price",
}

async def next_sku(session: AsyncSession) -> str:
    mx = await session.scalar(select(func.max(Product.id)))
    return f"PRD-{100001 + int(mx or 0)}"

async def get_price_for_qty(session: AsyncSession, product: Product, qty: int) -> Decimal:
    tier = await session.scalar(
        select(PriceTier).where(
            PriceTier.product_id == product.id,
            PriceTier.min_qty <= qty,
            (PriceTier.max_qty.is_(None)) | (PriceTier.max_qty >= qty),
        ).order_by(PriceTier.min_qty.desc())
    )
    # Customer checkout uses tier price when a quantity rule matches; otherwise
    # it uses the retail selling price (legacy base_price).
    return Decimal(tier.unit_price if tier else product.base_price)

async def set_product_price(
    session: AsyncSession, product: Product, new_price: Decimal,
    user_id: int | None, source: str = "manual", price_type: str = "retail"
):
    if price_type not in PRICE_ATTRS:
        raise ValueError(f"unsupported price_type: {price_type}")
    attr = PRICE_ATTRS[price_type]
    old = Decimal(getattr(product, attr) or 0)
    new_price = Decimal(new_price)
    if old != new_price:
        session.add(PriceHistory(
            product_id=product.id,
            old_price=old,
            new_price=new_price,
            changed_by_user_id=user_id,
            source=source,
            price_type=price_type,
        ))
        setattr(product, attr, new_price)
    return old

async def set_price(session: AsyncSession, product: Product, new_price: Decimal, user_id: int | None, source="manual"):
    # Backward-compatible alias: historical callers mean retail sale price.
    return await set_product_price(session, product, new_price, user_id, source, "retail")
