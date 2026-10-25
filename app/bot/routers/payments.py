from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import User, Order, Payment, UserPermission, Permission, AuditLog
from app.bot.keyboards import payment_review, reject_reasons
from app.utils import fa_num
from app.services.auth import has_permission

router = Router()

REASONS = {
    "amount":"مبلغ اشتباه",
    "unreadable":"فیش ناخوانا",
    "notfound":"واریز مشاهده نشد",
    "other":"سایر",
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

@router.message(F.photo)
async def receive_receipt(message: Message):
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)
        if not user:
            return
        order = await session.scalar(select(Order).where(
            Order.user_id == user.id,
            Order.status == "WAITING_PAYMENT",
        ).order_by(Order.id.desc()))
        if not order:
            return
        payment = Payment(order_id=order.id, receipt_file_id=message.photo[-1].file_id, status="PENDING")
        session.add(payment)
        order.status = "PAYMENT_REVIEW"
        await session.flush()
        session.add(AuditLog(user_id=user.id, action="payment.receipt", entity_type="payment", entity_id=str(payment.id)))
        await session.commit()
        await session.refresh(payment)
        recipients = await approver_telegram_ids(session)

    await message.answer("✅ فیش دریافت شد و برای بررسی مدیریت ارسال شد.")
    caption = (
        f"💳 فیش جدید\nسفارش: {order.order_no}\n"
        f"مشتری: {order.customer_name_snapshot or message.from_user.full_name}\n"
        f"مغازه: {order.shop_name_snapshot or '-'}\n"
        f"مبلغ: {fa_num(order.total_amount)} تومان"
    )
    for tg_id in recipients:
        try:
            await message.bot.send_photo(tg_id, payment.receipt_file_id, caption=caption, reply_markup=payment_review(payment.id))
        except Exception:
            pass

@router.callback_query(F.data.startswith("payok:"))
async def approve_payment(cb: CallbackQuery):
    if not await has_permission(cb.from_user.id, "payments.approve"):
        return await cb.answer("دسترسی ندارید.", show_alert=True)
    payment_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        admin = await get_user(session, cb.from_user.id)
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
        order = await session.get(Order, payment.order_id)
        customer = await session.get(User, order.user_id)
        payment.status = "APPROVED"
        payment.reviewed_by_user_id = admin.id
        payment.reviewed_at = datetime.utcnow()
        order.status = "CONFIRMED"
        session.add(AuditLog(user_id=admin.id, action="payment.approve", entity_type="payment", entity_id=str(payment.id), payload={"order_no":order.order_no}))
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.bot.send_message(customer.telegram_id, f"✅ پرداخت سفارش {order.order_no} تأیید شد و سفارش با موفقیت ثبت شد.")
    await cb.answer("تأیید شد.")

@router.callback_query(F.data.startswith("payreject:"))
async def reject_payment(cb: CallbackQuery):
    if not await has_permission(cb.from_user.id, "payments.approve"):
        return await cb.answer("دسترسی ندارید.", show_alert=True)
    payment_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
    await cb.message.answer("دلیل رد فیش را انتخاب کنید:", reply_markup=reject_reasons(payment_id))
    await cb.answer()

@router.callback_query(F.data.startswith("payreason:"))
async def reject_reason(cb: CallbackQuery):
    _, pid, code = cb.data.split(":")
    payment_id = int(pid)
    if not await has_permission(cb.from_user.id, "payments.approve"):
        return await cb.answer("دسترسی ندارید.", show_alert=True)
    async with SessionLocal() as session:
        admin = await get_user(session, cb.from_user.id)
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != "PENDING":
            return await cb.answer("این پرداخت قبلاً بررسی شده است.", show_alert=True)
        order = await session.get(Order, payment.order_id)
        customer = await session.get(User, order.user_id)
        reason = REASONS.get(code, "سایر")
        payment.status = "REJECTED"
        payment.rejection_reason = reason
        payment.reviewed_by_user_id = admin.id
        payment.reviewed_at = datetime.utcnow()
        order.status = "WAITING_PAYMENT"
        session.add(AuditLog(user_id=admin.id, action="payment.reject", entity_type="payment", entity_id=str(payment.id), payload={"reason":reason}))
        await session.commit()
    await cb.bot.send_message(customer.telegram_id, f"❌ فیش سفارش {order.order_no} تأیید نشد.\nدلیل: {reason}\nلطفاً فیش صحیح را دوباره ارسال کنید.")
    await cb.answer("رد شد.")
