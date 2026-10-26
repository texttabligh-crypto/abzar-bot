import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import User, Product, ImportBatch, ImportRow, Supplier
from app.bot.routers import imports as imports_router
from app.bot.routers.imports import price_value, json_safe, classify_rows, PRICE_TYPE_LABELS
from tests.fakes import FakeBot, FakeCallback, FakeMessage, FakeUser, make_state


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def test_price_value_handles_noise():
    assert price_value("۱٬۲۰۰٬۰۰۰") == Decimal("1200000")
    assert price_value("1,200,000 تومان") == Decimal("1200000")
    assert price_value("12500.0") == Decimal("12500")
    assert price_value(1200) == Decimal("1200")
    assert price_value("") is None
    assert price_value(None) is None
    assert price_value("نامعتبر") is None


def test_json_safe_serializes():
    payload = json_safe({"name": "x", "d": date(2026, 1, 1), "dt": datetime(2026, 1, 1, 10, 0),
                         "money": Decimal("10.50")})
    assert json.dumps(payload)  # نباید خطا بدهد
    assert payload["d"] == "2026-01-01"
    assert payload["money"] == "10.50"


@pytest.mark.asyncio
async def test_classify_uses_selected_price_type(db):
    async with db() as s:
        s.add(Product(internal_sku="PRD-1", name="واشر", purchase_price=Decimal("100000"),
                      base_price=Decimal("150000"), wholesale_price=Decimal("130000")))
        supplier = Supplier(name="تأمین‌کننده")
        s.add(supplier)
        await s.commit()
        sid = supplier.id

    rows = [{"name": "واشر", "price": 100000}]
    async with db() as s:
        purchase_status = (await classify_rows(s, rows, sid, "purchase"))[0]["status"]
        retail_status = (await classify_rows(s, rows, sid, "retail"))[0]["status"]
    assert purchase_status == "MATCH"          # قیمت خرید برابر است
    assert retail_status == "PRICE_CHANGED"    # قیمت فروش خرده فرق دارد


@pytest.mark.asyncio
async def test_apply_updates_only_chosen_price(db, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(imports_router, "redis_client", fake_redis)

    async with db() as s:
        admin = User(telegram_id=1, is_owner=True, is_active=True, is_approved=True)
        product = Product(internal_sku="PRD-1", name="واشر", purchase_price=Decimal("100000"),
                          base_price=Decimal("150000"), wholesale_price=Decimal("130000"))
        s.add_all([admin, product])
        await s.flush()
        batch = ImportBatch(original_filename="list.xlsx", status="DRY_RUN",
                            mapping={"name": 1, "price": 2}, summary={})
        s.add(batch)
        await s.flush()
        s.add(ImportRow(batch_id=batch.id, row_number=1, status="MATCH",
                        raw_data={"name": "واشر", "price": 110000},
                        matched_product_id=product.id, confidence=100))
        await s.commit()
        batch_id = batch.id

    fake_redis.store["import:1"] = json.dumps({
        "batch_id": batch_id, "supplier_id": None, "price_type": "purchase",
    })
    cb = FakeCallback("import:apply", user=FakeUser(id=1), bot=FakeBot())
    await imports_router.import_apply(cb, make_state(1))

    async with db() as s:
        p = await s.scalar(select(Product).where(Product.name == "واشر"))
        assert p.purchase_price == Decimal("110000")     # به‌روزرسانی شد
        assert p.base_price == Decimal("150000")         # دست نخورد
        assert p.wholesale_price == Decimal("130000")    # دست نخورد
    assert any("Import اعمال شد" in a for a in cb.message.answers)


@pytest.mark.asyncio
async def test_apply_creates_new_product_with_sane_prices(db, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(imports_router, "redis_client", fake_redis)
    async with db() as s:
        admin = User(telegram_id=1, is_owner=True, is_active=True, is_approved=True)
        s.add(admin)
        await s.flush()
        batch = ImportBatch(original_filename="new.xlsx", status="DRY_RUN",
                            mapping={"name": 1, "price": 2}, summary={})
        s.add(batch)
        await s.flush()
        s.add(ImportRow(batch_id=batch.id, row_number=1, status="NEW",
                        raw_data={"name": "کالای جدید", "price": 80000, "brand": "ایساکو"},
                        matched_product_id=None, confidence=0))
        await s.commit()
        batch_id = batch.id

    fake_redis.store["import:1"] = json.dumps({
        "batch_id": batch_id, "supplier_id": None, "price_type": "retail",
    })
    cb = FakeCallback("import:apply", user=FakeUser(id=1), bot=FakeBot())
    await imports_router.import_apply(cb, make_state(1))

    async with db() as s:
        p = await s.scalar(select(Product).where(Product.name == "کالای جدید"))
        assert p is not None
        assert p.base_price == Decimal("80000")
        assert p.internal_sku.startswith("PRD-")
