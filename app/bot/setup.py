from aiogram import Dispatcher, Bot
from aiogram.types import BotCommand, MenuButtonCommands, BotCommandScopeChat
from sqlalchemy import select

from app.core.config import settings
from app.db.database import SessionLocal
from app.models import User, Role, Permission, BankAccount, SystemSetting, UserPermission

from app.bot.routers.common import router as common_router
from app.bot.routers.customer import router as customer_router
from app.bot.routers.payments import router as payments_router
from app.bot.routers.admin import router as admin_router
from app.bot.routers.imports import router as import_router
from app.bot.routers.fallback import router as fallback_router

PERMS = [
    ("products.view","مشاهده محصولات"),
    ("products.create","ایجاد محصول"),
    ("products.edit","ویرایش محصول"),
    ("products.delete","حذف/غیرفعال محصول"),
    ("excel.import","ورود اکسل"),
    ("orders.view","مشاهده سفارش‌ها"),
    ("orders.manage","مدیریت سفارش‌ها"),
    ("payments.view","مشاهده پرداخت‌ها"),
    ("payments.approve","تأیید پرداخت"),
    ("users.manage","مدیریت کاربران"),
    ("admins.manage","مدیریت مدیران"),
    ("reports.view","گزارش‌ها"),
    ("settings.manage","تنظیمات"),
    ("channel.publish","ارسال به کانال"),
    ("channel.manage","اتصال و مدیریت کانال"),
]

async def seed_defaults():
    async with SessionLocal() as session:
        for code, title in PERMS:
            if not await session.scalar(select(Permission).where(Permission.code == code)):
                session.add(Permission(code=code, title=title))
        for name in ("owner", "admin", "customer"):
            if not await session.scalar(select(Role).where(Role.name == name)):
                session.add(Role(name=name, description=name))
        await session.commit()

        if settings.owner_telegram_id:
            owner = await session.scalar(select(User).where(User.telegram_id == settings.owner_telegram_id))
            if not owner:
                owner = User(
                    telegram_id=settings.owner_telegram_id,
                    first_name="Owner",
                    is_owner=True,
                    is_approved=True,
                    is_active=True,
                )
                session.add(owner)
            else:
                owner.is_owner = True
                owner.is_approved = True
                owner.is_active = True

        # اگر حساب بانکی از env آمده و هنوز حسابی در DB نیست، یک‌بار Seed شود.
        if settings.card_number and not await session.scalar(select(BankAccount).limit(1)):
            session.add(BankAccount(
                title="حساب اصلی",
                card_number=settings.card_number,
                holder_name=settings.card_holder or None,
                iban=settings.iban or None,
                is_active=True,
            ))
        defaults = {
            "registration_requires_approval": "0",
            "support_text": "برای پشتیبانی با مدیریت شرکت تماس بگیرید.",
            "company_contact": "",
            "notify_price_changes": "0",
            # V2.4
            "channel_footer": "",
            "auto_publish_new_products": "1",
        }
        for key, value in defaults.items():
            if not await session.get(SystemSetting, key):
                session.add(SystemSetting(key=key, value=value))
        await session.commit()

async def setup_bot_ui(bot: Bot):
    public_commands = [
        BotCommand(command="start", description="شروع"),
        BotCommand(command="menu", description="منوی اصلی"),
        BotCommand(command="products", description="محصولات"),
        BotCommand(command="cart", description="سبد خرید"),
        BotCommand(command="orders", description="سفارش‌های من"),
        BotCommand(command="price", description="لیست قیمت"),
    ]
    await bot.set_my_commands(public_commands)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    admin_commands = public_commands + [BotCommand(command="admin", description="پنل مدیریت")]
    if settings.owner_telegram_id:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=settings.owner_telegram_id),
        )

    async with SessionLocal() as session:
        manager_ids = (await session.execute(
            select(User.telegram_id)
            .join(UserPermission, UserPermission.user_id == User.id)
            .where(
                User.is_active.is_(True),
                User.is_owner.is_(False),
                UserPermission.is_allowed.is_(True),
            )
            .distinct()
        )).scalars().all()
    for tg_id in manager_ids:
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=tg_id),
            )
        except Exception:
            pass

def register_routers(dp: Dispatcher):
    dp.include_router(common_router)
    dp.include_router(admin_router)
    dp.include_router(import_router)
    dp.include_router(customer_router)
    dp.include_router(payments_router)
    # همیشه آخر: پاسخ دادن به دکمه‌های قدیمی و پیام‌های بی‌صاحب
    dp.include_router(fallback_router)
