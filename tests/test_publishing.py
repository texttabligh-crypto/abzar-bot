from decimal import Decimal

import pytest

from app.models import Product, PublishChannel, ProductChannelPost, ProductImage, SystemSetting, Brand, Category
from app.services.publishing import (
    with_footer, product_caption, publish_product, channel_footer,
    set_setting_value, connect_channel, CAPTION_LIMIT, FOOTER_KEY, build_post_url,
    normalize_username,
)


def test_footer_limits_and_username_normalization():
    assert with_footer("سلام", "فوتر") == "سلام\n\nفوتر"
    assert with_footer("سلام", "") == "سلام"
    long = "ا" * 2000
    out = with_footer(long, "فوتر فروشگاه")
    assert len(out) <= CAPTION_LIMIT
    assert out.endswith("…")
    assert normalize_username("https://t.me/mychannel/") == "mychannel"
    assert normalize_username("@mychannel") == "mychannel"
    assert build_post_url("@mychannel", 12) == "https://t.me/mychannel/12"


class FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.sent.append(("photo", chat_id, photo, caption))
        return FakeSent(100 + len(self.sent))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("text", chat_id, None, text))
        return FakeSent(200 + len(self.sent))

    async def send_media_group(self, chat_id, media, **kwargs):
        self.sent.append(("album", chat_id, len(media), media[0].caption))
        return [FakeSent(300)]

    async def edit_message_caption(self, chat_id, message_id, caption):
        self.edits.append((chat_id, message_id, caption))

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def get_chat(self, ref):
        class Chat:
            id = -100123
            title = "کانال تست"
            username = "test_channel"
            type = "channel"
        if ref in ("@test_channel", "-100123"):
            return Chat()
        raise RuntimeError("chat not found")

    async def get_me(self):
        class Me:
            id = 1
            username = "abzar_test_bot"
        return Me()

    async def get_chat_member(self, chat_id, user_id):
        class Member:
            status = "administrator"
        return Member()


class NotAdminBot(FakeBot):
    async def get_chat_member(self, chat_id, user_id):
        class Member:
            status = "member"
        return Member()


@pytest.mark.asyncio
async def test_caption_contains_footer_and_category(db):
    async with db() as s:
        brand = Brand(name="ایساکو")
        cat = Category(name="واشر سر سیلندر")
        s.add_all([brand, cat])
        await s.flush()
        product = Product(
            internal_sku="PRD-100020", name="واشر سر سیلندر سمند",
            brand_id=brand.id, category_id=cat.id,
            base_price=Decimal("150000"), wholesale_price=Decimal("130000"),
            description="کیفیت اصلی",
        )
        s.add(product)
        await set_setting_value(s, FOOTER_KEY, "📞 021-12345678")
        await s.commit()
        product_id = product.id

        caption = with_footer(await product_caption(s, product), await channel_footer(s))
        assert "ایساکو" in caption
        assert "واشر سر سیلندر" in caption
        assert "۱۵۰٬۰۰۰" in caption or "۱۵۰,۰۰۰" in caption or "۱۵۰٬۰۰۰" in caption
        assert "کیفیت اصلی" in caption
        assert caption.rstrip().endswith("021-12345678")

    await connect_channel(FakeBot(), "@test_channel")  # کانال فعال
    bot = FakeBot()
    post = await publish_product(bot, product_id)
    assert post.message_id
    kind = bot.sent[0][0]
    assert kind == "text"  # بدون عکس
    assert "021-12345678" in bot.sent[0][3]

    # پست دوم: همان پست ویرایش می‌شود، نه ارسال تکراری
    bot2 = FakeBot()
    await publish_product(bot2, product_id)
    assert bot2.sent == []
    assert bot2.edits and bot2.edits[0][1] == post.message_id


@pytest.mark.asyncio
async def test_publish_with_single_image(db):
    async with db() as s:
        product = Product(internal_sku="PRD-100021", name="محصول عکس‌دار", base_price=Decimal("10000"))
        s.add(product)
        await s.flush()
        s.add(ProductImage(product_id=product.id, telegram_file_id="FILE123", sort_order=0))
        await s.commit()
        pid = product.id
    await connect_channel(FakeBot(), "@test_channel")
    bot = FakeBot()
    post = await publish_product(bot, pid)
    assert bot.sent[0][0] == "photo"
    assert bot.sent[0][2] == "FILE123"
    assert post.post_url and post.post_url.startswith("https://t.me/test_channel/")


@pytest.mark.asyncio
async def test_connect_channel_requires_admin(db):
    bot = NotAdminBot()
    with pytest.raises(ValueError) as exc:
        await connect_channel(bot, "@test_channel")
    assert "ادمین" in str(exc.value)

    good = FakeBot()
    channel = await connect_channel(good, "@test_channel")
    assert channel.chat_ref == "@test_channel"
    assert channel.username == "test_channel"
    assert channel.is_active is True

    async with db() as s:
        rows = (await s.execute(__import__("sqlalchemy").select(PublishChannel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].is_active is True


@pytest.mark.asyncio
async def test_connect_channel_unknown_raises(db):
    bot = FakeBot()
    with pytest.raises(ValueError) as exc:
        await connect_channel(bot, "@does_not_exist")
    assert "پیدا نشد" in str(exc.value)
