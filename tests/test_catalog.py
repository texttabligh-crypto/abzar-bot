from decimal import Decimal

import pytest

from app.models import Category, Brand, Product, PriceTier, User
from app.services.catalog import (
    category_tree, category_label, category_path, descendant_ids,
    get_price_for_user, WHOLESALE, RETAIL, customer_type_of,
)


@pytest.mark.asyncio
async def test_category_tree_nested_and_labels(db):
    async with db() as s:
        root = Category(name="واشر", is_active=True)
        s.add(root)
        await s.flush()
        head_gasket = Category(name="واشر سر سیلندر", parent_id=root.id, is_active=True)
        s.add(head_gasket)
        await s.flush()
        s.add_all([
            Category(name="سمند", parent_id=head_gasket.id, is_active=True),
            Category(name="پژو", parent_id=head_gasket.id, is_active=True),
            Category(name="غیرفعال", parent_id=head_gasket.id, is_active=False),
        ])
        await s.commit()

        tree = await category_tree(s)
        names = [c.name for c, _ in tree]
        depths = {c.name: d for c, d in tree}
        assert "واشر سر سیلندر" in names
        assert depths["سمند"] == 2
        assert depths["واشر سر سیلندر"] == 1

        active = await category_tree(s, only_active=True)
        assert "غیرفعال" not in [c.name for c, _ in active]

        ids = await descendant_ids(s, head_gasket.id)
        assert set(ids) >= {head_gasket.id}
        assert len(ids) == 4  # خودش + ۳ فرزند (فعال و غیرفعال)

        label = category_label("سمند", 2)
        assert label.startswith("└")
        path = await category_path(s, head_gasket)
        assert path == "واشر › واشر سر سیلندر"


def test_customer_type_defaults():
    class Fake:
        customer_type = None
    assert customer_type_of(None) == RETAIL
    assert customer_type_of(Fake()) == RETAIL
    class WS:
        customer_type = WHOLESALE
    assert customer_type_of(WS()) == WHOLESALE


@pytest.mark.asyncio
async def test_wholesale_vs_retail_pricing_and_tiers(db):
    async with db() as s:
        retail_user = User(telegram_id=1, customer_type=RETAIL, is_approved=True, is_active=True, phone="0912")
        wholesale_user = User(telegram_id=2, customer_type=WHOLESALE, is_approved=True, is_active=True, phone="0913")
        product = Product(
            internal_sku="PRD-100001", name="واشر سر سیلندر سمند",
            purchase_price=Decimal("100000"), base_price=Decimal("150000"),
            wholesale_price=Decimal("130000"),
        )
        s.add_all([retail_user, wholesale_user, product])
        await s.flush()
        s.add(PriceTier(product_id=product.id, min_qty=100, max_qty=None, unit_price=Decimal("120000")))
        await s.commit()

        assert await get_price_for_user(s, product, 1, retail_user) == Decimal("150000")
        assert await get_price_for_user(s, product, 1, wholesale_user) == Decimal("130000")
        # پلکان برای هر دو نوع حساب اولویت دارد
        assert await get_price_for_user(s, product, 150, retail_user) == Decimal("120000")
        assert await get_price_for_user(s, product, 150, wholesale_user) == Decimal("120000")


@pytest.mark.asyncio
async def test_wholesale_without_price_falls_back_to_retail(db):
    async with db() as s:
        user = User(telegram_id=5, customer_type=WHOLESALE, is_approved=True, is_active=True)
        product = Product(
            internal_sku="PRD-100002", name="بدون قیمت عمده",
            base_price=Decimal("90000"), wholesale_price=Decimal("0"),
        )
        s.add_all([user, product])
        await s.commit()
        assert await get_price_for_user(s, product, 1, user) == Decimal("90000")
