from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, PriceTier, PriceHistory, Category, User

PRICE_ATTRS = {
    "purchase": "purchase_price",
    "retail": "base_price",
    "wholesale": "wholesale_price",
}

PRICE_LABELS = {
    "purchase": "قیمت پایه",
    "retail": "فروش خرده",
    "wholesale": "فروش عمده",
}

RETAIL = "retail"
WHOLESALE = "wholesale"

CUSTOMER_TYPE_LABELS = {
    RETAIL: "خرد",
    WHOLESALE: "عمده",
}


def customer_type_of(user: User | None) -> str:
    """Return the normalized customer segment for a user (defaults to retail)."""
    if user is None:
        return RETAIL
    return WHOLESALE if getattr(user, "customer_type", None) == WHOLESALE else RETAIL


async def next_sku(session: AsyncSession) -> str:
    mx = await session.scalar(select(func.max(Product.id)))
    return f"PRD-{100001 + int(mx or 0)}"


async def find_tier(session: AsyncSession, product: Product, qty: int) -> PriceTier | None:
    return await session.scalar(
        select(PriceTier).where(
            PriceTier.product_id == product.id,
            PriceTier.min_qty <= qty,
            (PriceTier.max_qty.is_(None)) | (PriceTier.max_qty >= qty),
        ).order_by(PriceTier.min_qty.desc())
    )


def base_price_for(user: User | None, product: Product) -> Decimal:
    """Price used when no quantity tier matches."""
    if customer_type_of(user) == WHOLESALE:
        wholesale = Decimal(product.wholesale_price or 0)
        if wholesale > 0:
            return wholesale
    return Decimal(product.base_price or 0)


async def get_price_for_user(
    session: AsyncSession, product: Product, qty: int, user: User | None = None
) -> Decimal:
    """Tier price wins; otherwise wholesale customers pay the wholesale price
    and retail customers pay the retail price (legacy `base_price`)."""
    tier = await find_tier(session, product, qty)
    if tier:
        return Decimal(tier.unit_price)
    return base_price_for(user, product)


async def get_price_for_qty(session: AsyncSession, product: Product, qty: int) -> Decimal:
    # Backward-compatible alias: no user context means retail pricing.
    return await get_price_for_user(session, product, qty, None)


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


# ---------------- CATEGORY TREE ----------------

async def category_tree(session: AsyncSession, only_active: bool = False) -> list[tuple[Category, int]]:
    """Flatten the category tree depth-first as [(category, depth), ...]."""
    stmt = select(Category)
    if only_active:
        stmt = stmt.where(Category.is_active.is_(True))
    rows = (await session.execute(stmt.order_by(Category.name))).scalars().all()

    by_parent: dict[int | None, list[Category]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)

    ordered: list[tuple[Category, int]] = []
    seen: set[int] = set()

    def walk(parent_id: int | None, depth: int):
        for row in by_parent.get(parent_id, []):
            if row.id in seen:
                continue
            seen.add(row.id)
            ordered.append((row, depth))
            walk(row.id, depth + 1)

    walk(None, 0)
    # Orphans (parent deactivated/removed) must not disappear from the UI.
    for row in rows:
        if row.id not in seen:
            ordered.append((row, 0))
    return ordered


def category_label(name: str, depth: int, is_active: bool = True) -> str:
    prefix = ("└ " * depth) if depth else ""
    mark = "" if is_active else " ⭕"
    return f"{prefix}{name}{mark}"


async def category_path(session: AsyncSession, category: Category | None) -> str:
    if category is None:
        return "بدون دسته‌بندی"
    parts = [category.name]
    parent_id = category.parent_id
    guard = 0
    while parent_id and guard < 10:
        parent = await session.get(Category, parent_id)
        if not parent:
            break
        parts.append(parent.name)
        parent_id = parent.parent_id
        guard += 1
    return " › ".join(reversed(parts))


async def descendant_ids(session: AsyncSession, category_id: int) -> list[int]:
    """Category id + all descendants, used for filtering products."""
    rows = (await session.execute(select(Category.id, Category.parent_id))).all()
    children: dict[int, list[int]] = {}
    for cid, parent_id in rows:
        if parent_id is not None:
            children.setdefault(parent_id, []).append(cid)
    out, stack = [], [category_id]
    seen = set()
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
        stack.extend(children.get(cid, []))
    return out
