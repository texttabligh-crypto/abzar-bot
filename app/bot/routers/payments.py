from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import User, Order, Payment, UserPermission, Permission, AuditLog
from app.bot.keyboards import payment_review, reject_reasons
from app.services.auth import guard
from app.services.catalog import WHOLESALE, customer_type_of
from app.utils import fa_num, order_status_label

router = Router()

REASONS = {
    "amount": "مبلغ اشتباه",
    "unreadable": "فیش ناخوانا",
    "notfound": "واریز مشاهده نشد",
    "other": "سایر",
}


async def get_user(session, tg_id):
    return await session.scalar(select(User).where(User.telegram_id == tg_id))


async def approver_telegram_ids(session):
    perm = await session.scalar(select(Permission).where(Permission.code == "payments.approve"))
    users = (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
    ids = []
    for u in users:
        if u.is_owner:
            ids.append(u.telegram_id)
        elif perm:
            up = await session.scalar(select(UserPermission).where(
                UserPermission.user_id == u.id,
                UserPermission.permission_id == perm.id,
                UserPermission.is_allowed.is_(True),
            ))
            if up:
                ids.append(u.telegram_id)
    return list(dict.fromkeys(ids))


def pick_order_keyboard(orders):
    rows = []
    for o in orders:
        rows.append([InlineKeyboardButton(
            text=f"{o.order_no} | {fa_num(o.total_amount)} تومان",
            callback_data=f"payorder:{o.id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.photo)
async def receive_receipt(message: Message):
    file_id = message.photo[-1].file_id
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        if not user or not user.is_active:
            return
        if customer_type_of(user) == WHOLESALE and not user.is_approved:
            await message.answer("⏳ حساب عمده شما هنوز تأیید نشده است.")
            return
        orders = (await session.execute(
            select(Order).where(
                Order.user_id == user.id,
                Order.status == "WAITING_PAYMENT",
            ).order_by(Order.id.desc()).limit(10)
        )).scalars().all()

        if not orders:
            paid = await session.scalar(
                select(Order.id).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(1)
            )
            if paid:
                await message.answer(
                    "سفارش در انتظار پرداختی ندارید.\n"
                    "اگر می‌خواهید سفارش جدید ثبت کنید، از «🧺 سبد خرید» اقدام کنید."
                )
            else:
                await message.answer("هنوز سفارشی ثبت نکرده‌اید.")
            return

        # یک فیش PENDING برای همان سفارش، مانع ارسال تکراری می‌شود.
        pending = None
        for order in orders:
            existing = await session.scalar(select(Payment).where(
                Payment.order_id == order.id,
                Payment.status == "PENDING",
            ))
            if existing:
                pending = existing
                break
        if pending:
            await message.answer(
                "⏳ یک فیش از شما در حال بررسی است. لطفاً تا تعیین وضعیت صبر کنید.\n"
                "در صورت رد شدن، می‌توانید فیش جدید ارسال کنید."
            )
            return

        if len(orders) > 1:
            await message.answer(
                "این فیش مربوط به کدام سفارش است؟",
                reply_markup=pick_order_keyboard(orders),
            )
            await message.answer(
                "ℹ️ اگر سفارش موردنظر را انتخاب نکنید، فیش ثبت نمی‌شود.",
            )
            # فیش را موقتاً نگه می‌داریم تا در انتخاب سفارش استفاده شود.
            from app.core.redis import redis_client

            try:
                await redis_client.setex(f"receipt:{message.from_user.id}", 900, file_id)
            except Exception:
                await message.answer(
                    "برای ثبت فیش، لطفاً پس از انتخاب سفارش، تصویر را دوباره ارسال کنید."
                )
            return

        order = orders[0]

    await register_receipt(message, order.id, file_id)


async def register_receipt(message: Message, order_id: int, file_id: str):
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        order = await session.get(Order, order_id)
        if not order or order.user_id != user.id:
            return await message.answer("سفارش پیدا نشد.")
        if order.status != "WAITING_PAYMENT":
            return await message.answer("این سفارش دیگر در انتظار پرداخت نیست.")

        payment = Payment(order_id=order.id, receipt_file_id=file_id, status="PENDING")
        session.add(payment)
        order.status = "PAYMENT_REVIEW"
        await session.flush()
        session.add(AuditLog(
            user_id=user.id, action="payment.receipt",
            entity_type="payment", entity_id=str(payment.id),
        ))
        await session.commit()
        await session.refresh(payment)
        recipients = await approver_telegram_ids(session)
        order_no = order.order_no
        total = order.total_amount
        customer = order.customer_name_snapshot or message.from_user.full_name
        shop = order.shop_name_snapshot or "-"

    await message.answer("✅ فیش دریافت شد و برای بررسی مدیریت ارسال شد.")
    caption = (
        f"💳 فیش جدید\nسفارش: {order_no}\n"
        f"مشتری: {customer}\nمغازه: {shop}\n"
        f"مبلغ: {fa_num(total)} تومان"
    )
    for tg_id in recipients:
        try:
            await message.bot.send_photo(
                tg_id, payment.receipt_file_id, caption=caption,
                reply_markup=payment_review(payment.id),
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("payorder:"))
async def choose_receipt_order(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    from app.core.redis import redis_client

    try:
        file_id = await redis_client.get(f"receipt:{cb.from_user.id}")
    except Exception:
        file_id = None
    if not file_id:
        await cb.answer("مدت انتخاب سفارش گذشته است. تصویر فیش را دوباره ارسال کنید.", show_alert=True)
        return
    await redis_client.delete(f"receipt:{cb.from_user.id}")
    await register_receipt(cb.message, order_id, file_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("فیش ثبت شد.")


@router.callback_query(F.data.startswith("payok:"))
@guard("payments.approve")
async def approve_payment(cb: CallbackQuery):
    payment_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        admin = await get_user(session, cb.from_user.id)
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
        order = await session.get(Order, payment.order_id)
        if not order:
            return await cb.answer("سفارش پیدا نشد.", show_alert=True)
        if order.status not in ("PAYMENT_REVIEW", "WAITING_PAYMENT"):
            return await cb.answer(
                f"وضعیت سفارش «{order_status_label(order.status)}» است و تأیید پرداخت معنا ندارد.",
                show_alert=True,
            )
        customer = await session.get(User, order.user_id)
        payment.status = "APPROVED"
        payment.reviewed_by_user_id = admin.id if admin else None
        payment.reviewed_at = datetime.now(timezone.utc)
        order.status = "CONFIRMED"
        session.add(AuditLog(
            user_id=admin.id if admin else None, action="payment.approve",
            entity_type="payment", entity_id=str(payment.id),
            payload={"order_no": order.order_no},
        ))
        await session.commit()
        order_no = order.order_no
        customer_tg = customer.telegram_id if customer else None

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if customer_tg:
        try:
            await cb.bot.send_message(
                customer_tg, f"✅ پرداخت سفارش {order_no} تأیید شد و سفارش با موفقیت ثبت شد."
            )
        except Exception:
            pass
    await cb.answer("تأیید شد.")


@router.callback_query(F.data.startswith("payreject:"))
@guard("payments.approve")
async def reject_payment(cb: CallbackQuery):
    payment_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
    await cb.message.answer("دلیل رد فیش را انتخاب کنید:", reply_markup=reject_reasons(payment_id))
    await cb.answer()


@router.callback_query(F.data.startswith("payreason:"))
@guard("payments.approve")
async def reject_reason(cb: CallbackQuery):
    try:
        _, pid, code = cb.data.split(":")
        payment_id = int(pid)
    except ValueError:
        return await cb.answer("درخواست نامعتبر است.", show_alert=True)
    async with SessionLocal() as session:
        admin = await get_user(session, cb.from_user.id)
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
        order = await session.get(Order, payment.order_id)
        if not order:
            return await cb.answer("سفارش پیدا نشد.", show_alert=True)
        customer = await session.get(User, order.user_id)
        reason = REASONS.get(code, "سایر")
        payment.status = "REJECTED"
        payment.rejection_reason = reason
        payment.reviewed_by_user_id = admin.id if admin else None
        payment.reviewed_at = datetime.now(timezone.utc)
        order.status = "WAITING_PAYMENT"
        session.add(AuditLog(
            user_id=admin.id if admin else None, action="payment.reject",
            entity_type="payment", entity_id=str(payment.id), payload={"reason": reason},
        ))
        await session.commit()
        order_no = order.order_no
        customer_tg = customer.telegram_id if customer else None

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if customer_tg:
        try:
            await cb.bot.send_message(
                customer_tg,
                f"❌ فیش سفارش {order_no} تأیید نشد.\nدلیل: {reason}\n"
                "لطفاً فیش صحیح را دوباره ارسال کنید.",
            )
        except Exception:
            pass
    await cb.answer("رد شد.")
