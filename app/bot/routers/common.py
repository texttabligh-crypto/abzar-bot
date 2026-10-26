import hashlib
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import (
    User, SystemSetting, AdminInvite, Permission, UserPermission,
)
from app.bot.keyboards import (
    main_menu, admin_menu, request_phone_keyboard, customer_type_keyboard,
    wholesale_review_keyboard,
)
from app.services.auth import get_allowed_permission_codes, is_admin_user
from app.services.catalog import RETAIL, WHOLESALE, customer_type_of
from app.services.orders import customer_name
from app.version import version_text, short_version

router = Router()

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="شروع"),
    BotCommand(command="menu", description="منوی اصلی"),
    BotCommand(command="products", description="محصولات"),
    BotCommand(command="cart", description="سبد خرید"),
    BotCommand(command="orders", description="سفارش‌های من"),
    BotCommand(command="price", description="لیست قیمت"),
]

# وضعیت‌های حساب
ACTIVE = "active"
PENDING = "pending"          # حساب عمده، منتظر تأیید مدیریت
INCOMPLETE = "incomplete"    # ثبت‌نام نیمه‌کاره
BLOCKED = "blocked"          # غیرفعال/رد شده


class Registration(StatesGroup):
    customer_type = State()
    retail_phone = State()
    full_name = State()
    shop_name = State()
    phone = State()
    address = State()


async def get_user(tg_id):
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == tg_id))


async def setting(session, key, default=None):
    row = await session.get(SystemSetting, key)
    return row.value if row and row.value is not None else default


def account_state(u) -> str:
    if not u or not u.is_active:
        return BLOCKED
    if u.is_owner:
        return ACTIVE
    if customer_type_of(u) == RETAIL:
        return ACTIVE if u.phone else INCOMPLETE
    return ACTIVE if u.is_approved else PENDING


async def set_chat_admin_commands(bot, tg_id: int, enabled: bool):
    commands = list(PUBLIC_COMMANDS)
    if enabled:
        commands.append(BotCommand(command="admin", description="پنل مدیریت"))
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=tg_id))
    except Exception:
        pass


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
                customer_type=RETAIL,
                is_approved=True,
                is_active=True,
                is_owner=False,
                approved_at=now,
            )
            session.add(user)
            await session.flush()
        else:
            user.first_name = user.first_name or message.from_user.first_name or "مدیر"
            user.last_name = user.last_name or message.from_user.last_name
            user.is_approved = True
            user.is_active = True
            user.approved_at = user.approved_at or now

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


async def send_main_menu(target, user, is_admin: bool):
    label = "عمده (فروشگاهی)" if customer_type_of(user) == WHOLESALE else "خرد"
    name = customer_name(user) or ""
    text = f"سلام {name} خوش آمدید.".replace("  ", " ")
    if is_admin:
        text += "\n\nمنوی اصلی (دسترسی مدیریتی فعال است):"
    else:
        text += f"\nنوع حساب شما: {label}\n\nمنوی اصلی:"
    await target.answer(text, reply_markup=main_menu(is_admin=is_admin, customer_type=customer_type_of(user)))


async def ask_customer_type(message: Message, state: FSMContext):
    await state.set_state(Registration.customer_type)
    await message.answer(
        "سلام 👋\n\n"
        "لطفاً نوع خرید خود را انتخاب کنید:\n\n"
        "🛒 خرید خرد — فقط با شماره تماس، بدون نیاز به تأیید\n"
        "📦 خرید عمده — مخصوص فروشگاه‌ها؛ با نام فروشگاه و آدرس، پس از تأیید مدیریت",
        reply_markup=customer_type_keyboard(),
    )


async def begin_retail_phone(message: Message, state: FSMContext):
    await state.set_state(Registration.retail_phone)
    await message.answer(
        "شماره تماس خود را ارسال کنید یا از دکمه زیر استفاده کنید.",
        reply_markup=request_phone_keyboard(),
    )


