from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cart, CartItem, Product, Order, OrderItem, User
from app.services.catalog import get_price_for_user


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

    last_id = await session.scalar(select(func.max(Order.id))) or 0
    order = Order(
        order_no=f"ORD-{100001 + int(last_id)}",
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

    total = Decimal("0")
    kept: list[CartItem] = []
    for item in items:
        product = await session.get(Product, item.product_id)
        if not product:
            # Product was deleted while sitting in the cart: drop the line instead
            # of crashing the whole checkout.
            await session.delete(item)
            continue
        unit = await get_price_for_user(session, product, item.quantity, customer)
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
    return order
