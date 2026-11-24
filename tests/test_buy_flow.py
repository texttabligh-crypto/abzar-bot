"""V2.5: channel buy button, retail≤5 / wholesale>5 pricing, copyable payment,
single-decision receipts, stock reservation, order transitions, brand multi-select."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    User, Product, CartItem, Order, OrderItem, Payment, PriceTier, Brand, Category,
    BankAccount, PublishChannel, ProductChannelPost, ProductImage,
)
from app.bot.routers import customer, common, payments, admin
from app.services.catalog import get_price_for_user, RETAIL_MAX_QTY
from app.services.orders import get_or_create_cart, create_order_from_cart
from app.services.publishing import publish_product, sync_product_post, UNAVAILABLE_NOTE
from tests.fakes import FakeUser, FakeMessage, FakeCallback, FakeContact, make_state


class Sent:
    def __init__(self, message_id):
        self.message_id = message_id


class RecordingBot:
    """Returns message ids and records every call with its keyword arguments."""

    def __init__(self):
        self.calls = []
        self._next = 500

    def _id(self):
        self._next += 1
        return self._next

    async def get_me(self):
        return FakeUser(id=42, username="abzar_bot", is_bot=True)

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.calls.append(("send_message", chat_id, text, reply_markup))
        return Sent(self._id())

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kwargs):
        self.calls.append(("send_photo", chat_id, caption, reply_markup))
        return Sent(self._id())

    async def send_media_group(self, chat_id, media, **kwargs):
        self.calls.append(("send_media_group", chat_id, media[0].caption, None))
        return [Sent(self._id()) for _ in media]

    async def edit_message_caption(self, chat_id, message_id, caption, reply_markup=None, **kwargs):
        self.calls.append(("edit_caption", chat_id, message_id, caption, reply_markup))
        return True

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
        self.calls.append(("edit_text", chat_id, message_id, text, reply_markup))
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        self.calls.append(("edit_markup", chat_id, message_id, reply_markup))
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.calls.append(("delete", chat_id, message_id))
        return True

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row] if markup else []


def callbacks(markup):
    return [b.callback_data for b in buttons(markup)]


async def seed(s, *, stock=None, wholesale=80):
    buyer = User(telegram_id=7100, first_name="Buyer", phone="09120000000",
                 is_active=True, is_approved=True, customer_type="retail")
    product = Product(internal_sku="PRD-B1", name="واشر سر سیلندر", base_price=100,
                      wholesale_price=wholesale, stock_quantity=stock)
    s.add_all([buyer, product])
    await s.commit()
    return buyer, product


# ---------------- pricing ----------------

@pytest.mark.asyncio
async def test_retail_up_to_five_then_wholesale(db):
    async with db() as s:
        buyer, p = await seed(s)
        ws = User(telegram_id=7101, customer_type="wholesale", is_approved=True, is_active=True)
        s.add(ws)
        await s.commit()
        assert await get_price_for_user(s, p, RETAIL_MAX_QTY, buyer) == Decimal("100")
        assert await get_price_for_user(s, p, RETAIL_MAX_QTY + 1, buyer) == Decimal("80")
        assert await get_price_for_user(s, p, 1, ws) == Decimal("80")
        s.add(PriceTier(product_id=p.id, min_qty=10, max_qty=None, unit_price=60))
        await s.commit()
        assert await get_price_for_user(s, p, 10, buyer) == Decimal("60")
        p.wholesale_price = 0
        await s.commit()
        assert await get_price_for_user(s, p, 7, buyer) == Decimal("100")


# ---------------- buy flow ----------------

@pytest.mark.asyncio
async def test_channel_link_opens_quantity_picker_and_adds_to_cart(db):
    async with db() as s:
        _, p = await seed(s)
        pid = p.id
    message = FakeMessage(f"/start buy_{pid}", user=FakeUser(id=7100))
    await common.start(message, make_state(7100))
    card = message.answers[-1]
    assert "واشر سر سیلندر" in card and "۸۰" in card
    assert f"buyadd:{pid}:1" in callbacks(message.markups[-1])

    cb = FakeCallback(f"buyq:{pid}:6", user=FakeUser(id=7100))
    await customer.buy_qty_change(cb)
    assert "۴۸۰" in cb.message.edits[-1]  # 6 × wholesale 80
    assert f"buyadd:{pid}:6" in callbacks(cb.message.edit_markups[-1])

    add = FakeCallback(f"buyadd:{pid}:6", user=FakeUser(id=7100))
    await customer.buy_add(add, make_state(7100))
    async with db() as s:
        item = await s.scalar(select(CartItem))
        assert item.quantity == 6
    summary = add.message.markups[-1]
    assert {"menu:products", "checkout"} <= set(callbacks(summary))
    assert "۴۸۰" in add.message.answers[-1]


@pytest.mark.asyncio
async def test_buy_link_before_registration_resumes_after_phone(db):
    async with db() as s:
        _, p = await seed(s)
        pid = p.id
    state = make_state(7200)
    await state.clear()
    start = FakeMessage(f"/start buy_{pid}", user=FakeUser(id=7200))
    await common.start(start, state)
    assert "ctype:retail" in callbacks(start.markups[-1])
    await common.choose_customer_type(FakeCallback("ctype:retail", user=FakeUser(id=7200)), state)
    phone = FakeMessage(user=FakeUser(id=7200), contact=FakeContact("09121111111"))
    await common.retail_phone_contact(phone, state)
    assert f"buyadd:{pid}:1" in callbacks(phone.markups[-1])


@pytest.mark.asyncio
async def test_checkout_shows_copyable_amount_and_card(db):
    async with db() as s:
        buyer, p = await seed(s)
        s.add(BankAccount(title="اصلی", card_number="6037-9911 2233 4455", holder_name="نیما", iban="IR12 0000"))
        cart = await get_or_create_cart(s, buyer.id)
        s.add(CartItem(cart_id=cart.id, product_id=p.id, quantity=2))
        await s.commit()
    cb = FakeCallback("checkout", user=FakeUser(id=7100), bot=RecordingBot())
    await customer.checkout(cb)
    text = cb.message.answers[-1]
    assert "<code>200</code>" in text
    assert "<code>6037991122334455</code>" in text
    assert "عکس فیش" in text
    copies = [b.copy_text.text for b in buttons(cb.message.markups[-1]) if b.copy_text]
    assert "200" in copies and "6037991122334455" in copies and "IR120000" in copies


# ---------------- orders / stock ----------------

@pytest.mark.asyncio
async def test_order_number_from_id_stock_reserved_and_restored_on_cancel(db):
    async with db() as s:
        buyer, p = await seed(s, stock=10)
        s.add(User(telegram_id=7300, is_owner=True, is_active=True))
        cart = await get_or_create_cart(s, buyer.id)
        s.add(CartItem(cart_id=cart.id, product_id=p.id, quantity=4))
        await s.commit()
        order = await create_order_from_cart(s, buyer.id)
        assert order.order_no == f"ORD-{100000 + order.id}"
        oid, pid = order.id, p.id
    async with db() as s:
        assert (await s.get(Product, pid)).stock_quantity == 6

    bad = FakeCallback(f"orderstatus:{oid}:DELIVERED", user=FakeUser(id=7300), bot=RecordingBot())
    await admin.order_status_change(bad)
    assert bad.alerts
    async with db() as s:
        assert (await s.get(Order, oid)).status == "WAITING_PAYMENT"

    cancel = FakeCallback(f"orderstatus:{oid}:CANCELLED", user=FakeUser(id=7300), bot=RecordingBot())
    await admin.order_status_change(cancel)
    async with db() as s:
        assert (await s.get(Order, oid)).status == "CANCELLED"
        assert (await s.get(Product, pid)).stock_quantity == 10

    again = FakeCallback(f"orderstatus:{oid}:CONFIRMED", user=FakeUser(id=7300), bot=RecordingBot())
    await admin.order_status_change(again)
    assert again.alerts
    async with db() as s:
        assert (await s.get(Order, oid)).status == "CANCELLED"


# ---------------- receipts ----------------

@pytest.mark.asyncio
async def test_first_manager_decision_closes_receipt_for_everyone(db):
    async with db() as s:
        buyer, p = await seed(s)
        s.add_all([
            User(telegram_id=7401, first_name="مدیر یک", is_owner=True, is_active=True),
            User(telegram_id=7402, first_name="مدیر دو", is_owner=True, is_active=True),
        ])
        order = Order(order_no="ORD-T1", user_id=buyer.id, status="WAITING_PAYMENT", total_amount=500)
        s.add(order)
        await s.commit()
        oid = order.id
    bot = RecordingBot()
    receipt = FakeMessage(user=FakeUser(id=7100), bot=bot)
    assert await payments.register_receipt(receipt, oid, "FILE-RECEIPT")
    assert {c[1] for c in bot.of("send_photo")} == {7401, 7402}
    async with db() as s:
        payment = await s.scalar(select(Payment))
        assert len(payment.review_messages["messages"]) == 2
        pay_id = payment.id

    first = FakeCallback(f"payok:{pay_id}", user=FakeUser(id=7401), bot=bot)
    await payments.approve_payment(first)
    edits = bot.of("edit_caption")
    assert {c[1] for c in edits} == {7401, 7402}
    assert all("تأیید شد توسط مدیر یک" in c[3] and c[4] is None for c in edits)
    assert any(c[1] == 7100 and "سفارش شما تأیید شد" in c[2] for c in bot.of("send_message"))

    second = FakeCallback(f"payok:{pay_id}", user=FakeUser(id=7402), bot=bot)
    await payments.approve_payment(second)
    assert second.alerts and "قبلاً" in second.alerts[-1]
    async with db() as s:
        assert (await s.get(Order, oid)).status == "CONFIRMED"


# ---------------- channel posts ----------------

@pytest.mark.asyncio
async def test_channel_post_has_buy_button_and_goes_unavailable(db):
    async with db() as s:
        _, p = await seed(s)
        s.add(PublishChannel(title="کانال", chat_ref="@shop", username="shop", is_active=True))
        await s.commit()
        pid = p.id
    bot = RecordingBot()
    post = await publish_product(bot, pid)
    sent = bot.of("send_message")[-1]
    url = buttons(sent[3])[0].url
    assert url == f"https://t.me/abzar_bot?start=buy_{pid}"
    assert post.button_message_id is None

    async with db() as s:
        (await s.get(Product, pid)).is_active = False
        await s.commit()
    bot2 = RecordingBot()
    await sync_product_post(bot2, pid)
    edit = bot2.calls[0]
    assert UNAVAILABLE_NOTE in edit[3] and edit[4] is None


@pytest.mark.asyncio
async def test_album_gets_separate_buy_button_and_delete_removes_all(db):
    async with db() as s:
        owner = User(telegram_id=7500, is_owner=True, is_active=True)
        p = Product(internal_sku="PRD-ALB", name="آلبوم", base_price=10)
        s.add_all([owner, p, PublishChannel(title="ک", chat_ref="@shop", username="shop", is_active=True)])
        await s.flush()
        s.add_all([ProductImage(product_id=p.id, telegram_file_id=f"F{i}", sort_order=i) for i in range(3)])
        await s.commit()
        pid = p.id
    bot = RecordingBot()
    post = await publish_product(bot, pid)
    assert post.album_size == 3 and post.button_message_id
    assert buttons(bot.of("send_message")[-1][3])[0].url.endswith(f"buy_{pid}")

    cb = FakeCallback(f"prod:delete_confirm:{pid}", user=FakeUser(id=7500), bot=bot)
    await admin.product_delete_apply(cb)
    deleted = {c[2] for c in bot.of("delete")}
    assert deleted == {post.message_id, post.message_id + 1, post.message_id + 2, post.button_message_id}
    async with db() as s:
        assert await s.get(Product, pid) is None
        assert await s.scalar(select(ProductChannelPost)) is None


# ---------------- categories ----------------

@pytest.mark.asyncio
async def test_disabled_category_branch_hidden_from_catalog(db):
    async with db() as s:
        buyer, _ = await seed(s)
        parent = Category(name="غیرفعال", is_active=False)
        s.add(parent)
        await s.flush()
        child = Category(name="زیرمجموعه", parent_id=parent.id)
        s.add(child)
        await s.flush()
        s.add(Product(internal_sku="PRD-H", name="پنهان", base_price=1, category_id=child.id))
        await s.commit()
    message = FakeMessage(user=FakeUser(id=7100))
    await customer.products_cmd(message)
    assert "واشر سر سیلندر" in message.answers[0] and "پنهان" not in message.answers[0]


# ---------------- brand multi-select ----------------

@pytest.mark.asyncio
async def test_brand_multi_select_assigns_checked_products(db):
    async with db() as s:
        s.add(User(telegram_id=7600, is_owner=True, is_active=True))
        brand = Brand(name="ایساکو")
        s.add(brand)
        s.add_all([
            Product(internal_sku="W1", name="واشر سمند", base_price=1),
            Product(internal_sku="W2", name="واشر پژو", base_price=1),
            Product(internal_sku="W3", name="واشر ۴۰۵", base_price=1),
            Product(internal_sku="X1", name="فیلتر روغن", base_price=1),
        ])
        await s.commit()
        bid = brand.id
        ids = {p.internal_sku: p.id for p in (await s.execute(select(Product))).scalars()}
    user = FakeUser(id=7600)
    state = make_state(7600)
    await state.clear()
    await admin.brand_assign_start(FakeCallback(f"brandadd:start:{bid}", user=user), state)
    search = FakeMessage("واشر", user=user)
    await admin.brand_assign_search(search, state)
    listed = callbacks(search.markups[-1])
    assert f"brandadd:t:{ids['W1']}" in listed and f"brandadd:t:{ids['X1']}" not in listed

    await admin.brand_assign_toggle(FakeCallback(f"brandadd:t:{ids['W1']}", user=user), state)
    await admin.brand_assign_toggle(FakeCallback(f"brandadd:t:{ids['W3']}", user=user), state)
    await admin.brand_assign_save(FakeCallback("brandadd:save", user=user, bot=RecordingBot()), state)
    async with db() as s:
        brands = {p.internal_sku: p.brand_id for p in (await s.execute(select(Product))).scalars()}
    assert brands["W1"] == bid and brands["W3"] == bid
    assert brands["W2"] is None and brands["X1"] is None


@pytest.mark.asyncio
async def test_admin_text_forms_survive_photo_instead_of_text(db):
    async with db() as s:
        s.add(User(telegram_id=7700, is_owner=True, is_active=True))
        await s.commit()
    for handler in (admin.edit_product_query, admin.publish_single_query, admin.tier_product, admin.bank_title):
        message = FakeMessage(None, user=FakeUser(id=7700), photo=[object()])
        await handler(message, make_state(7700))
        assert message.answers
