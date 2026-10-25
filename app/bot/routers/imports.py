import os, tempfile, json
from decimal import Decimal
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from rapidfuzz import fuzz

from app.db.database import SessionLocal
from app.core.redis import redis_client
from app.models import (
    User, Supplier, Product, SupplierProduct, ImportBatch, ImportRow,
    Brand, SupplierMapping, SystemSetting
)
from app.services.auth import has_permission
from app.services.catalog import next_sku, set_price
from app.services.excel_import import parse_workbook
from app.bot.keyboards import confirm_import
from app.utils import norm_text, normalize_digits

router = Router()

class ImportFlow(StatesGroup):
    file = State()

def price_value(v):
    try:
        return Decimal(normalize_digits(str(v or "")).replace(",","").replace("٬",""))
    except Exception:
        return None

async def classify_rows(session, rows, supplier_id):
    products = (await session.execute(select(Product))).scalars().all()
    seen = set()
    result = []
    for idx, row in enumerate(rows, start=1):
        name = str(row.get("name") or "").strip()
        price = price_value(row.get("price"))
        sku = str(row.get("sku") or "").strip() or None
        key = (norm_text(name), str(price), sku)
        if key in seen:
            result.append({"row_number":idx,"status":"DUPLICATE","row":row,"product_id":None,"confidence":100,"note":"ردیف تکراری"})
            continue
        seen.add(key)
        if not name or price is None:
            result.append({"row_number":idx,"status":"ERROR","row":row,"product_id":None,"confidence":0,"note":"نام یا قیمت نامعتبر"})
            continue

        product = None
        if supplier_id and sku:
            sp = await session.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id==supplier_id, SupplierProduct.supplier_sku==sku))
            if sp:
                product = await session.get(Product, sp.product_id)
                status = "PRICE_CHANGED" if Decimal(product.base_price or 0) != price else "MATCH"
                result.append({"row_number":idx,"status":status,"row":row,"product_id":product.id,"confidence":100,"note":"Supplier SKU"})
                continue

        exact = next((p for p in products if norm_text(p.name)==norm_text(name)), None)
        if exact:
            status = "PRICE_CHANGED" if Decimal(exact.base_price or 0) != price else "MATCH"
            result.append({"row_number":idx,"status":status,"row":row,"product_id":exact.id,"confidence":100,"note":"نام دقیق"})
            continue

        best, best_score = None, 0
        for p in products:
            score = fuzz.ratio(norm_text(p.name), norm_text(name))
            if score > best_score:
                best, best_score = p, score
        if best and best_score >= 94:
            status = "PRICE_CHANGED" if Decimal(best.base_price or 0) != price else "MATCH"
            result.append({"row_number":idx,"status":status,"row":row,"product_id":best.id,"confidence":best_score,"note":"شباهت بالا"})
        elif best and best_score >= 75:
            result.append({"row_number":idx,"status":"UNCERTAIN","row":row,"product_id":best.id,"confidence":best_score,"note":f"مشابه: {best.name}"})
        else:
            result.append({"row_number":idx,"status":"NEW","row":row,"product_id":None,"confidence":best_score,"note":"محصول جدید"})
    return result

