import functools

from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.db.database import SessionLocal
from app.models import User, Permission, UserPermission
from app.services.catalog import WHOLESALE, customer_type_of

# مجوزهای ویژه‌ای که فقط Owner دارد.
OWNER_ONLY = {"owner.only"}


async def get_user_by_tg(tg_id: int):
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == tg_id))


async def get_allowed_permission_codes(tg_id: int) -> set[str]:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == tg_id))
        if not user or not user.is_active:
            return set()
        if user.is_owner:
            rows = (await session.execute(select(Permission.code))).scalars().all()
            return set(rows)
        rows = (await session.execute(
            select(Permission.code)
            .join(UserPermission, UserPermission.permission_id == Permission.id)
            .where(
                UserPermission.user_id == user.id,
                UserPermission.is_allowed.is_(True),
            )
        )).scalars().all()
        return set(rows)


async def is_admin_user(tg_id: int) -> bool:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == tg_id))
        if not user or not user.is_active:
            return False
        if user.is_owner:
            return True
        exists = await session.scalar(
            select(UserPermission.id).where(
                UserPermission.user_id == user.id,
                UserPermission.is_allowed.is_(True),
            ).limit(1)
        )
        return bool(exists)


async def has_permission(tg_id: int, code: str) -> bool:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == tg_id))
        if not user or not user.is_active:
            return False
        if user.is_owner:
            return True
        perm = await session.scalar(select(Permission).where(Permission.code == code))
        if not perm:
            return False
        row = await session.scalar(
            select(UserPermission).where(
                UserPermission.user_id == user.id,
                UserPermission.permission_id == perm.id,
                UserPermission.is_allowed.is_(True),
            )
        )
        return bool(row)


async def can_access(tg_id: int, codes: tuple[str, ...]) -> bool:
    """True when the user is owner or holds any of the given permission codes."""
    for code in codes:
        if code in OWNER_ONLY:
            user = await get_user_by_tg(tg_id)
            return bool(user and user.is_owner)
        if await has_permission(tg_id, code):
            return True
    return False


async def is_wholesale_member(session, tg_id: int) -> bool:
    """Wholesale accounts are only usable after management approval."""
    user = await session.scalar(select(User).where(User.telegram_id == tg_id))
    if not user:
        return False
    return customer_type_of(user) == WHOLESALE and bool(user.is_approved)


def guard(*codes: str):
    """دکوریتور بررسی مجوز برای هندلرهای دکمه‌ای.

    استفاده:
        @router.callback_query(F.data == "...")
        @guard("products.edit")
        async def handler(cb: CallbackQuery): ...

    هندلرهای قدیمی که خودشان مجوز را چک می‌کنند هم می‌توانند از این استفاده کنند؛
    اجرای دوباره‌ی بررسی ضرری ندارد.
    """
    permission_codes = tuple(codes)

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(event, *args, **kwargs):
            tg_id = event.from_user.id if getattr(event, "from_user", None) else None
            if tg_id is None:
                return None
            if not await can_access(tg_id, permission_codes):
                try:
                    if isinstance(event, CallbackQuery):
                        await event.answer("دسترسی ندارید.", show_alert=True)
                    else:
                        await event.answer("دسترسی ندارید.")
                except TypeError:
                    pass
                return None
            return await func(event, *args, **kwargs)

        return wrapper

    return decorator
