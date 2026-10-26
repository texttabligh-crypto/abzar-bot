from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    User, Product, Category, PriceTier, PriceHistory, Brand, SystemSetting,
)
from app.services.catalog import RETAIL, WHOLESALE
from tests.fakes import FakeBot, FakeCallback, FakeMessage, FakeUser, FakeContact, make_state

from app.bot.routers import admin as admin_router
from app.bot.routers import common as common_router
from app.bot.routers import customer as customer_router


async def owner(s, tg_id=1):
    u = User(telegram_id=tg_id, first_name="مالک", is_owner=True, is_active=True,
             is_approved=True, customer_type=common_router.RETAIL, phone="0912")
    s.add(u)
    await s.commit()
    return u


@pytest.mark.asyncio
async def test_add_subcategory_then_product_inside_it(db):
    """سناریوی گزارش‌شده: واشر سر سیلندر ← افزودن زیرمجموعه ← افزودن محصول."""
    async with db() as s:
        await owner(s)
        parent = Category(name="واشر سر سیلندر", is_active=True)
        s.add(parent)
        await s.commit()
        parent_id = parent.id

    state = make_state()
    bot = FakeBot()
    user = FakeUser()

    # ۱) افزودن زیرمجموعه «سمند»
    cb = FakeCallback(f"cat:sub:{parent_id}", user=user, bot=bot)
    await admin_router.category_add_sub(cb, state)
    assert "زیرمجموعه" in cb.message.answers[0]

    msg = FakeMessage("سمند", user=user, bot=bot)
    await admin_router.category_save(msg, state)

    async with db() as s:
        child = await s.scalar(select(Category).where(Category.name == "سمند"))
        assert child is not None
        assert child.parent_id == parent_id
        assert child.is_active is True
        # هیچ دسته‌بندی‌ای به‌خاطر باز کردن غیرفعال نشده است
        parent_row = await s.get(Category, parent_id)
        assert parent_row.is_active is True
        child_id = child.id

    # ۲) افزودن محصول داخل زیرمجموعه
    cb2 = FakeCallback(f"cat:prod:{child_id}", user=user, bot=bot)
    await admin_router.category_add_product(cb2, state)

    await admin_router.category_product_name(FakeMessage("واشر سمند", user=user, bot=bot), state)
    await admin_router.category_product_price(FakeMessage("100000", user=user, bot=bot), state)
    await admin_router.category_product_retail(FakeMessage("150000", user=user, bot=bot), state)
    final_msg = FakeMessage("130000", user=user, bot=bot)
    await admin_router.category_product_wholesale(final_msg, state)

    async with db() as s:
        product = await s.scalar(select(Product).where(Product.name == "واشر سمند"))
        assert product is not None
        assert product.category_id == child_id
        assert product.base_price == Decimal("150000")
        assert product.wholesale_price == Decimal("130000")
        assert product.purchase_price == Decimal("100000")
        assert product.internal_sku.startswith("PRD-")


@pytest.mark.asyncio
async def test_brand_open_does_not_deactivate(db):
    async with db() as s:
        await owner(s)
        brand = Brand(name="ایساکو", is_active=True)
        s.add(brand)
        await s.commit()
        bid = brand.id

    cb = FakeCallback(f"brand:view:{bid}")
    await admin_router.brand_view(cb)
    async with db() as s:
        assert (await s.get(Brand, bid)).is_active is True
    assert "ایساکو" in cb.message.answers[0]


@pytest.mark.asyncio
async def test_edit_prices_asked_sequentially(db):
    async with db() as s:
        await owner(s)
        product = Product(
            internal_sku="PRD-100001", name="محصول تست",
            purchase_price=Decimal("100"), base_price=Decimal("200"),
            wholesale_price=Decimal("150"),
        )
        s.add(product)
        await s.commit()
        pid = product.id

    state = make_state()
    await state.update_data(product_id=pid)
    bot = FakeBot()
    user = FakeUser()

    cb = FakeCallback("editp:pricing", user=user, bot=bot)
    await admin_router.edit_pricing_menu(cb, state)
    joined = "\n".join(cb.message.answers)
    assert "قیمت پایه" in joined and "مرحله ۱ از ۳" in joined

    # پایه → خرده → عمده پشت سر هم
    msgs = []
    for value in ("120", "250", "180"):
        m = FakeMessage(value, user=user, bot=bot)
        await admin_router.handle_price_step(m, state, {
            0: "purchase", 1: "retail", 2: "wholesale",
        }[len(msgs)])
        msgs.append(m)

    async with db() as s:
        p = await s.get(Product, pid)
        assert (p.purchase_price, p.base_price, p.wholesale_price) == (
            Decimal("120"), Decimal("250"), Decimal("180")
        )
        history = (await s.execute(select(PriceHistory))).scalars().all()
        assert {h.price_type for h in history} == {"purchase", "retail", "wholesale"}
    assert any("پلکان" in a for a in msgs[-1].answers), "بعد از قیمت‌ها باید پلکان پرسیده شود"