async def begin_wholesale(message: Message, state: FSMContext):
    await state.set_state(Registration.full_name)
    await message.answer(
        "📦 ثبت حساب عمده\n\n"
        "چند سؤال کوتاه می‌پرسم و بعد درخواست شما برای مدیریت ارسال می‌شود.\n\n"
        "۱) نام و نام خانوادگی را ارسال کنید.",
        reply_markup=ReplyKeyboardRemove(),
    )


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
                customer_type=RETAIL,
                is_approved=False,
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

    await state.clear()
    admin_enabled = await is_admin_user(message.from_user.id)
    if user.is_owner or admin_enabled:
        await message.answer("منوی مدیریت فعال است.", reply_markup=ReplyKeyboardRemove())
        return await send_main_menu(message, user, True)

    status = account_state(user)
    if status == BLOCKED:
        return await message.answer("⛔ حساب شما توسط مدیریت غیرفعال شده است. برای پیگیری با پشتیبانی تماس بگیرید.")
    if status == PENDING:
        return await message.answer(
            "⏳ درخواست حساب عمده شما در حال بررسی است.\n"
            "پس از تأیید مدیریت، قیمت‌های عمده و پلکانی برای شما فعال می‌شود."
        )
    if status == INCOMPLETE or customer_type_of(user) == WHOLESALE:
        if customer_type_of(user) == WHOLESALE and user.phone and user.shop_name and user.first_name:
            # پروفایل عمده کامل است ولی هنوز تأیید نشده (PENDING بالا هندل شده) — این حالت نادر است.
            return await begin_wholesale(message, state)
        await message.answer("منوی قبلی حذف شد.", reply_markup=ReplyKeyboardRemove())
        return await ask_customer_type(message, state)

    await message.answer("منوی قبلی حذف شد.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message, user, False)


@router.message(Registration.customer_type)
async def customer_type_retry(message: Message):
    await message.answer(
        "لطفاً یکی از دو گزینه را انتخاب کنید:\n"
        "🛒 خرید خرد یا 📦 خرید عمده",
        reply_markup=customer_type_keyboard(),
    )


@router.callback_query(F.data.startswith("ctype:"))
async def choose_customer_type(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":")[1]
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        if not user:
            user = User(telegram_id=cb.from_user.id, customer_type=RETAIL, is_approved=False, is_active=True)
            session.add(user)
            await session.commit()

    if choice == "retail":
        await begin_retail_phone(cb.message, state)
    else:
        await begin_wholesale(cb.message, state)
    await cb.answer()


# ---------------- RETAIL ----------------

@router.message(Registration.retail_phone, F.contact)
async def retail_phone_contact(message: Message, state: FSMContext):
    await save_retail_phone(message, state, message.contact.phone_number)


@router.message(Registration.retail_phone)
async def retail_phone_text(message: Message, state: FSMContext):
    await save_retail_phone(message, state, (message.text or "").strip())


async def save_retail_phone(message: Message, state: FSMContext, phone: str):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 7:
        return await message.answer("شماره تماس معتبر نیست. دوباره ارسال کنید.")
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            return await message.answer("برای شروع «/start» را بزنید.")
        user.phone = phone
        user.customer_type = RETAIL
        user.is_approved = True
        user.is_active = True
        user.approved_at = datetime.now(timezone.utc)
        if not user.first_name:
            user.first_name = message.from_user.first_name
            user.last_name = message.from_user.last_name
        await session.commit()
        await session.refresh(user)
    await state.clear()
    await message.answer("✅ ثبت‌نام شما کامل شد.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message, user, False)


# ---------------- WHOLESALE ----------------

@router.message(Registration.full_name)
async def ws_full_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 3:
        return await message.answer("نام و نام خانوادگی را کامل ارسال کنید.")
    await state.update_data(full_name=text)
    await state.set_state(Registration.shop_name)
    await message.answer("۲) نام فروشگاه را ارسال کنید.")


@router.message(Registration.shop_name)
async def ws_shop_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 2:
        return await message.answer("نام فروشگاه معتبر نیست.")
    await state.update_data(shop_name=text)
    await state.set_state(Registration.phone)
    await message.answer(
        "۳) شماره تماس را ارسال کنید یا از دکمه زیر استفاده کنید.",
        reply_markup=request_phone_keyboard(),
    )


@router.message(Registration.phone, F.contact)
async def ws_phone_contact(message: Message, state: FSMContext):
    await ask_address(message, state, message.contact.phone_number)


@router.message(Registration.phone)
async def ws_phone_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len("".join(ch for ch in text if ch.isdigit())) < 7:
        return await message.answer("شماره تماس معتبر نیست. دوباره ارسال کنید.")
    await ask_address(message, state, text)


async def ask_address(message: Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    await state.set_state(Registration.address)
    await message.answer("۴) آدرس فروشگاه را ارسال کنید.", reply_markup=ReplyKeyboardRemove())


async def notify_wholesale_request(message: Message, user_id: int):
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        perm = await session.scalar(select(Permission).where(Permission.code == "users.manage"))
        managers = (await session.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.id)
        )).scalars().all()
        targets: list[int] = []
        for manager in managers:
            if manager.is_owner:
                targets.append(manager.telegram_id)
            elif perm:
                row = await session.scalar(select(UserPermission).where(
                    UserPermission.user_id == manager.id,
                    UserPermission.permission_id == perm.id,
                    UserPermission.is_allowed.is_(True),
                ))
                if row:
                    targets.append(manager.telegram_id)
        caption = (
            "📦 درخواست حساب عمده\n\n"
            f"نام: {customer_name(user)}\n"
            f"فروشگاه: {user.shop_name or '-'}\n"
            f"تلفن: {user.phone or '-'}\n"
            f"آدرس: {user.address or '-'}\n"
            f"آیدی عددی: {user.telegram_id}"
        )

    for tg_id in dict.fromkeys(targets):
        try:
            await message.bot.send_message(
                tg_id, caption, reply_markup=wholesale_review_keyboard(user_id)
            )
        except Exception:
            pass


@router.message(Registration.address)
async def ws_address(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 5:
        return await message.answer("آدرس را کامل‌تر ارسال کنید.")
    data = await state.get_data()
    full_name = data.get("full_name", "")
    first, _, last = full_name.partition(" ")

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            return await message.answer("برای شروع «/start» را بزنید.")
        user.first_name = first or message.from_user.first_name
        user.last_name = last or None
        user.shop_name = data.get("shop_name")
        user.phone = data.get("phone")
        user.address = text
        user.customer_type = WHOLESALE
        user.is_approved = False
        user.is_active = True
        user.approved_at = None
        await session.commit()
        await session.refresh(user)
        uid = user.id

    await state.clear()
    await message.answer(
        "✅ اطلاعات فروشگاه شما ثبت شد.\n\n"
        "⏳ درخواست حساب عمده برای مدیریت ارسال شد. پس از تأیید، از همین ربات باخبر می‌شوید.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await notify_wholesale_request(message, uid)


# ---------------- COMMON ----------------

@router.message(Command("menu"))
async def menu_cmd(message: Message):
    user = await get_user(message.from_user.id)
    admin_enabled = await is_admin_user(message.from_user.id)
    if not user:
        return await message.answer("برای شروع «/start» را بزنید.")
    status = account_state(user)
    if status == BLOCKED:
        return await message.answer("⛔ حساب شما غیرفعال است.")
    if status == PENDING:
        return await message.answer("⏳ حساب عمده شما در انتظار تأیید مدیریت است.")
    if status == INCOMPLETE:
        return await message.answer("برای تکمیل ثبت‌نام «/start» را بزنید.")
    await send_main_menu(message, user, admin_enabled)


@router.message(Command("version"))
async def version_cmd(message: Message):
    admin_enabled = await is_admin_user(message.from_user.id)
    await message.answer(version_text(is_admin=admin_enabled))


@router.message(Command("admin"))
async def admin_cmd(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not await is_admin_user(message.from_user.id):
        return await message.answer("دسترسی مدیریتی ندارید.")
    allowed = await get_allowed_permission_codes(message.from_user.id)
    await message.answer(
        f"⚙️ پنل مدیریت\n{short_version()}",
        reply_markup=admin_menu(allowed_codes=allowed, is_owner=user.is_owner),
    )


@router.callback_query(F.data == "menu:admin")
async def open_admin(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if not user or not await is_admin_user(cb.from_user.id):
        return await cb.answer("دسترسی مدیریتی ندارید.", show_alert=True)
    allowed = await get_allowed_permission_codes(cb.from_user.id)
    await cb.message.answer(
        f"⚙️ پنل مدیریت\n{short_version()}",
        reply_markup=admin_menu(allowed_codes=allowed, is_owner=user.is_owner),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:back")
async def back(cb: CallbackQuery):
    admin_enabled = await is_admin_user(cb.from_user.id)
    user = await get_user(cb.from_user.id)
    await send_main_menu(cb.message, user, admin_enabled)
    await cb.answer()
