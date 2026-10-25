from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from sqlalchemy import select, or_

from app.db.database import SessionLocal
from app.models import (
    User, Product, Brand, Category, SupplierProduct, CartItem, Order,
    PriceHistory, BankAccount, ProductChannelPost, SystemSetting
)
from app.bot.keyboards import product_list_actions, cart_actions, checkout_button, price_list_formats
from app.services.orders import get_or_create_cart, create_order_from_cart
from app.services.catalog import get_price_for_qty
from app.services.export import build_price_list, build_price_list_pdf
from app.utils import fa_num, normalize_digits

router = Router()

class CartQuantity(StatesGroup):
    qty = State()

class SearchState(StatesGroup):
    query = State()

async def db_user(session, tg_id):
    return await session.scalar(select(User).where(User.telegram_id == tg_id))

async def compact_product(target, p):
    async with SessionLocal() as session:
        brand = await session.get(Brand, p.brand_id) if p.brand_id else None
        post = await session.scalar(
            select(ProductChannelPost).where(ProductChannelPost.product_id == p.id).order_by(ProductChannelPost.id.desc())
        )
    lines = [f"📦 {p.name}", f"کد: {p.internal_sku}"]
    if p.factory_code:
        lines.append(f"کد کارخانه: {p.factory_code}")
    if brand:
        lines.append(f"برند: {brand.name}")
    lines.append(f"قیمت خرده: {fa_num(p.base_price)} تومان")
    if p.wholesale_price is not None and p.wholesale_price != p.base_price:
        lines.append(f"قیمت عمده: {fa_num(p.wholesale_price)} تومان")
    lines.append(f"وضعیت: {p.stock_status}")
    await target.answer("\n".join(lines), reply_markup=product_list_actions(p.id, post.post_url if post else None))

async def show_products(target, stmt=None):
    async with SessionLocal() as session:
        stmt = stmt or select(Product).where(Product.is_active.is_(True))
        rows = (await session.execute(stmt.order_by(Product.name).limit(50))).scalars().all()
    if not rows:
        return await target.answer("محصولی پیدا نشد.")
    for p in rows:
        await compact_product(target, p)

@router.message(Command("products"))
async def products_cmd(message: Message):
    await show_products(message)

@router.callback_query(F.data == "menu:products")
async def products_inline(cb: CallbackQuery):
    await show_products(cb.message)
    await cb.answer()