@router.callback_query(F.data == "admin:excel")
async def import_start(cb: CallbackQuery, state: FSMContext):
    if not await has_permission(cb.from_user.id, "excel.import"):
        return await cb.answer("دسترسی ندارید.", show_alert=True)
    async with SessionLocal() as session:
        suppliers = (await session.execute(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name))).scalars().all()
    kb = [[InlineKeyboardButton(text=s.name, callback_data=f"import:supplier:{s.id}")] for s in suppliers]
    kb.append([InlineKeyboardButton(text="بدون تأمین‌کننده", callback_data="import:supplier:0")])
    await cb.message.answer("تأمین‌کننده این فایل را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("import:supplier:"))
async def import_supplier(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await state.update_data(supplier_id=sid or None)
    await state.set_state(ImportFlow.file)
    await cb.message.answer("فایل Excel با پسوند xlsx را ارسال کنید.")
    await cb.answer()

@router.message(ImportFlow.file, F.document)
async def import_file(message: Message, state: FSMContext):
    if not message.document.file_name.lower().endswith(".xlsx"):
        return await message.answer("فقط فایل xlsx پذیرفته می‌شود.")
    if message.document.file_size and message.document.file_size > 15 * 1024 * 1024:
        return await message.answer("حجم فایل بیشتر از ۱۵ مگابایت است.")
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        await message.bot.download(message.document, destination=path)
        report = parse_workbook(path)
        if "name" not in report["mapping"] or "price" not in report["mapping"]:
            return await message.answer(f"ستون‌های نام/قیمت شناسایی نشد.\nMapping: {report['mapping']}")
        data = await state.get_data()
        async with SessionLocal() as session:
            actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
            classified = await classify_rows(session, report["rows"], data.get("supplier_id"))
            counts = {}
            for x in classified:
                counts[x["status"]] = counts.get(x["status"],0)+1
            batch = ImportBatch(
                supplier_id=data.get("supplier_id"),
                original_filename=message.document.file_name,
                status="DRY_RUN",
                mapping=report["mapping"],
                summary=counts,
                created_by_user_id=actor.id if actor else None,
            )
            session.add(batch)
            await session.flush()
            for x in classified:
                session.add(ImportRow(
                    batch_id=batch.id,row_number=x["row_number"],status=x["status"],
                    raw_data=x["row"],matched_product_id=x["product_id"],confidence=x["confidence"],note=x["note"]
                ))
            await session.commit()
            batch_id = batch.id

        payload = {"batch_id":batch_id,"supplier_id":data.get("supplier_id"),"filename":message.document.file_name,"mapping":report["mapping"]}
        await redis_client.setex(f"import:{message.from_user.id}",3600,json.dumps(payload,ensure_ascii=False))
        text = (
            f"✅ فایل تحلیل شد\nتعداد ردیف: {len(report['rows'])}\n"
            f"New: {counts.get('NEW',0)}\nMatch: {counts.get('MATCH',0)}\n"
            f"Price Changed: {counts.get('PRICE_CHANGED',0)}\nUncertain: {counts.get('UNCERTAIN',0)}\n"
            f"Duplicate: {counts.get('DUPLICATE',0)}\nError: {counts.get('ERROR',0)}\n\n"
            f"Mapping: {report['mapping']}"
        )
        await message.answer(text, reply_markup=confirm_import(batch_id))
    finally:
        if os.path.exists(path):
            os.remove(path)

@router.callback_query(F.data.startswith("import:review:"))
async def import_review(cb: CallbackQuery):
    batch_id = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        rows = (await session.execute(select(ImportRow).where(ImportRow.batch_id==batch_id, ImportRow.status=="UNCERTAIN").order_by(ImportRow.row_number).limit(25))).scalars().all()
        if not rows:
            await cb.message.answer("مورد مشکوکی وجود ندارد.")
        else:
            for r in rows:
                p = await session.get(Product, r.matched_product_id) if r.matched_product_id else None
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ همین محصول است", callback_data=f"uncertain:match:{r.id}"),
                    InlineKeyboardButton(text="➕ محصول جدید", callback_data=f"uncertain:new:{r.id}"),
                ]])
                await cb.message.answer(
                    f"ردیف {r.row_number}\nExcel: {r.raw_data.get('name')}\n"
                    f"مشابه: {p.name if p else '-'}\nاطمینان: {r.confidence}٪",
                    reply_markup=kb
                )
    await cb.answer()

@router.callback_query(F.data.startswith("uncertain:"))
async def uncertain_decision(cb: CallbackQuery):
    _, decision, rid = cb.data.split(":")
    async with SessionLocal() as session:
        r = await session.get(ImportRow, int(rid))
        r.status = "MATCH" if decision=="match" else "NEW"
        if decision=="new":
            r.matched_product_id = None
        await session.commit()
    await cb.answer("ثبت شد.")