@pytest.mark.asyncio
async def test_edit_prices_skip_and_tier_with_overlap_guard(db):
    async with db() as s:
        await owner(s)
        product = Product(internal_sku="PRD-100002", name="با پلکان",
                          base_price=Decimal("200"), wholesale_price=Decimal("180"))
        s.add(product)
        await s.flush()
        s.add(PriceTier(product_id=product.id, min_qty=100, max_qty=199, unit_price=Decimal("190")))
        await s.commit()
        pid = product.id

    state = make_state()
    await state.update_data(product_id=pid)
    bot = FakeBot()
    user = FakeUser()

    await admin_router.handle_price_step(FakeMessage("-", user=user, bot=bot), state, "purchase")
    await admin_router.handle_price_step(FakeMessage("-", user=user, bot=bot), state, "retail")
    m = FakeMessage("-", user=user, bot=bot)
    await admin_router.handle_price_step(m, state, "wholesale")
    assert any("بدون تغییر" in a or "تغییری ثبت نشد" in a for a in m.answers)

    # افزودن پلکان هم‌پوشان باید رد شود
    cb = FakeCallback("edittier:add", user=user, bot=bot)
    await admin_router.edit_tier_add(cb, state)
    await admin_router.edit_tier_min(FakeMessage("150", user=user, bot=bot), state)
    await admin_router.edit_tier_max(FakeMessage("250", user=user, bot=bot), state)
    overlap_msg = FakeMessage("170", user=user, bot=bot)
    await admin_router.edit_tier_price(overlap_msg, state)
    assert any("هم‌پوشانی" in a for a in overlap_msg.answers)
    async with db() as s:
        tiers = (await s.execute(select(PriceTier))).scalars().all()
        assert len(tiers) == 1

    # بازه غیرهم‌پوشان پذیرفته شود
    await admin_router.edit_tier_min(FakeMessage("200", user=user, bot=bot), state)
    await admin_router.edit_tier_max(FakeMessage("299", user=user, bot=bot), state)
    ok_msg = FakeMessage("175", user=user, bot=bot)
    await admin_router.edit_tier_price(ok_msg, state)
    async with db() as s:
        tiers = (await s.execute(select(PriceTier))).scalars().all()
        assert len(tiers) == 2


@pytest.mark.asyncio
async def test_quick_percent_increase_all_prices_for_all_products(db):
    async with db() as s:
        await owner(s)
        s.add_all([
            Product(internal_sku="PRD-1", name="الف", purchase_price=Decimal("100"),
                    base_price=Decimal("200"), wholesale_price=Decimal("150")),
            Product(internal_sku="PRD-2", name="ب", purchase_price=Decimal("50"),
                    base_price=Decimal("80"), wholesale_price=Decimal("70")),
        ])
        await s.commit()

    state = make_state()
    bot = FakeBot()
    user = FakeUser()

    cb = FakeCallback("pricechg:quick", user=user, bot=bot)
    await admin_router.price_change_quick(cb, state)

    scope = FakeCallback("pricechg:scope:all", user=user, bot=bot)
    await admin_router.price_change_all_scope(scope, state)
    assert any("درصد" in a for a in scope.message.answers)

    preview = FakeMessage("20", user=user, bot=bot)
    await admin_router.price_change_value(preview, state)
    assert any("پیش‌نمایش" in a for a in preview.answers)

    confirm = FakeCallback("pricechg:confirm", user=user, bot=bot)
    await admin_router.price_change_confirm(confirm, state)

    async with db() as s:
        a = await s.scalar(select(Product).where(Product.name == "الف"))
        b = await s.scalar(select(Product).where(Product.name == "ب"))
        assert (a.purchase_price, a.base_price, a.wholesale_price) == (
            Decimal("120"), Decimal("240"), Decimal("180"))
        assert (b.purchase_price, b.base_price, b.wholesale_price) == (
            Decimal("60"), Decimal("96"), Decimal("84"))
        history = (await s.execute(select(PriceHistory))).scalars().all()
        assert len(history) == 6
        assert {h.price_type for h in history} == {"purchase", "retail", "wholesale"}


