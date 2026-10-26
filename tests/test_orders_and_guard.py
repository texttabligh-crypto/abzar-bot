from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    User, Product, PriceTier, CartItem, Order, OrderItem, Permission, UserPermission,
)
from app.services.catalog import RETAIL, WHOLESALE
from app.services.orders import get_or_create_cart, create_order_from_cart
from app.services.auth import can_access, has_permission, guard


async def make_user(s, tg_id, kind=RETAIL, approved=True, active=True):
    user = User(
        telegram_id=tg_id, first_name="ادریس", last_name="تست",
        phone="09120000000", shop_name="فروشگاه تست", customer_type=kind,
        is_approved=approved, is_active=active,
    )
    s.add(user)
    await s.flush()
    return user


@pytest.mark.asyncio
async def test_order_snapshots_and_wholesale_price(db):
    async with db() as s:
        user = await make_user(s, 100, WHOLESALE)
        product = Product(
            internal_sku="PRD-100010", name="واشر سر سیلندر پژو",
            base_price=Decimal("200000"), wholesale_price=Decimal("170000"),
            purchase_price=Decimal("150000"),
        )
        s.add(product)
        await s.flush()
        s.add(PriceTier(product_id=product.id, min_qty=50, max_qty=None, unit_price=Decimal("160000")))
        cart = await get_or_create_cart(s, user.id)
        s.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=60))
        await s.commit()

        order = await create_order_from_cart(s, user.id)
        assert order is not None
        assert order.customer_name_snapshot == "ادریس تست"
        assert order.customer_phone_snapshot == "09120000000"
        assert order.shop_name_snapshot == "فروشگاه تست"

        items = (await s.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
        assert len(items) == 1
        assert items[0].unit_price_snapshot == Decimal("160000")  # پلکان
        assert order.total_amount == Decimal("160000") * 60
        # سبد خالی شد
        remaining = (await s.execute(select(CartItem).where(CartItem.cart_id == cart.id))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_order_retail_price_snapshot(db):
    async with db() as s:
        user = await make_user(s, 101, RETAIL)
        product = Product(
            internal_sku="PRD-100011", name="محصول خرد",
            base_price=Decimal("50000"), wholesale_price=Decimal("40000"),
        )
        s.add(product)
        await s.flush()
        cart = await get_or_create_cart(s, user.id)
        s.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=2))
        await s.commit()
        order = await create_order_from_cart(s, user.id)
        assert order.total_amount == Decimal("100000")


@pytest.mark.asyncio
async def test_empty_cart_returns_none(db):
    async with db() as s:
        user = await make_user(s, 102)
        await s.commit()
        assert await create_order_from_cart(s, user.id) is None


@pytest.mark.asyncio
async def test_permissions_owner_admin_and_denied(db):
    async with db() as s:
        owner = await make_user(s, 200)
        owner.is_owner = True
        manager = await make_user(s, 201)
        nobody = await make_user(s, 202)
        perm = Permission(code="products.edit", title="ویرایش محصول")
        s.add(perm)
        await s.flush()
        s.add(UserPermission(user_id=manager.id, permission_id=perm.id, is_allowed=True))
        await s.commit()

    assert await has_permission(200, "products.edit") is True          # owner
    assert await has_permission(201, "products.edit") is True          # manager
    assert await has_permission(202, "products.edit") is False         # nobody
    assert await can_access(201, ("products.delete", "products.edit")) is True
    assert await can_access(202, ("products.delete",)) is False


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeEvent:
    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.answered = None

    async def answer(self, text=None, show_alert=False):
        self.answered = text


@pytest.mark.asyncio
async def test_guard_blocks_and_allows(db):
    async with db() as s:
        admin = await make_user(s, 300)
        admin.is_owner = True
        await s.commit()

    calls = []

    @guard("products.edit")
    async def handler(event):
        calls.append(event.from_user.id)
        return "ok"

    denied_event = FakeEvent(999)
    assert await handler(denied_event) is None
    assert denied_event.answered == "دسترسی ندارید."

    allowed_event = FakeEvent(300)
    assert await handler(allowed_event) == "ok"
    assert calls == [300]
