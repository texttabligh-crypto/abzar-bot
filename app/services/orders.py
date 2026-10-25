from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cart, CartItem, Product, Order, OrderItem, User
from app.services.catalog import get_price_for_qty

async def get_or_create_cart(session: AsyncSession, user_id: int) -> Cart:
    cart = await session.scalar(select(Cart).where(Cart.user_id == user_id))
    if not cart:
        cart = Cart(user_id=user_id)
        session.add(cart)
        await session.commit()
        await session.refresh(cart)
    return cart

async def create_order_from_cart(session: AsyncSession, user_id: int) -> Order | None:
    cart = await get_or_create_cart(session, user_id)
    items = (await session.execute(select(CartItem).where(CartItem.cart_id == cart.id))).scalars().all()
    if not items:
        return None

    last_id = await session.scalar(select(func.max(Order.id))) or 0
    order = Order(
        order_no=f"ORD-{100001 + int(last_id)}",
        user_id=user_id,
        status="WAITING_PAYMENT",
        total_amount=0,
    )
    session.add(order)
    await session.flush()

    total = Decimal("0")
    for item in items:
        product = await session.get(Product, item.product_id)
        unit = await get_price_for_qty(session, product, item.quantity)
        line = unit * item.quantity
        total += line
        session.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            product_name_snapshot=product.name,
            quantity=item.quantity,
            unit_price_snapshot=unit,
            line_total=line,
        ))
    order.total_amount = total
    for item in items:
        await session.delete(item)
    await session.commit()
    await session.refresh(order)
    return order
