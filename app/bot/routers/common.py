import hashlib
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import User, SystemSetting, AdminInvite, Permission, UserPermission
from app.bot.keyboards import main_menu, admin_menu, request_phone_keyboard
from app.services.auth import get_allowed_permission_codes, is_admin_user

router = Router()

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="شروع"),
    BotCommand(command="menu", description="منوی اصلی"),
    BotCommand(command="products", description="محصولات"),
    BotCommand(command="cart", description="سبد خرید"),
    BotCommand(command="orders", description="سفارش‌های من"),
    BotCommand(command="price", description="لیست قیمت"),
]

class Registration(StatesGroup):
    full_name = State()
    shop_name = State()
    phone = State()
    city = State()

async def get_user(tg_id):
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == tg_id))

async def setting(session, key, default=None):
    row = await session.get(SystemSetting, key)
    return row.value if row and row.value is not None else default

def complete_profile(u):
    return bool(u and u.first_name and u.phone and u.shop_name and u.city)

async def set_chat_admin_commands(bot, tg_id: int, enabled: bool):
    commands = list(PUBLIC_COMMANDS)
    if enabled:
        commands.append(BotCommand(command="admin", description="پنل مدیریت"))
    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=tg_id))

async def activate_admin_invite(message: Message, token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    async with SessionLocal() as session:
        invite = await session.scalar(
            select(AdminInvite)
            .where(AdminInvite.token_hash == token_hash)
            .with_for_update()
        )
        if not invite or not invite.is_active:
            await message.answer("❌ این لینک دعوت معتبر نیست یا توسط مدیر لغو شده است.")
            return True
        if invite.used_at is not None:
            await message.answer("❌ این لینک دعوت قبلاً استفاده شده است. برای هر مدیر باید لینک جداگانه ساخته شود.")
            return True

        exp = invite.expires_at
        if exp is not None:
            expired = exp < (now if exp.tzinfo else now.replace(tzinfo=None))
            if expired:
                invite.is_active = False
                await session.commit()
                await message.answer("⌛ این لینک دعوت منقضی شده است. از مدیر سیستم یک لینک جدید بگیرید.")
                return True

        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            user = User(
                telegram_id=message.from_user.id,
                first_name=message.from_user.first_name or "مدیر",
                last_name=message.from_user.last_name,
                is_approved=True,
                is_active=True,
                is_owner=False,
            )
            session.add(user)
            await session.flush()
        else:
            user.first_name = user.first_name or message.from_user.first_name or "مدیر"
            user.last_name = user.last_name or message.from_user.last_name
            user.is_approved = True
            user.is_active = True

        selected_codes = set(invite.permissions or [])
        perms = (await session.execute(select(Permission))).scalars().all()
        for perm in perms:
            row = await session.scalar(select(UserPermission).where(
                UserPermission.user_id == user.id,
                UserPermission.permission_id == perm.id,
            ))
            allowed = perm.code in selected_codes
            if row:
                row.is_allowed = allowed
            else:
                session.add(UserPermission(
                    user_id=user.id,
                    permission_id=perm.id,
                    is_allowed=allowed,
                ))

        invite.used_at = now
        invite.used_by_user_id = user.id
        invite.is_active = False
        await session.commit()

    await set_chat_admin_commands(message.bot, message.from_user.id, True)
    allowed = await get_allowed_permission_codes(message.from_user.id)
    await message.answer(
        "✅ شما با موفقیت به‌عنوان مدیر ثبت شدید.\n\n"
        "دسترسی‌های پنل فقط همان مواردی است که برای شما تعیین شده.\n"
        "از این به بعد می‌توانید از دکمه «⚙️ مدیریت» یا دستور /admin استفاده کنید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "⚙️ پنل مدیریت شما:",
        reply_markup=admin_menu(allowed_codes=allowed, is_owner=False),
    )
    return True

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    payload = None
    if message.text and " " in message.text:
        payload = message.text.split(maxsplit=1)[1].strip()
    if payload and payload.startswith("adm_"):
        handled = await activate_admin_invite(message, payload[4:])
        if handled:
            await state.clear()
            return

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            user = User(
                telegram_id=message.from_user.id,
                first_name=None,
                last_name=None,
                is_approved=False,
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

    admin_enabled = await is_admin_user(message.from_user.id)
    if user.is_owner or admin_enabled:
        await message.answer("منوی قدیمی حذف شد.", reply_markup=ReplyKeyboardRemove())
        return await message.answer("سلام، خوش آمدید.\n\nمنوی اصلی:", reply_markup=main_menu(is_admin=True))

    if not complete_profile(user):
        await state.set_state(Registration.full_name)
        return await message.answer("برای شروع، نام و نام خانوادگی را ارسال کنید.")

    if not user.is_approved:
        return await message.answer("⏳ حساب شما در انتظار تأیید مدیریت است.")

    await message.answer("منوی قدیمی حذف شد.", reply_markup=ReplyKeyboardRemove())
    await message.answer("سلام، خوش آمدید.\n\nمنوی اصلی:", reply_markup=main_menu(is_admin=False))

@router.message(Registration.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(Registration.shop_name)
    await message.answer("نام فروشگاه را ارسال کنید.")

@router.message(Registration.shop_name)
async def reg_shop(message: Message, state: FSMContext):
    await state.update_data(shop_name=message.text.strip())
    await state.set_state(Registration.phone)
    await message.answer("شماره تماس را ارسال کنید یا از دکمه زیر استفاده کنید.", reply_markup=request_phone_keyboard())

@router.message(Registration.phone, F.contact)
async def reg_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(Registration.city)
    await message.answer("شهر را ارسال کنید.", reply_markup=ReplyKeyboardRemove())

@router.message(Registration.phone)
async def reg_phone_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 7:
        return await message.answer("شماره تماس معتبر نیست.")
    await state.update_data(phone=text)
    await state.set_state(Registration.city)
    await message.answer("شهر را ارسال کنید.", reply_markup=ReplyKeyboardRemove())

@router.message(Registration.city)
async def reg_city(message: Message, state: FSMContext):
    data = await state.get_data()
    full_name = data["full_name"].split()
    first = full_name[0] if full_name else ""
    last = " ".join(full_name[1:]) if len(full_name) > 1 else None
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        user.first_name = first
        user.last_name = last
        user.shop_name = data["shop_name"]
        user.phone = data["phone"]
        user.city = message.text.strip()
        requires = (await setting(session, "registration_requires_approval", "0")) == "1"
        user.is_approved = not requires
        await session.commit()
    await state.clear()
    if user.is_approved:
        await message.answer("✅ ثبت‌نام کامل شد.", reply_markup=ReplyKeyboardRemove())
        await message.answer("منوی اصلی:", reply_markup=main_menu(False))
    else:
        await message.answer("✅ اطلاعات ثبت شد.\n⏳ پس از تأیید مدیریت می‌توانید از ربات استفاده کنید.", reply_markup=ReplyKeyboardRemove())

@router.message(Command("menu"))
async def menu_cmd(message: Message):
    user = await get_user(message.from_user.id)
    admin_enabled = await is_admin_user(message.from_user.id)
    if not user or (not admin_enabled and not user.is_approved):
        return await message.answer("حساب شما هنوز فعال نشده است.")
    await message.answer("منوی اصلی:", reply_markup=main_menu(is_admin=admin_enabled))

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not await is_admin_user(message.from_user.id):
        return await message.answer("دسترسی مدیریتی ندارید.")
    allowed = await get_allowed_permission_codes(message.from_user.id)
    await message.answer(
        "⚙️ پنل مدیریت",
        reply_markup=admin_menu(allowed_codes=allowed, is_owner=user.is_owner),
    )

@router.callback_query(F.data == "menu:admin")
async def open_admin(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if not user or not await is_admin_user(cb.from_user.id):
        return await cb.answer("دسترسی مدیریتی ندارید.", show_alert=True)
    allowed = await get_allowed_permission_codes(cb.from_user.id)
    await cb.message.answer(
        "⚙️ پنل مدیریت",
        reply_markup=admin_menu(allowed_codes=allowed, is_owner=user.is_owner),
    )
    await cb.answer()

@router.callback_query(F.data == "admin:back")
async def back(cb: CallbackQuery):
    admin_enabled = await is_admin_user(cb.from_user.id)
    await cb.message.answer("منوی اصلی:", reply_markup=main_menu(is_admin=admin_enabled))
    await cb.answer()
