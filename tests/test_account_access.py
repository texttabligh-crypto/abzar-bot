import pytest
from sqlalchemy import select
from app.models import User, Permission, UserPermission
from app.bot.routers.customer import allowed_user
from tests.fakes import FakeMessage


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,phone,approved,active,owner,expected', [
    ('retail', None, False, True, True, True),
    ('wholesale', None, False, True, True, True),
    ('retail', None, True, True, False, False),
    ('retail', '0912', True, True, False, True),
    ('wholesale', '0912', False, True, False, False),
    ('wholesale', '0912', True, True, False, True),
    ('retail', None, True, False, True, False),
])
async def test_shared_access_policy(db, kind, phone, approved, active, owner, expected):
    async with db() as s:
        user = User(telegram_id=2000, customer_type=kind, phone=phone, is_approved=approved,
                    is_active=active, is_owner=owner)
        s.add(user)
        await s.commit()
        assert bool(await allowed_user(FakeMessage(), 2000, s)) is expected
        assert await allowed_user(FakeMessage(), 99999, s) is None


@pytest.mark.asyncio
async def test_revoked_manager_no_longer_gets_staff_exemption(db):
    async with db() as s:
        user = User(telegram_id=2001, phone=None, is_active=True, customer_type='retail')
        permission = Permission(code='products.edit', title='Edit')
        s.add_all([user, permission])
        await s.flush()
        grant = UserPermission(user_id=user.id, permission_id=permission.id, is_allowed=True)
        s.add(grant)
        await s.commit()
        assert await allowed_user(FakeMessage(), 2001, s) is user
        grant.is_allowed = False
        await s.commit()
        target = FakeMessage()
        assert await allowed_user(target, 2001, s) is None
        assert any('تکمیل ثبت' in text for text in target.answers)
