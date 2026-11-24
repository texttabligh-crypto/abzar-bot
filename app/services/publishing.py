from __future__ import annotations

from datetime import datetime
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import (
    Product,
    ProductImage,
    Brand,
    PublishChannel,
    ProductChannelPost,
    SystemSetting,
    Category,
)
from app.services.catalog import category_path, is_available, RETAIL_MAX_QTY
from app.utils import fa_num

CAPTION_LIMIT = 1024  # Telegram photo caption limit
FOOTER_KEY = "channel_footer"
AUTO_PUBLISH_KEY = "auto_publish_new_products"
CHANNEL_ACTIONS = {"administrator", "creator"}
UNAVAILABLE_NOTE = "⛔ این محصول فعلاً موجود نیست."


def buy_url(bot_username: str, product_id: int) -> str:
    return f"https://t.me/{bot_username}?start=buy_{product_id}"


def buy_markup(bot_username: str | None, product_id: int) -> InlineKeyboardMarkup | None:
    if not bot_username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 خرید / انتخاب تعداد", url=buy_url(bot_username, product_id)),
    ]])


async def bot_username(bot: Bot) -> str | None:
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001 - publishing must not fail on this
        return None
    return getattr(me, "username", None)


def button_message_text(product: Product, available: bool) -> str:
    if available:
        return f"🛒 برای خرید «{product.name}» روی دکمه زیر بزنید."
    return f"«{product.name}»\n{UNAVAILABLE_NOTE}"


def _not_modified(exc: Exception) -> bool:
    return "not modified" in str(exc).lower()


async def _edit_post_message(bot: Bot, chat_ref, message_id: int, text: str, markup):
    """Edit a channel post whatever its kind (photo/album caption or text).

    Album items cannot carry keyboards, so a caption edit is retried without
    the markup before falling back to a text edit.
    """
    attempts = [("caption", markup)]
    if markup is not None:
        attempts.append(("caption", None))
    attempts.append(("text", markup))
    last_exc: Exception | None = None
    for kind, mk in attempts:
        try:
            if kind == "caption":
                await bot.edit_message_caption(
                    chat_id=chat_ref, message_id=message_id, caption=text, reply_markup=mk
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_ref, message_id=message_id, text=text, reply_markup=mk
                )
            return
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                return
            last_exc = exc
    if last_exc:
        raise last_exc


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.rstrip("/").split("/")[-1]
    if value.startswith("t.me/"):
        value = value.rstrip("/").split("/")[-1]
    if value.startswith("@"):
        value = value[1:]
    return value or None


def build_post_url(username: str | None, message_id: int) -> str | None:
    username = normalize_username(username)
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"


async def setting_value(session, key: str, default: str = "") -> str:
    row = await session.get(SystemSetting, key)
    if row and row.value is not None:
        return row.value
    return default


async def set_setting_value(session, key: str, value: str):
    row = await session.get(SystemSetting, key)
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        session.add(SystemSetting(key=key, value=value))


def with_footer(caption: str, footer: str | None) -> str:
    text = caption.strip()
    footer = (footer or "").strip()
    if footer:
        text = f"{text}\n\n{footer}"
    if len(text) > CAPTION_LIMIT:
        text = text[: CAPTION_LIMIT - 1].rstrip() + "…"
    return text


async def product_caption(session, product: Product, include_wholesale: bool = True) -> str:
    brand_name = "بدون برند"
    if product.brand_id:
        brand = await session.get(Brand, product.brand_id)
        if brand:
            brand_name = brand.name

    category_text = ""
    if product.category_id:
        category_obj = await session.get(Category, product.category_id)
        category_text = await category_path(session, category_obj)

    lines = [
        f"🛍 {product.name}",
        f"🏷 برند: {brand_name}",
    ]
    if category_text:
        lines.append(f"🗂 دسته‌بندی: {category_text}")
    lines.append(f"🆔 کد: {product.internal_sku}")
    if product.factory_code:
        lines.append(f"🔧 کد کارخانه: {product.factory_code}")
    if product.model:
        lines.append(f"📐 مدل: {product.model}")
    has_wholesale = bool(product.wholesale_price) and product.wholesale_price != product.base_price
    if include_wholesale and has_wholesale:
        lines.append(f"💰 قیمت خرده (۱ تا {fa_num(RETAIL_MAX_QTY)} عدد): {fa_num(product.base_price)} تومان")
        lines.append(f"📦 قیمت عمده (بیش از {fa_num(RETAIL_MAX_QTY)} عدد): {fa_num(product.wholesale_price)} تومان")
    else:
        lines.append(f"💰 قیمت خرده: {fa_num(product.base_price)} تومان")
    if product.description:
        lines.extend(["", product.description.strip()])
    return "\n".join(lines)


