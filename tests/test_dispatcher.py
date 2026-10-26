"""تست انتها-به-انتها با Dispatcher واقعی aiogram و یک Bot جعلی (بدون شبکه)."""
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, GetChat,
    GetChatMember, GetMe, SendMessage, SendPhoto, SetChatMenuButton, SetMyCommands,
)
from aiogram.types import (
    CallbackQuery, Chat, ChatMemberAdministrator, Message, Update, User as TgUser,
)
from sqlalchemy import select

from app.bot.setup import register_routers
from app.models import User, Product, Category
from app.services.catalog import RETAIL, WHOLESALE


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        return None

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        now = datetime.now(timezone.utc)
        chat = Chat(id=42, type="private")
        if isinstance(method, GetMe):
            return TgUser(id=42, is_bot=True, first_name="TestBot", username="test_bot")
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.requests), date=now, chat=chat, text=method.text)
        if isinstance(method, SendPhoto):
            return Message(message_id=len(self.requests), date=now, chat=chat)
        if isinstance(method, GetChat):
            return Chat(id=-100, type="channel", title="چ", username="ch")
        if isinstance(method, GetChatMember):
            return ChatMemberAdministrator(
                user=TgUser(id=42, is_bot=True, first_name="TestBot"),
                can_be_edited=False, is_anonymous=False,
                can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_promote_members=False, can_change_info=True,
                can_invite_users=True, can_post_messages=True, can_edit_messages=True,
                can_pin_messages=True, can_post_stories=True, can_edit_stories=True,
                can_delete_stories=True,
            )
        # AnswerCallbackQuery / SetMyCommands / edits / ...
        return True

    def sent_texts(self):
        return [m.text for m in self.requests if isinstance(m, SendMessage)]


def tg_user(uid=1, name="ادریس"):
    return TgUser(id=uid, is_bot=False, first_name=name)


def msg_update(uid, text, update_id=1):
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type="private"), from_user=tg_user(uid), text=text,
        ),
    )


def cb_update(uid, data, update_id=2):
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id), from_user=tg_user(uid),
            chat_instance="ci", data=data,
            message=Message(
                message_id=99, date=datetime.now(timezone.utc),
                chat=Chat(id=uid, type="private"), text="x",
            ),
        ),
    )


# روترها singleton هستند، پس Dispatcher فقط یک‌بار در کل ماژول ساخته می‌شود.
_session = FakeSession()
_bot = Bot(token="42:TEST", session=_session)
_dp = Dispatcher(storage=MemoryStorage())
register_routers(_dp)


@pytest.fixture
def app():
    _session.requests.clear()
    return _bot, _dp, _session


@pytest.mark.asyncio
async def test_registration_flow_through_dispatcher(db, app):
    async with db() as s:
        s.add(User(telegram_id=1, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()

    bot, dp, session = app
    await dp.feed_update(bot, msg_update(900, "/start"))
    assert any("نوع خرید" in t for t in session.sent_texts())

    await dp.feed_update(bot, cb_update(900, "ctype:retail"))
    assert any("شماره تماس" in t for t in session.sent_texts())

    await dp.feed_update(bot, msg_update(900, "09351234567", update_id=3))
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 900))
        assert user is not None
        assert user.customer_type == RETAIL
        assert user.phone == "09351234567"


