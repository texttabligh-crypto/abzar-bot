"""آخرین روتر: هیچ پیام یا دکمه‌ای بی‌پاسخ نمی‌ماند.

چرا لازم است: دکمه‌های شیشه‌ای (inline) در چت‌های قدیمی همان‌جا می‌مانند و
callback_data قدیمی خودشان را دارند. اگر بعد از به‌روزرسانی، مدیر روی دکمه‌ی
منوی قبلی بزند و هیچ هندلری برایش نباشد، تلگرام فقط اسپینر نشان می‌دهد و
کاربر فکر می‌کند «ربات خراب شده». این روتر پیام روشن می‌دهد.
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(cb: CallbackQuery):
    await cb.answer(
        "این دکمه از منوی قبلی است و دیگر فعال نیست.\n"
        "لطفاً دستور /admin را بزنید و منوی تازه را باز کنید.",
        show_alert=True,
    )


@router.message(F.text)
async def unknown_text(message: Message):
    await message.answer(
        "متوجه نشدم 🤔\n"
        "برای دیدن منو /menu و برای شروع /start را بزنید."
    )