async def active_channel(session) -> PublishChannel | None:
    return await session.scalar(
        select(PublishChannel)
        .where(PublishChannel.is_active.is_(True))
        .order_by(PublishChannel.id)
    )


async def channel_footer(session) -> str:
    return await setting_value(session, FOOTER_KEY, "")


async def post_content(session, product: Product) -> tuple[str, bool]:
    available = is_available(product)
    caption = await product_caption(session, product)
    if not available:
        caption = f"{caption}\n\n{UNAVAILABLE_NOTE}"
    return with_footer(caption, await channel_footer(session)), available


async def _sync_row(bot: Bot, channel: PublishChannel, row: ProductChannelPost,
                    product: Product, caption: str, available: bool, username: str | None):
    markup = buy_markup(username, product.id) if available else None
    if row.button_message_id:
        await _edit_post_message(bot, channel.chat_ref, row.message_id, caption, None)
        try:
            await bot.edit_message_text(
                chat_id=channel.chat_ref, message_id=row.button_message_id,
                text=button_message_text(product, available), reply_markup=markup,
            )
        except TelegramBadRequest:
            pass  # the button message is secondary; never fail the sync on it
    else:
        await _edit_post_message(bot, channel.chat_ref, row.message_id, caption, markup)


async def publish_product(bot: Bot, product_id: int, channel_id: int | None = None) -> ProductChannelPost:
    username = await bot_username(bot)
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            raise ValueError("product not found")

        channel = await session.get(PublishChannel, channel_id) if channel_id else await active_channel(session)
        if not channel:
            raise ValueError("active publish channel not configured")

        caption, available = await post_content(session, product)
        markup = buy_markup(username, product.id) if available else None
        row = await session.scalar(select(ProductChannelPost).where(
            ProductChannelPost.product_id == product.id,
            ProductChannelPost.channel_id == channel.id,
        ))

        # مثل Publisher املاک: اگر قبلاً منتشر شده، اول همان پست قبلی را Sync کن.
        if row:
            try:
                await _sync_row(bot, channel, row, product, caption, available, username)
                row.post_url = build_post_url(channel.username, row.message_id)
                row.updated_at = datetime.utcnow()
                await session.commit()
                await session.refresh(row)
                return row
            except (TelegramBadRequest, TelegramForbiddenError):
                # اگر پست حذف شده یا دیگر قابل ویرایش نیست، پست جدید ساخته می‌شود.
                pass

        images = (await session.execute(
            select(ProductImage)
            .where(ProductImage.product_id == product.id)
            .order_by(ProductImage.sort_order, ProductImage.id)
        )).scalars().all()

        button_message_id = None
        album_size = 1
        # Album captions only accept 1024 chars; keep the footer inside the limit.
        if len(images) > 1:
            media = []
            for idx, img in enumerate(images[:10]):
                media.append(InputMediaPhoto(
                    media=img.telegram_file_id,
                    caption=caption if idx == 0 else None,
                ))
            sent = await bot.send_media_group(channel.chat_ref, media=media)
            first_message_id = sent[0].message_id
            album_size = len(sent)
            if markup:
                btn = await bot.send_message(
                    channel.chat_ref, button_message_text(product, available), reply_markup=markup
                )
                button_message_id = getattr(btn, "message_id", None)
        elif len(images) == 1:
            sent = await bot.send_photo(channel.chat_ref, images[0].telegram_file_id, caption=caption, reply_markup=markup)
            first_message_id = sent.message_id
        elif product.telegram_file_id:
            sent = await bot.send_photo(channel.chat_ref, product.telegram_file_id, caption=caption, reply_markup=markup)
            first_message_id = sent.message_id
        else:
            sent = await bot.send_message(channel.chat_ref, caption, reply_markup=markup)
            first_message_id = sent.message_id

        post_url = build_post_url(channel.username, first_message_id)
        if not row:
            row = ProductChannelPost(
                product_id=product.id, channel_id=channel.id,
                message_id=first_message_id, post_url=post_url,
                button_message_id=button_message_id, album_size=album_size,
            )
            session.add(row)
        else:
            row.message_id = first_message_id
            row.post_url = post_url
            row.button_message_id = button_message_id
            row.album_size = album_size
            row.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(row)
        return row


