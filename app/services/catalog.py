from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, func, or_
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


# تا این تعداد، مشتری خرد با قیمت خرده حساب می‌شود؛ بیشتر از آن با قیمت عمده.
RETAIL_MAX_QTY = 5


async def next_sku(session: AsyncSession) -> str:
    """Legacy helper (race-prone). New code: temp_sku() + assign_sku() after flush."""
    mx = await session.scalar(select(func.max(Product.id)))
    return f"PRD-{100001 + int(mx or 0)}"


def temp_sku() -> str:
    """Unique placeholder until the product id exists."""
    return f"TMP-{uuid4().hex[:20]}"


def sku_for_id(product_id: int) -> str:
    # Same numbering as before (PRD-100001 for id 1), but derived from the
    # row's own id so two concurrent creations never collide.
    return f"PRD-{100000 + int(product_id)}"


async def assign_sku(session: AsyncSession, product: Product) -> str:
    """Flush to get the id, then give the product its final SKU."""
    await session.flush()
    product.internal_sku = sku_for_id(product.id)
    await session.flush()
    return product.internal_sku


async def find_tier(session: AsyncSession, product: Product, qty: int) -> PriceTier | None:
    return await session.scalar(
        select(PriceTier).where(
            PriceTier.product_id == product.id,
            PriceTier.min_qty <= qty,
            (PriceTier.max_qty.is_(None)) | (PriceTier.max_qty >= qty),
        ).order_by(PriceTier.min_qty.desc())
    )


def base_price_for(user: User | None, product: Product, qty: int = 1) -> Decimal:
    """Price used when no quantity tier matches.

    - approved wholesale accounts always pay the wholesale price;
    - everyone else pays retail up to RETAIL_MAX_QTY and wholesale above it.
    A zero wholesale price falls back to retail (never sell at zero).
    """
    wholesale = Decimal(product.wholesale_price or 0)
    if wholesale > 0 and (customer_type_of(user) == WHOLESALE or int(qty or 0) > RETAIL_MAX_QTY):
        return wholesale
    return Decimal(product.base_price or 0)


async def get_price_for_user(
    session: AsyncSession, product: Product, qty: int, user: User | None = None
) -> Decimal:
    """Tier price wins; otherwise see base_price_for (retail ≤5, wholesale >5)."""
    tier = await find_tier(session, product, qty)
    if tier:
        return Decimal(tier.unit_price)
    return base_price_for(user, product, qty)


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


async def hidden_category_ids(session: AsyncSession) -> set[int]:
    """Inactive categories plus all of their descendants."""
    rows = (await session.execute(select(Category.id, Category.parent_id, Category.is_active))).all()
    children: dict[int, list[int]] = {}
    for cid, parent_id, _active in rows:
        if parent_id is not None:
            children.setdefault(parent_id, []).append(cid)
    hidden: set[int] = set()
    stack = [cid for cid, _p, active in rows if not active]
    while stack:
        cid = stack.pop()
        if cid in hidden:
            continue
        hidden.add(cid)
        stack.extend(children.get(cid, []))
    return hidden


def visible_products_filter(hidden: set[int]):
    """SQL condition that hides products sitting in a disabled category branch."""
    if not hidden:
        return None
    return or_(Product.category_id.is_(None), Product.category_id.not_in(sorted(hidden)))


def is_available(product: Product | None) -> bool:
    return bool(
        product and product.is_active
        and product.stock_status not in {"unavailable", "out_of_stock"}
        and product.stock_quantity != 0
    )


async def product_visible(session: AsyncSession, product: Product | None) -> bool:
    if not product or not product.is_active:
        return False
    if product.category_id is None:
        return True
    return product.category_id not in await hidden_category_ids(session)
