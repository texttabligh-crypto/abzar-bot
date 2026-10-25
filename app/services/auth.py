from sqlalchemy import select
from app.db.database import SessionLocal
from app.models import User, Permission, UserPermission

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
