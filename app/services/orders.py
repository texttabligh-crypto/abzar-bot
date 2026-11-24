from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cart, CartItem, Product, Order, OrderItem, User
from app.services.catalog import get_price_for_user


class CheckoutError(ValueError):
    """Cart cannot be checked out without customer intervention."""


async def get_or_create_cart(session: AsyncSession, user_id: int) -> Cart:
    cart = await session.scalar(select(Cart).where(Cart.user_id == user_id))
    if not cart:
        cart = Cart(user_id=user_id)
        session.add(cart)
        await session.commit()
        await session.refresh(cart)
    return cart


def customer_name(user: User | None) -> str | None:
    if not user:
        return None
    name = " ".join(p for p in [user.first_name, user.last_name] if p).strip()
    return name or None


async def create_order_from_cart(session: AsyncSession, user_id: int) -> Order | None:
    cart = await get_or_create_cart(session, user_id)
    items = (await session.execute(select(CartItem).where(CartItem.cart_id == cart.id))).scalars().all()
    if not items:
        return None

    customer = await session.get(User, user_id)
    if not customer:
        return None

    # Lock all cart products in a stable order before validating or reserving stock.
    products = (await session.execute(
        select(Product).where(Product.id.in_([i.product_id for i in items]))
        .order_by(Product.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    by_id = {p.id: p for p in products}
    for item in items:
        p = by_id.get(item.product_id)
        if not p or not p.is_active or p.stock_status in {"unavailable", "out_of_stock"}:
            raise CheckoutError("محصولی در سبد غیرفعال یا ناموجود شده؛ سبد را اصلاح کنید.")
        if item.quantity < 1 or (p.stock_quantity is not None and item.quantity > p.stock_quantity):
            raise CheckoutError("موجودی محصول برای تعداد انتخاب‌شده کافی نیست.")

    order = Order(
        # Placeholder; the final number is derived from the row id below so
        # two simultaneous checkouts can never get the same number.
        order_no=f"TMP-{uuid4().hex[:20]}",
        user_id=user_id,
        status="WAITING_PAYMENT",
        total_amount=0,
        # Immutable snapshots so later profile edits never rewrite history.
        customer_name_snapshot=customer_name(customer),
        customer_phone_snapshot=customer.phone,
        shop_name_snapshot=customer.shop_name,
    )
    session.add(order)
    await session.flush()
    order.order_no = f"ORD-{100000 + int(order.id)}"

    total = Decimal("0")
    kept: list[CartItem] = []
    depleted: list[int] = []
    for item in items:
        product = await session.get(Product, item.product_id)
        if not product:
            # Product was deleted while sitting in the cart: drop the line instead
            # of crashing the whole checkout.
            await session.delete(item)
            continue
        unit = await get_price_for_user(session, product, item.quantity, customer)
        # Reserve stock now; it is given back if the order is cancelled.
        if product.stock_quantity is not None:
            product.stock_quantity = int(product.stock_quantity) - int(item.quantity)
            if product.stock_quantity <= 0:
                product.stock_quantity = 0
                depleted.append(product.id)
        line = unit * item.quantity
        total += line
        kept.append(item)
        session.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            product_name_snapshot=product.name,
            quantity=item.quantity,
            unit_price_snapshot=unit,
            line_total=line,
        ))
    if not kept:
        # Nothing left to buy — roll the empty order back.
        await session.rollback()
        return None
    order.total_amount = total
    for item in kept:
        await session.delete(item)
    await session.commit()
    await session.refresh(order)
    # Plain attribute (not a column): products whose channel post must now
    # show «ناموجود».
    order.depleted_product_ids = depleted
    return order


async def restore_order_stock(session: AsyncSession, order: Order) -> list[int]:
    """Give reserved stock back (used when an order is cancelled)."""
    items = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    ids = sorted({i.product_id for i in items if i.product_id})
    if not ids:
        return []
    products = {
        p.id: p for p in (await session.execute(
            select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update()
        )).scalars().all()
    }
    restored = []
    for item in items:
        p = products.get(item.product_id)
        if p is not None and p.stock_quantity is not None:
            p.stock_quantity = int(p.stock_quantity) + int(item.quantity)
            restored.append(p.id)
    return sorted(set(restored))