@pytest.mark.asyncio
async def test_registration_retail_only_asks_phone(db):
    state = make_state(55)
    bot = FakeBot()
    user = FakeUser(id=55)

    await common_router.choose_customer_type(FakeCallback("ctype:retail", user=user, bot=bot), state)
    await common_router.save_retail_phone(FakeMessage("09121112233", user=user, bot=bot), state, "09121112233")

    async with db() as s:
        u = await s.scalar(select(User).where(User.telegram_id == 55))
        assert u.phone == "09121112233"
        assert u.customer_type == RETAIL
        assert u.is_approved is True


@pytest.mark.asyncio
async def test_registration_wholesale_flow_and_approval(db):
    state = make_state(66)
    bot = FakeBot()
    manager = FakeUser(id=1)
    user = FakeUser(id=66, first_name="رضا")

    async with db() as s:
        await owner(s, 1)

    await common_router.choose_customer_type(FakeCallback("ctype:wholesale", user=user, bot=bot), state)
    await common_router.ws_full_name(FakeMessage("رضا محمدی", user=user, bot=bot), state)
    await common_router.ws_shop_name(FakeMessage("فروشگاه رضا", user=user, bot=bot), state)
    await common_router.ws_phone_text(FakeMessage("09351112233", user=user, bot=bot), state)

    addr = FakeMessage("تهران، خیابان ولیعصر، پلاک ۱", user=user, bot=bot)
    await common_router.ws_address(addr, state)

    async with db() as s:
        u = await s.scalar(select(User).where(User.telegram_id == 66))
        assert u.customer_type == WHOLESALE
        assert u.is_approved is False
        assert u.shop_name == "فروشگاه رضا"
        assert u.address.startswith("تهران")
        uid = u.id

    # مدیر باید اعلان با دکمه تأیید گرفته باشد
    assert any("درخواست حساب عمده" in text for _cid, text, _mk in addr.bot.sent)

    # تأیید توسط مدیر
    cb = FakeCallback(f"wsapprove:{uid}", user=manager, bot=bot)
    await admin_router.wholesale_approve(cb)
    async with db() as s:
        u = await s.get(User, uid)
        assert u.is_approved is True
        assert u.approved_at is not None
    assert any(cid == 66 and "تأیید شد" in text for cid, text, _mk in bot.sent)


@pytest.mark.asyncio
async def test_customer_type_text_fallback_reasks(db):
    state = make_state(77)
    await state.set_state(common_router.Registration.customer_type)
    msg = FakeMessage("سلام", user=FakeUser(id=77), bot=FakeBot())
    await common_router.customer_type_retry(msg)
    assert any("خرید خرد" in a for a in msg.answers)


@pytest.mark.asyncio
async def test_wholesale_sees_wholesale_price_and_retail_does_not(db):
    async with db() as s:
        ws = User(telegram_id=70, first_name="عمده", customer_type=WHOLESALE,
                  is_approved=True, is_active=True, phone="0912")
        rt = User(telegram_id=71, first_name="خرد", customer_type=RETAIL,
                  is_approved=True, is_active=True, phone="0913")
        product = Product(internal_sku="PRD-9", name="واشر", base_price=Decimal("200000"),
                          wholesale_price=Decimal("170000"))
        s.add_all([ws, rt, product])
        await s.flush()
        s.add(PriceTier(product_id=product.id, min_qty=10, max_qty=None, unit_price=Decimal("160000")))
        await s.commit()
        pid = product.id

    async with db() as s:
        ws_user = await s.scalar(select(User).where(User.telegram_id == 70))
        rt_user = await s.scalar(select(User).where(User.telegram_id == 71))
        p = await s.get(Product, pid)

    ws_msg = FakeMessage(user=FakeUser(id=70), bot=FakeBot())
    await customer_router.compact_product(ws_msg, p, ws_user)
    ws_text = ws_msg.answers[0]
    assert "قیمت عمده" in ws_text and "۱۷۰٬۰۰۰" in ws_text
    assert "قیمت خرده" in ws_text
    assert "پلکان" in ws_text

    rt_msg = FakeMessage(user=FakeUser(id=71), bot=FakeBot())
    await customer_router.compact_product(rt_msg, p, rt_user)
    rt_text = rt_msg.answers[0]
    assert "قیمت عمده" not in rt_text
    assert "۲۰۰٬۰۰۰" in rt_text


@pytest.mark.asyncio
async def test_pending_wholesale_cannot_browse_products(db):
    async with db() as s:
        s.add(User(telegram_id=80, customer_type=WHOLESALE, is_approved=False, is_active=True))
        s.add(Product(internal_sku="PRD-8", name="محصول", base_price=Decimal("1000")))
        await s.commit()

    msg = FakeMessage(user=FakeUser(id=80), bot=FakeBot())
    async with db() as s:
        result = await customer_router.allowed_user(msg, 80, s)
    assert result is None
    assert any("انتظار تأیید" in a for a in msg.answers)