@router.callback_query(F.data == "import:cancel")
async def import_cancel(cb: CallbackQuery, state: FSMContext):
    await redis_client.delete(f"import:{cb.from_user.id}")
    await state.clear()
    await cb.message.answer("Import لغو شد.")
    await cb.answer()

@router.callback_query(F.data == "import:apply")
async def import_apply(cb: CallbackQuery, state: FSMContext):
    if not await has_permission(cb.from_user.id, "excel.import"):
        return await cb.answer("دسترسی ندارید.", show_alert=True)
    raw = await redis_client.get(f"import:{cb.from_user.id}")
    if not raw:
        return await cb.answer("اطلاعات Import منقضی شده.", show_alert=True)
    payload = json.loads(raw)
    batch_id = payload["batch_id"]
    created = updated = skipped = 0
    changed_count = 0
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        batch = await session.get(ImportBatch, batch_id)
        rows = (await session.execute(select(ImportRow).where(ImportRow.batch_id==batch_id).order_by(ImportRow.row_number))).scalars().all()
        if any(r.status=="UNCERTAIN" for r in rows):
            return await cb.answer("ابتدا همه موارد مشکوک را بررسی کنید.", show_alert=True)
        supplier_id = batch.supplier_id
        for r in rows:
            if r.status in ("DUPLICATE","ERROR"):
                skipped += 1
                continue
            row = r.raw_data
            name = str(row.get("name") or "").strip()
            price = price_value(row.get("price"))
            sku = str(row.get("sku") or "").strip() or None
            brand_name = str(row.get("brand") or "").strip() or None
            if price is None:
                skipped += 1
                continue
            product = await session.get(Product, r.matched_product_id) if r.matched_product_id else None
            if product:
                old = Decimal(product.base_price or 0)
                await set_price(session, product, price, actor.id if actor else None, "excel")
                updated += 1
                if old != price:
                    changed_count += 1
            else:
                brand_id = None
                if brand_name:
                    brand = await session.scalar(select(Brand).where(Brand.name.ilike(brand_name)))
                    if not brand:
                        brand = Brand(name=brand_name)
                        session.add(brand)
                        await session.flush()
                    brand_id = brand.id
                product = Product(
                    internal_sku=await next_sku(session), name=name,
                    purchase_price=price, base_price=price, wholesale_price=price,
                    brand_id=brand_id
                )
                session.add(product)
                await session.flush()
                created += 1

            if supplier_id:
                sp = None
                if sku:
                    sp = await session.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id==supplier_id, SupplierProduct.supplier_sku==sku))
                if not sp:
                    session.add(SupplierProduct(supplier_id=supplier_id,product_id=product.id,supplier_sku=sku,supplier_name=name,last_supplier_price=price))
                else:
                    sp.last_supplier_price=price
                    sp.supplier_name=name

        if supplier_id:
            supplier = await session.get(Supplier, supplier_id)
            if supplier:
                supplier.last_import_at = datetime.utcnow()
            mapping = await session.scalar(select(SupplierMapping).where(SupplierMapping.supplier_id==supplier_id))
            if mapping:
                mapping.header_mapping = batch.mapping or {}
            else:
                session.add(SupplierMapping(supplier_id=supplier_id,header_mapping=batch.mapping or {}))
        batch.status="APPLIED"
        batch.summary={"created":created,"updated":updated,"skipped":skipped,"price_changed":changed_count}
        notify = await session.get(SystemSetting,"notify_price_changes")
        users = []
        if notify and notify.value=="1" and changed_count:
            users = (await session.execute(select(User).where(User.is_active.is_(True),User.is_approved.is_(True)))).scalars().all()
        await session.commit()

    if users:
        for u in users:
            try:
                await cb.bot.send_message(u.telegram_id, f"📈 لیست قیمت بروزرسانی شد.\n{changed_count} کالا تغییر قیمت داشتند.")
            except Exception:
                pass

    await redis_client.delete(f"import:{cb.from_user.id}")
    await state.clear()
    await cb.message.answer(f"✅ Import اعمال شد.\nمحصول جدید: {created}\nبه‌روزرسانی: {updated}\nردشده: {skipped}")
    await cb.answer()