@router.callback_query(F.data == "menu:brands")
async def brands(cb: CallbackQuery):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name))).scalars().all()
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = [[InlineKeyboardButton(text=b.name, callback_data=f"browsebrand:{b.id}")] for b in rows]
    await cb.message.answer("🏷 برندها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)
    await cb.answer()

@router.callback_query(F.data.startswith("browsebrand:"))
async def browse_brand(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    await show_products(cb.message, select(Product).where(Product.is_active.is_(True), Product.brand_id == bid))
    await cb.answer()

@router.callback_query(F.data == "menu:categories")
async def categories(cb: CallbackQuery):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Category).where(Category.is_active.is_(True), Category.parent_id.is_(None)).order_by(Category.name))).scalars().all()
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = [[InlineKeyboardButton(text=c.name, callback_data=f"browsecat:{c.id}")] for c in rows]
    await cb.message.answer("🗂 دسته‌بندی‌ها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)
    await cb.answer()

@router.callback_query(F.data.startswith("browsecat:"))
async def browse_cat(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        children = (await session.execute(select(Category).where(Category.is_active.is_(True), Category.parent_id == cid).order_by(Category.name))).scalars().all()
    if children:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = [[InlineKeyboardButton(text=c.name, callback_data=f"browsecat:{c.id}")] for c in children]
        kb.append([InlineKeyboardButton(text="📦 محصولات این دسته", callback_data=f"browsecatproducts:{cid}")])
        await cb.message.answer("زیرمجموعه‌ها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        await show_products(cb.message, select(Product).where(Product.is_active.is_(True), Product.category_id == cid))
    await cb.answer()

@router.callback_query(F.data.startswith("browsecatproducts:"))
async def browse_cat_products(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    await show_products(cb.message, select(Product).where(Product.is_active.is_(True), Product.category_id == cid))
    await cb.answer()

@router.callback_query(F.data == "menu:support")
async def support(cb: CallbackQuery):
    async with SessionLocal() as session:
        row = await session.get(SystemSetting, "support_text")
    await cb.message.answer(row.value if row and row.value else "برای پشتیبانی با مدیریت شرکت تماس بگیرید.")
    await cb.answer()

@router.callback_query(F.data == "menu:search")
async def search_inline(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await cb.message.answer("نام، کد داخلی، کد کارخانه، کد تأمین‌کننده، برند یا مدل را ارسال کنید.")
    await cb.answer()

@router.message(SearchState.query)
async def search_result(message: Message, state: FSMContext):
    q = message.text.strip()
    async with SessionLocal() as session:
        brand_ids = (await session.execute(select(Brand.id).where(Brand.name.ilike(f"%{q}%")))).scalars().all()
        supplier_product_ids = (await session.execute(
            select(SupplierProduct.product_id).where(SupplierProduct.supplier_sku.ilike(f"%{q}%"))
        )).scalars().all()
        rows = (await session.execute(
            select(Product).where(
                Product.is_active.is_(True),
                or_(
                    Product.name.ilike(f"%{q}%"),
                    Product.internal_sku.ilike(f"%{q}%"),
                    Product.factory_code.ilike(f"%{q}%"),
                    Product.model.ilike(f"%{q}%"),
                    Product.brand_id.in_(brand_ids or [-1]),
                    Product.id.in_(supplier_product_ids or [-1]),
                )
            ).limit(30)
        )).scalars().all()
    if not rows:
        await message.answer("محصولی پیدا نشد.")
    else:
        for p in rows:
            await compact_product(message, p)
    await state.clear()

@router.callback_query(F.data.startswith("addcart:"))
async def ask_qty(cb: CallbackQuery, state: FSMContext):
    await state.update_data(product_id=int(cb.data.split(":")[1]))
    await state.set_state(CartQuantity.qty)
    await cb.message.answer("تعداد موردنظر را فقط به عدد ارسال کنید.")
    await cb.answer()

@router.message(CartQuantity.qty)
async def set_qty(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return await message.answer("تعداد معتبر نیست.")
    qty = int(raw)
    data = await state.get_data()
    async with SessionLocal() as session:
        user = await db_user(session, message.from_user.id)
        product = await session.get(Product, data["product_id"])
        cart = await get_or_create_cart(session, user.id)
        item = await session.scalar(select(CartItem).where(CartItem.cart_id == cart.id, CartItem.product_id == product.id))
        if item:
            item.quantity = qty
        else:
            session.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=qty))
        unit = await get_price_for_qty(session, product, qty)
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ به سبد اضافه شد.\n{product.name}\nتعداد: {fa_num(qty)}\n"
        f"قیمت واحد: {fa_num(unit)} تومان\nجمع: {fa_num(unit * qty)} تومان"
    )

async def render_cart(target, tg_id):
    async with SessionLocal() as session:
        user = await db_user(session, tg_id)
        cart = await get_or_create_cart(session, user.id)
        items = (await session.execute(select(CartItem).where(CartItem.cart_id == cart.id))).scalars().all()
        if not items:
            return await target.answer("سبد خرید خالی است.")
        total = 0
        for item in items:
            p = await session.get(Product, item.product_id)
            unit = await get_price_for_qty(session, p, item.quantity)
            line = unit * item.quantity
            total += line
            await target.answer(
                f"{p.name}\nتعداد: {fa_num(item.quantity)}\nقیمت واحد: {fa_num(unit)} تومان\nجمع: {fa_num(line)} تومان",
                reply_markup=cart_actions(item.id),
            )
        await target.answer(f"💰 جمع کل: {fa_num(total)} تومان", reply_markup=checkout_button())

@router.message(Command("cart"))
async def cart_cmd(message: Message):
    await render_cart(message, message.from_user.id)

@router.callback_query(F.data == "menu:cart")
async def cart_inline(cb: CallbackQuery):
    await render_cart(cb.message, cb.from_user.id)
    await cb.answer()

@router.callback_query(F.data.startswith(("cartinc:", "cartdec:", "cartdel:")))
async def cart_change(cb: CallbackQuery):
    action, raw_id = cb.data.split(":")
    item_id = int(raw_id)
    async with SessionLocal() as session:
        item = await session.get(CartItem, item_id)
        if not item:
            return await cb.answer("آیتم پیدا نشد.", show_alert=True)
        if action == "cartdel" or (action == "cartdec" and item.quantity <= 1):
            await session.delete(item)
        elif action == "cartinc":
            item.quantity += 1
        else:
            item.quantity -= 1
        await session.commit()
    await cb.answer("سبد به‌روزرسانی شد.")
    await cb.message.answer("سبد خرید را دوباره باز کنید.")

@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery):
    async with SessionLocal() as session:
        user = await db_user(session, cb.from_user.id)
        order = await create_order_from_cart(session, user.id)
        if not order:
            return await cb.answer("سبد خالی است.", show_alert=True)
        account = await session.scalar(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.id))
    text = f"✅ سفارش {order.order_no} ثبت شد.\nمبلغ دقیق: {fa_num(order.total_amount)} تومان\n\n"
    if account:
        if account.card_number:
            text += f"💳 شماره کارت:\n{account.card_number}\n"
        if account.holder_name:
            text += f"به نام: {account.holder_name}\n"
        if account.iban:
            text += f"شبا: {account.iban}\n"
    text += "\nپس از واریز، تصویر فیش را همین‌جا ارسال کنید."
    await cb.message.answer(text)
    await cb.answer()

@router.message(Command("orders"))
async def orders_cmd(message: Message):
    await _orders(message, message.from_user.id)

@router.callback_query(F.data == "menu:orders")
async def orders_inline(cb: CallbackQuery):
    await _orders(cb.message, cb.from_user.id)
    await cb.answer()

async def _orders(target, tg_id):
    async with SessionLocal() as session:
        user = await db_user(session, tg_id)
        rows = (await session.execute(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(20))).scalars().all()
    labels = {
        "WAITING_PAYMENT":"منتظر پرداخت","PAYMENT_REVIEW":"منتظر تأیید",
        "CONFIRMED":"تأیید شده","PREPARING":"در حال آماده‌سازی","READY":"آماده",
        "SHIPPED":"ارسال شده","DELIVERED":"تکمیل شده","CANCELLED":"لغو شده"
    }
    await target.answer("\n".join(f"{o.order_no} | {labels.get(o.status,o.status)} | {fa_num(o.total_amount)} تومان" for o in rows) if rows else "سفارشی ندارید.")

@router.message(Command("price"))
async def price_cmd(message: Message):
    await message.answer("فرمت لیست قیمت را انتخاب کنید:", reply_markup=price_list_formats())

@router.callback_query(F.data == "menu:pricelist")
async def price_inline(cb: CallbackQuery):
    await cb.message.answer("فرمت لیست قیمت را انتخاب کنید:", reply_markup=price_list_formats())
    await cb.answer()

@router.callback_query(F.data == "pricefmt:xlsx")
async def price_xlsx(cb: CallbackQuery):
    async with SessionLocal() as session:
        data = await build_price_list(session)
    await cb.message.answer_document(BufferedInputFile(data.read(), filename="price-list.xlsx"))
    await cb.answer()

@router.callback_query(F.data == "pricefmt:pdf")
async def price_pdf(cb: CallbackQuery):
    async with SessionLocal() as session:
        data = await build_price_list_pdf(session)
    await cb.message.answer_document(BufferedInputFile(data.read(), filename="price-list.pdf"))
    await cb.answer()

@router.callback_query(F.data == "menu:changes")
async def changes(cb: CallbackQuery):
    async with SessionLocal() as session:
        rows = (await session.execute(select(PriceHistory).where(PriceHistory.price_type == "retail").order_by(PriceHistory.id.desc()).limit(40))).scalars().all()
        ups, downs = [], []
        for r in rows:
            p = await session.get(Product, r.product_id)
            line = f"{p.name}: {fa_num(r.old_price or 0)} ← {fa_num(r.new_price)} تومان"
            if (r.new_price or 0) >= (r.old_price or 0):
                ups.append(line)
            else:
                downs.append(line)
    text = ""
    if ups:
        text += "🔺 افزایش قیمت\n" + "\n".join(ups[:15])
    if downs:
        text += ("\n\n" if text else "") + "🔻 کاهش قیمت\n" + "\n".join(downs[:15])
    await cb.message.answer(text or "هنوز تغییر قیمتی ثبت نشده.")
    await cb.answer()
