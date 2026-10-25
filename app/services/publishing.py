from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aiogram import Bot
from aiogram.types import InputMediaPhoto
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import (
    Product,
    ProductImage,
    Brand,
    PublishChannel,
    ProductChannelPost,
)
from app.utils import fa_num


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.rstrip("/").split("/")[-1]
    if value.startswith("@"): 
        value = value[1:]
    return value or None


def build_post_url(username: str | None, message_id: int) -> str | None:
    username = normalize_username(username)
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"


async def product_caption(session, product: Product) -> str:
    brand_name = "بدون برند"
    if product.brand_id:
        brand = await session.get(Brand, product.brand_id)
        if brand:
            brand_name = brand.name

    lines = [
        f"🛍 {product.name}",
        f"🏷 برند: {brand_name}",
        f"🆔 کد: {product.internal_sku}",
        f"💰 قیمت خرده: {fa_num(product.base_price)} تومان",
        f"🏷 قیمت عمده: {fa_num(product.wholesale_price)} تومان",
    ]
    if product.description:
        lines.extend(["", product.description.strip()])
    return "\n".join(lines)


async def active_channel(session) -> PublishChannel | None:
    return await session.scalar(
        select(PublishChannel)
        .where(PublishChannel.is_active.is_(True))
        .order_by(PublishChannel.id)
    )


async def publish_product(bot: Bot, product_id: int, channel_id: int | None = None) -> ProductChannelPost:
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            raise ValueError("product not found")

        channel = await session.get(PublishChannel, channel_id) if channel_id else await active_channel(session)
        if not channel:
            raise ValueError("active publish channel not configured")

        caption = await product_caption(session, product)
        row = await session.scalar(select(ProductChannelPost).where(
            ProductChannelPost.product_id == product.id,
            ProductChannelPost.channel_id == channel.id,
        ))

        # مثل Publisher املاک: اگر قبلاً منتشر شده، اول همان پست قبلی را Sync کن.
        if row:
            try:
                try:
                    await bot.edit_message_caption(
                        chat_id=channel.chat_ref, message_id=row.message_id, caption=caption
                    )
                except Exception:
                    await bot.edit_message_text(
                        chat_id=channel.chat_ref, message_id=row.message_id, text=caption
                    )
                row.post_url = build_post_url(channel.username, row.message_id)
                row.updated_at = datetime.utcnow()
                await session.commit()
                await session.refresh(row)
                return row
            except Exception:
                # اگر پست حذف شده یا دیگر قابل ویرایش نیست، پست جدید ساخته می‌شود.
                pass

        images = (await session.execute(
            select(ProductImage)
            .where(ProductImage.product_id == product.id)
            .order_by(ProductImage.sort_order, ProductImage.id)
        )).scalars().all()

        if len(images) > 1:
            media = []
            for idx, img in enumerate(images[:10]):
                media.append(InputMediaPhoto(
                    media=img.telegram_file_id,
                    caption=caption if idx == 0 else None,
                ))
            sent = await bot.send_media_group(channel.chat_ref, media=media)
            first_message_id = sent[0].message_id
        elif len(images) == 1:
            sent = await bot.send_photo(channel.chat_ref, images[0].telegram_file_id, caption=caption)
            first_message_id = sent.message_id
        elif product.telegram_file_id:
            sent = await bot.send_photo(channel.chat_ref, product.telegram_file_id, caption=caption)
            first_message_id = sent.message_id
        else:
            sent = await bot.send_message(channel.chat_ref, caption)
            first_message_id = sent.message_id

        post_url = build_post_url(channel.username, first_message_id)
        if not row:
            row = ProductChannelPost(
                product_id=product.id, channel_id=channel.id,
                message_id=first_message_id, post_url=post_url,
            )
            session.add(row)
        else:
            row.message_id = first_message_id
            row.post_url = post_url
            row.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(row)
        return row


async def sync_product_post(bot: Bot, product_id: int):
    async with SessionLocal() as session:
        product = await session.get(Product, product_id)
        if not product:
            return
        rows = (await session.execute(
            select(ProductChannelPost).where(ProductChannelPost.product_id == product.id)
        )).scalars().all()
        caption = await product_caption(session, product)
        for row in rows:
            channel = await session.get(PublishChannel, row.channel_id)
            if not channel or not channel.is_active:
                continue
            try:
                # Works for photo/album first item captions; fallback to text edit.
                try:
                    await bot.edit_message_caption(
                        chat_id=channel.chat_ref,
                        message_id=row.message_id,
                        caption=caption,
                    )
                except Exception:
                    await bot.edit_message_text(
                        chat_id=channel.chat_ref,
                        message_id=row.message_id,
                        text=caption,
                    )
                row.updated_at = datetime.utcnow()
            except Exception:
                # A deleted/uneditable post should not break price editing.
                pass
        await session.commit()


async def publish_many(bot: Bot, product_ids: list[int], channel_id: int | None = None) -> tuple[int, list[str]]:
    success = 0
    errors: list[str] = []
    for product_id in product_ids:
        try:
            await publish_product(bot, product_id, channel_id)
            success += 1
        except Exception as e:
            errors.append(f"{product_id}: {e}")
    return success, errors