@pytest.mark.asyncio
async def test_admin_cannot_be_reached_without_permission(db, app):
    async with db() as s:
        s.add(User(telegram_id=901, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()
    bot, dp, session = app
    await dp.feed_update(bot, cb_update(901, "admin:add_product"))
    answers = [m for m in session.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers and "دسترسی ندارید" in answers[-1].text
    # هیچ جریان ساخت محصولی شروع نشده است
    assert not any("نام محصول" in t for t in session.sent_texts())


@pytest.mark.asyncio
async def test_owner_opens_admin_menu_and_category_tree(db, app):
    async with db() as s:
        s.add(User(telegram_id=902, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        parent = Category(name="واشر سر سیلندر", is_active=True)
        s.add(parent)
        await s.flush()
        s.add(Category(name="سمند", parent_id=parent.id, is_active=True))
        await s.commit()

    bot, dp, session = app
    await dp.feed_update(bot, cb_update(902, "admin:categories"))
    sends = [m for m in session.requests if isinstance(m, SendMessage)]
    assert sends, "منوی دسته‌بندی باید ارسال شود"
    buttons = [b for m in sends if m.reply_markup
               for row in m.reply_markup.inline_keyboard for b in row]
    labels = [b.text for b in buttons]
    assert any("واشر سر سیلندر" in t for t in labels)
    assert any("سمند" in t and t.startswith("└") for t in labels), "زیرمجموعه باید تودرتو نمایش داده شود"
    assert all(not b.callback_data.endswith(":edit:1") for b in buttons if b.callback_data)
    # هیچ غیرفعال‌سازی خودکاری نباید رخ داده باشد
    async with db() as s:
        cats = (await s.execute(select(Category))).scalars().all()
        assert all(c.is_active for c in cats)


@pytest.mark.asyncio
async def test_owner_channel_settings_screen(db, app):
    async with db() as s:
        s.add(User(telegram_id=903, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()
    bot, dp, session = app
    await dp.feed_update(bot, cb_update(903, "admin:channel_settings"))
    texts = session.sent_texts()
    assert any("اتصال به کانال" in t for t in texts)
    assert any("فوتر" in t for t in texts)


@pytest.mark.asyncio
async def test_full_category_subcategory_and_product_flow(db, app):
    """سناریوی گزارش‌شده، کامل از مسیر Dispatcher و روترهای واقعی."""
    async with db() as s:
        s.add(User(telegram_id=904, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        parent = Category(name="واشر سر سیلندر", is_active=True)
        s.add(parent)
        await s.commit()
        parent_id = parent.id

    bot, dp, session = app
    await dp.feed_update(bot, cb_update(904, "admin:categories"))
    await dp.feed_update(bot, cb_update(904, f"cat:sub:{parent_id}"))
    await dp.feed_update(bot, msg_update(904, "سمند", update_id=4))

    async with db() as s:
        child = await s.scalar(select(Category).where(Category.name == "سمند"))
        assert child is not None and child.parent_id == parent_id
        child_id = child.id
        assert (await s.get(Category, parent_id)).is_active is True

    await dp.feed_update(bot, cb_update(904, f"cat:prod:{child_id}"))
    await dp.feed_update(bot, msg_update(904, "واشر سمند", update_id=5))
    await dp.feed_update(bot, msg_update(904, "100000", update_id=6))
    await dp.feed_update(bot, msg_update(904, "150000", update_id=7))
    await dp.feed_update(bot, msg_update(904, "130000", update_id=8))

    async with db() as s:
        product = await s.scalar(select(Product).where(Product.name == "واشر سمند"))
        assert product is not None
        assert product.category_id == child_id
        assert product.base_price == 150000
        assert product.wholesale_price == 130000
    assert any("قیمت فروش عمده" in t for t in session.sent_texts())


@pytest.mark.asyncio
async def test_channel_connect_then_new_product_published_end_to_end(db, app):
    """اتصال کانال از پنل + انتشار خودکار محصول جدید، همه از مسیر Dispatcher."""
    from aiogram.methods import SendMediaGroup

    async with db() as s:
        s.add(User(telegram_id=905, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()

    bot, dp, session = app

    # ۱) صفحه تنظیمات کانال
    await dp.feed_update(bot, cb_update(905, "admin:channel_settings"))
    assert any("اتصال به کانال" in t for t in session.sent_texts())

    # ۲) کلیک «اتصال / تغییر کانال» و ارسال آیدی
    await dp.feed_update(bot, cb_update(905, "channel:add"))
    assert any("آیدی کانال" in t for t in session.sent_texts())
    await dp.feed_update(bot, msg_update(905, "@mychannel", update_id=20))

    texts = session.sent_texts()
    assert any("با موفقیت متصل شد" in t for t in texts), texts[-3:]

    from app.models import PublishChannel
    async with db() as s:
        channel = await s.scalar(select(PublishChannel).where(PublishChannel.is_active.is_(True)))
        assert channel is not None
        assert channel.chat_ref == "@ch"          # از get_chat خوانده شده
        assert channel.username == "ch"
        channel_ref = channel.chat_ref

    # ۳) تعیین فوتر
    await dp.feed_update(bot, cb_update(905, "channel:footer"))
    await dp.feed_update(bot, msg_update(905, "📞 سفارش: 021-12345678", update_id=21))
    assert any("فوتر ذخیره شد" in t for t in session.sent_texts())

    # ۴) ساخت محصول جدید از پنل و بررسی انتشار خودکار
    async with db() as s:
        parent = Category(name="دسته کانال", is_active=True)
        s.add(parent)
        await s.commit()
        parent_id = parent.id

    before = len(session.requests)
    await dp.feed_update(bot, cb_update(905, f"cat:prod:{parent_id}"))
    await dp.feed_update(bot, msg_update(905, "محصول کانالی", update_id=22))
    await dp.feed_update(bot, msg_update(905, "100000", update_id=23))
    await dp.feed_update(bot, msg_update(905, "160000", update_id=24))
    await dp.feed_update(bot, msg_update(905, "140000", update_id=25))

    new_requests = session.requests[before:]
    to_channel = [
        m for m in new_requests
        if isinstance(m, (SendMessage, SendPhoto, SendMediaGroup))
        and getattr(m, "chat_id", None) == channel_ref
    ]
    assert to_channel, "محصول جدید باید به کانال ارسال شود"
    payload = to_channel[0]
    body = getattr(payload, "text", None) or getattr(payload, "caption", None) or ""
    assert "محصول کانالی" in body
    assert "021-12345678" in body, "فوتر باید به انتهای پیام کانال اضافه شود"
    assert any("منتشر شد" in t for t in session.sent_texts())

    # ۵) خاموش کردن ارسال خودکار و ساخت محصول دوم
    await dp.feed_update(bot, cb_update(905, "channel:toggle_auto"))
    before = len(session.requests)
    await dp.feed_update(bot, cb_update(905, f"cat:prod:{parent_id}"))
    await dp.feed_update(bot, msg_update(905, "محصول دوم", update_id=26))
    await dp.feed_update(bot, msg_update(905, "1000", update_id=27))
    await dp.feed_update(bot, msg_update(905, "2000", update_id=28))
    await dp.feed_update(bot, msg_update(905, "-", update_id=29))
    after = [m for m in session.requests[before:]
             if isinstance(m, (SendMessage, SendPhoto)) and getattr(m, "chat_id", None) == channel_ref]
    assert after == [], "با خاموش بودن ارسال خودکار نباید چیزی به کانال برود"


@pytest.mark.asyncio
async def test_channel_entry_points_visible_in_system_settings(db, app):
    """درخواست کاربر: گزینه اتصال کانال و فوتر از «تنظیمات» هم در دسترس باشد."""
    async with db() as s:
        s.add(User(telegram_id=906, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()

    bot, dp, session = app
    await dp.feed_update(bot, cb_update(906, "admin:system_settings"))

    sends = [m for m in session.requests if isinstance(m, SendMessage)]
    assert sends
    buttons = [b for m in sends if m.reply_markup
               for row in m.reply_markup.inline_keyboard for b in row]
    callbacks = {b.callback_data for b in buttons if b.callback_data}
    assert "admin:channel_settings" in callbacks
    assert "channel:footer" in callbacks
    assert "channel:toggle_auto" in callbacks
    assert any("فوتر پیام کانال" in t for m in sends for t in [m.text])

    # و صفحه‌ی اتصال هم از همین‌جا باز می‌شود
    await dp.feed_update(bot, cb_update(906, "admin:channel_settings"))
    assert any("اتصال به کانال" in t for t in session.sent_texts())


@pytest.mark.asyncio
async def test_version_command_and_admin_panel_show_version(db, app):
    async with db() as s:
        s.add(User(telegram_id=907, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()

    bot, dp, session = app
    await dp.feed_update(bot, msg_update(907, "/version", update_id=40))
    texts = session.sent_texts()
    assert any("2.4.0" in t for t in texts), texts
    assert any("اتصال به کانال" in t for t in texts)   # فهرست قابلیت‌ها برای مدیر

    await dp.feed_update(bot, msg_update(907, "/admin", update_id=41))
    assert any("2.4.0" in t for t in session.sent_texts())


@pytest.mark.asyncio
async def test_old_menu_button_gives_clear_message(db, app):
    """دکمه‌ی منوی نسخه قبلی نباید بی‌پاسخ (اسپینر) بماند."""
    async with db() as s:
        s.add(User(telegram_id=908, is_owner=True, is_active=True, is_approved=True,
                   customer_type=RETAIL, phone="0912"))
        await s.commit()

    bot, dp, session = app
    await dp.feed_update(bot, cb_update(908, "brand:edit:1", update_id=42))
    answers = [m for m in session.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, "باید به کاربر پاسخ داده شود"
    assert "منوی قبلی" in (answers[-1].text or "")