async def sync_product_post(bot: Bot, product_id: int):
    username = await bot_username(bot)
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            return
        rows = (await session.execute(
            select(ProductChannelPost).where(ProductChannelPost.product_id == product.id)
        )).scalars().all()
        if not rows:
            return
        caption, available = await post_content(session, product)
        for row in rows:
            channel = await session.get(PublishChannel, row.channel_id)
            if not channel or not channel.is_active:
                continue
            try:
                await _sync_row(bot, channel, row, product, caption, available, username)
                row.updated_at = datetime.utcnow()
            except (TelegramBadRequest, TelegramForbiddenError):
                # A deleted/uneditable post should not break price editing.
                pass
        await session.commit()


async def delete_product_posts(bot: Bot, product_ids: list[int]) -> int:
    """Remove channel posts (album items + buy button) of products about to be deleted."""
    removed = 0
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ProductChannelPost).where(ProductChannelPost.product_id.in_(list(product_ids)))
        )).scalars().all()
        for row in rows:
            channel = await session.get(PublishChannel, row.channel_id)
            if not channel:
                continue
            ids = [row.message_id + i for i in range(max(1, int(row.album_size or 1)))]
            if row.button_message_id:
                ids.append(row.button_message_id)
            for mid in ids:
                try:
                    await bot.delete_message(chat_id=channel.chat_ref, message_id=mid)
                    removed += 1
                except Exception:  # noqa: BLE001 - old posts (>48h) may not be deletable
                    pass
            await session.delete(row)
        await session.commit()
    return removed


async def auto_publish_if_enabled(bot: Bot, product_id: int) -> tuple[bool, str]:
    """Publish a freshly created product when the owner enabled auto-posting."""
    async with SessionLocal() as session:
        enabled = await setting_value(session, AUTO_PUBLISH_KEY, "1") == "1"
        channel = await active_channel(session)
    if not enabled or not channel:
        return False, ""
    try:
        row = await publish_product(bot, product_id, channel.id)
        return True, row.post_url or ""
    except Exception as exc:  # noqa: BLE001 - never break product creation
        return False, str(exc)


async def publish_many(
    bot: Bot, product_ids: list[int], channel_id: int | None = None, pause: float = 0.5
) -> tuple[int, list[str]]:
    """Publish many products sequentially.

    Telegram rate limits (≈20 messages/minute in a channel) are awaited and
    retried instead of being reported as failures.
    """
    import asyncio
    from aiogram.exceptions import TelegramRetryAfter

    success = 0
    errors: list[str] = []
    for product_id in product_ids:
        for attempt in range(3):
            try:
                await publish_product(bot, product_id, channel_id)
                success += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{product_id}: {exc}")
                break
        else:
            errors.append(f"{product_id}: rate limit")
        await asyncio.sleep(pause)
    return success, errors


async def connect_channel(bot: Bot, chat_ref: str) -> PublishChannel:
    """Validate that the bot can post in the channel, then register it.

    Raises ValueError with a Persian, user-facing message on any problem.
    """
    ref = (chat_ref or "").strip()
    if not ref:
        raise ValueError("آیدی کانال خالی است.")

    try:
        chat = await bot.get_chat(ref)
    except Exception:
        raise ValueError(
            "کانال پیدا نشد. آیدی را به شکل @username یا -100xxxxxxxxxx بفرستید و مطمئن شوید ربات عضو کانال است."
        )

    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        raise ValueError("ربات عضو این کانال نیست. اول ربات را در کانال ادمین کنید.")

    if getattr(member, "status", None) not in CHANNEL_ACTIONS:
        raise ValueError(
            "ربات در کانال «ادمین» نیست. در تنظیمات کانال، ربات را ادمین کنید و دوباره تلاش کنید."
        )

    if getattr(chat, "type", "") == "channel" and chat.username:
        stored_ref = f"@{chat.username}"
    else:
        stored_ref = str(chat.id)

    async with SessionLocal() as session:
        for other in (await session.execute(select(PublishChannel))).scalars().all():
            other.is_active = False
        existing = await session.scalar(
            select(PublishChannel).where(PublishChannel.chat_ref == stored_ref)
        )
        if existing:
            existing.title = chat.title or existing.title
            existing.username = chat.username
            existing.is_active = True
            channel = existing
        else:
            channel = PublishChannel(
                title=chat.title or stored_ref,
                chat_ref=stored_ref,
                username=chat.username,
                is_active=True,
            )
            session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel
