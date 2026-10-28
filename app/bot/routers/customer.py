from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
)
from html import escape
import secrets

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, or_, func

from app.db.database import SessionLocal
from app.models import (
    User, Product, Brand, Category, SupplierProduct, Cart, CartItem, Order,
    PriceHistory, BankAccount, ProductChannelPost, PriceTier,
)
from app.bot.keyboards import product_list_actions, cart_actions, checkout_button, price_list_formats
from app.services.orders import CheckoutError, get_or_create_cart, create_order_from_cart
from app.services.catalog import get_price_for_user, category_tree, WHOLESALE, customer_type_of
from app.services.auth import account_state, is_admin_account
from app.services.export import build_price_list, build_price_list_pdf
from app.utils import fa_num, normalize_digits, stock_label, order_status_label

router = Router()

MAX_LIST = 6


class CartQuantity(StatesGroup):
    qty = State()


class SearchState(StatesGroup):
    query = State()


async def db_user(session, tg_id):
    return await session.scalar(select(User).where(User.telegram_id == tg_id))


async def allowed_user(target, tg_id, session):
    """مشتری فعال و مجاز؛ در غیر این صورت پیام مناسب و None برمی‌گرداند."""
    user = await db_user(session, tg_id)
    status = account_state(user, await is_admin_account(session, user))
    if status == "blocked":
        await target.answer("⛔ حساب شما فعال نیست. با /start شروع کنید.")
        return None
    if status == "pending":
        await target.answer("⏳ حساب عمده شما در انتظار تأیید مدیریت است.")
        return None
    if status == "incomplete":
        await target.answer("برای تکمیل ثبت‌نام «/start» را بزنید.")
        return None
    return user


def product_actions_keyboard(product_id: int, post_url: str | None = None):
    return product_list_actions(product_id, post_url)


async def compact_product(target, p, user=None):
    async with SessionLocal() as session:
        brand = await session.get(Brand, p.brand_id) if p.brand_id else None
        category = await session.get(Category, p.category_id) if p.category_id else None
        post = await session.scalar(
            select(ProductChannelPost).where(ProductChannelPost.product_id == p.id).order_by(ProductChannelPost.id.desc())
        )
        tier_count = await session.scalar(
            select(PriceTier.id).where(PriceTier.product_id == p.id).limit(1)
        )
        wholesale = customer_type_of(user) == WHOLESALE
        unit_price = await get_price_for_user(session, p, 1, user)

    lines = [f"📦 {p.name}", f"کد: {p.internal_sku}"]
    if p.factory_code:
        lines.append(f"کد کارخانه: {p.factory_code}")
    if p.model:
        lines.append(f"مدل: {p.model}")
    if brand:
        lines.append(f"برند: {brand.name}")
    if category:
        lines.append(f"دسته: {category.name}")

    if wholesale:
        lines.append(f"قیمت عمده: {fa_num(p.wholesale_price)} تومان")
        if unit_price != (p.wholesale_price or 0):
            lines.append(f"قیمت شما با پلکان: {fa_num(unit_price)} تومان")
        lines.append(f"قیمت خرده: {fa_num(p.base_price)} تومان")
        if tier_count:
            lines.append("🎁 این محصول قیمت پلکانی دارد (برای تعداد بیشتر، قیمت بهتر).")
    else:
        lines.append(f"قیمت: {fa_num(p.base_price)} تومان")

    lines.append(f"وضعیت: {stock_label(p.stock_status)}")
    await target.answer(
        "\n".join(lines),
        reply_markup=product_actions_keyboard(p.id, post.post_url if post else None),
    )


def catalog_statement(scope="all", ref=0, query=""):
    stmt = select(Product).where(Product.is_active.is_(True))
    if scope == "brand":
        return stmt.where(Product.brand_id == ref)
    if scope == "category":
        return stmt.where(Product.category_id == ref)
    if scope == "search":
        pattern = f"%{query}%"
        return stmt.where(or_(
            Product.name.ilike(pattern), Product.internal_sku.ilike(pattern),
            Product.factory_code.ilike(pattern), Product.model.ilike(pattern),
            Product.brand_id.in_(select(Brand.id).where(Brand.name.ilike(pattern))),
            Product.id.in_(select(SupplierProduct.product_id).where(SupplierProduct.supplier_sku.ilike(pattern))),
        ))
    return stmt


async def show_products(target, stmt=None, tg_id=None, *, page=0, scope="all", ref=0, edit=False):
    async with SessionLocal() as session:
        user = await allowed_user(target, tg_id, session)
        if not user:
            return
        if stmt is None:
            stmt = catalog_statement(scope, ref)
        count = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        pages = max(1, (count + MAX_LIST - 1) // MAX_LIST)
        page = max(0, min(page, pages - 1))
        rows = (await session.execute(stmt.order_by(None).order_by(Product.name, Product.id)
                .offset(page * MAX_LIST).limit(MAX_LIST))).scalars().all()
        prices = {p.id: await get_price_for_user(session, p, 1, user) for p in rows}
    me = await target.bot.get_me()
    lines = [f"📦 فهرست محصولات | صفحه {fa_num(page + 1)} از {fa_num(pages)}",
             "مرتب‌شده بر اساس نام محصول"]
    for index, p in enumerate(rows, start=page * MAX_LIST + 1):
        url = f"https://t.me/{me.username}?start=product_{p.id}"
        lines.extend(["", f"<b>{fa_num(index)}. {escape(p.name[:160])}</b>",
                      f"کد: {escape(p.internal_sku)} | قیمت: {fa_num(prices[p.id])} تومان",
                      "برای سفارش محصول روی لینک زیر کلیک کنید:",
                      f'<a href="{url}">مشاهده و سفارش محصول</a>'])
    if not rows:
        lines.append("محصولی پیدا نشد.")
    buttons = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"catalog:{scope}:{ref}:{page-1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"catalog:{scope}:{ref}:{page+1}"))
    if navigation:
        buttons.append(navigation)
    if scope == "category":
        buttons.append([InlineKeyboardButton(text="🗂 بازگشت به دسته", callback_data=f"browsecat:{ref}")])
    elif scope == "brand":
        buttons.append([InlineKeyboardButton(text="🏷 بازگشت به برندها", callback_data="menu:brands")])
    buttons.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="admin:back")])
    kwargs = dict(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML", disable_web_page_preview=True)
    if edit:
        try:
            await target.edit_text("\n".join(lines), **kwargs)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    else:
        await target.answer("\n".join(lines), **kwargs)


@router.callback_query(F.data.startswith("catalog:"))
async def catalog_page(cb: CallbackQuery, state: FSMContext):
    try:
        _, scope, ref, page = cb.data.split(":")
        ref, page = int(ref), int(page)
        if scope not in {"all", "brand", "category", "search"} or page < 0:
            raise ValueError
    except ValueError:
        return await cb.answer("صفحه نامعتبر است.", show_alert=True)
    query = (await state.get_data()).get("catalog_searches", {}).get(str(ref), "")
    if scope == "search" and not query:
        return await cb.answer("جستجوی قبلی منقضی شده؛ دوباره جستجو کنید.", show_alert=True)
    await show_products(cb.message, catalog_statement(scope, ref, query), cb.from_user.id,
                        page=page, scope=scope, ref=ref, edit=True)
    await cb.answer()


@router.message(Command("products"))
async def products_cmd(message: Message):
    async with SessionLocal() as session:
        if not await allowed_user(message, message.from_user.id, session):
            return
    await show_products(message, tg_id=message.from_user.id)


@router.callback_query(F.data == "menu:products")
async def products_inline(cb: CallbackQuery):
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
    await show_products(cb.message, tg_id=cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("showprod:"))
async def show_product(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        p = await session.get(Product, pid)
    if not p or not p.is_active:
        return await cb.answer("محصول پیدا نشد.", show_alert=True)
    await compact_product(cb.message, p, user)
    await cb.answer()


@router.callback_query(F.data == "menu:brands")
async def brands(cb: CallbackQuery):
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
        rows = (await session.execute(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name)
        )).scalars().all()
        counts = {}
        for brand in rows:
            counts[brand.id] = await session.scalar(
                select(Product.id).where(
                    Product.brand_id == brand.id, Product.is_active.is_(True)
                ).limit(1)
            )
    kb = [[InlineKeyboardButton(text=b.name, callback_data=f"browsebrand:{b.id}")]
          for b in rows if counts.get(b.id)]
    await cb.message.answer(
        "🏷 برندها:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None,
    )
    if not kb:
        await cb.message.answer("هنوز برندی با محصول فعال ثبت نشده است.")
    await cb.answer()


@router.callback_query(F.data.startswith("browsebrand:"))
async def browse_brand(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
    await show_products(
        cb.message,
        select(Product).where(Product.is_active.is_(True), Product.brand_id == bid),
        tg_id=cb.from_user.id, scope="brand", ref=bid,
    )
    await cb.answer()


@router.callback_query(F.data == "menu:categories")
async def categories(cb: CallbackQuery):
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
        tree = await category_tree(session, only_active=True)
        roots = [(c, d) for c, d in tree if d == 0]
        has_any = False
        rows = []
        for cat, _depth in roots:
            product_exists = await session.scalar(
                select(Product.id).where(
                    Product.category_id == cat.id, Product.is_active.is_(True)
                ).limit(1)
            )
            child_exists = await session.scalar(
                select(Category.id).where(
                    Category.parent_id == cat.id, Category.is_active.is_(True)
                ).limit(1)
            )
            if product_exists or child_exists:
                has_any = True
                rows.append([InlineKeyboardButton(text=f"🗂 {cat.name}", callback_data=f"browsecat:{cat.id}")])
    await cb.message.answer(
        "🗂 دسته‌بندی‌ها:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if has_any else None,
    )
    if not has_any:
        await cb.message.answer("هنوز دسته‌بندی فعالی با محصول ثبت نشده است.")
    await cb.answer()


@router.callback_query(F.data.startswith("browsecat:"))
async def browse_cat(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
        parent_category = await session.get(Category, cid)
        children = (await session.execute(
            select(Category).where(
                Category.parent_id == cid, Category.is_active.is_(True)
            ).order_by(Category.name)
        )).scalars().all()
        products = (await session.execute(
            select(Product).where(
                Product.category_id == cid, Product.is_active.is_(True)
            ).order_by(Product.name).limit(20)
        )).scalars().all()
        breadcrumb = []
        node = parent_category
        guard = 0
        while node and guard < 10:
            breadcrumb.append(node.name)
            node = await session.get(Category, node.parent_id) if node.parent_id else None
            guard += 1

    rows = []
    for child in children:
        rows.append([InlineKeyboardButton(text=f"🗂 {child.name}", callback_data=f"browsecat:{child.id}")])
    if products:
        rows.append([InlineKeyboardButton(text="📦 نمایش محصولات این دسته", callback_data=f"catalog:category:{cid}:0")])
    if parent_category and parent_category.parent_id:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"browsecat:{parent_category.parent_id}")])

    title = " › ".join(reversed(breadcrumb)) if breadcrumb else "دسته‌بندی"
    if not rows:
        await cb.message.answer(f"{title}\n\nدر این دسته محصول یا زیرمجموعه فعالی نیست.")
    else:
        await cb.message.answer(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data == "menu:support")
async def support(cb: CallbackQuery):
    async with SessionLocal() as session:
        from app.models import SystemSetting

        row = await session.get(SystemSetting, "support_text")
    await cb.message.answer(row.value if row and row.value else "برای پشتیبانی با مدیریت شرکت تماس بگیرید.")
    await cb.answer()


@router.callback_query(F.data == "menu:search")
async def search_inline(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await cb.message.answer("نام، کد داخلی، کد کارخانه، برند یا مدل را ارسال کنید.")
    await cb.answer()


@router.message(SearchState.query)
async def search_result(message: Message, state: FSMContext):
    q = normalize_digits((message.text or "").strip())
    if not q:
        return await message.answer("عبارت جستجو خالی است.")
    await state.set_state(None)
    searches = (await state.get_data()).get("catalog_searches", {})
    ref = secrets.randbits(32)
    searches[str(ref)] = q
    searches = dict(list(searches.items())[-5:])
    await state.update_data(catalog_searches=searches)
    await show_products(message, catalog_statement("search", query=q), message.from_user.id, scope="search", ref=ref)


@router.callback_query(F.data.startswith("addcart:"))
async def ask_qty(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        product = await session.get(Product, pid)
        if not product or not product.is_active or product.stock_status in {"unavailable", "out_of_stock"} or product.stock_quantity == 0:
            return await cb.answer("این محصول در دسترس نیست.", show_alert=True)
        cart = await get_or_create_cart(session, user.id)
        await session.execute(select(Cart).where(Cart.id == cart.id).with_for_update())
        item = await session.scalar(select(CartItem).where(CartItem.cart_id == cart.id, CartItem.product_id == pid))
        if not item:
            item = CartItem(cart_id=cart.id, product_id=pid, quantity=1)
            session.add(item)
        await session.flush()
        text, markup = await cart_item_card(session, user, product, item)
        await session.commit()
    await state.set_state(None)
    await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


async def cart_item_card(session, user, product, item):
    unit = await get_price_for_user(session, product, item.quantity, user)
    total = 0
    for row in (await session.execute(select(CartItem).where(CartItem.cart_id == item.cart_id))).scalars():
        p = await session.get(Product, row.product_id)
        if p:
            total += (await get_price_for_user(session, p, row.quantity, user)) * row.quantity
    text = (f"🛒 {product.name}\nتعداد: {fa_num(item.quantity)}\n"
            f"قیمت واحد برای این تعداد: {fa_num(unit)} تومان\n"
            f"جمع این محصول: {fa_num(unit * item.quantity)} تومان\n"
            f"جمع کل سبد: {fa_num(total)} تومان\n\n"
            "تعداد را با ➕ و ➖ تغییر دهید؛ قیمت پلکانی خودکار محاسبه می‌شود.")
    markup = cart_actions(item.id)
    markup.inline_keyboard.extend([
        [InlineKeyboardButton(text="🧺 مشاهده سبد خرید", callback_data="menu:cart")],
        [InlineKeyboardButton(text="✅ ثبت نهایی سفارش", callback_data="checkout")],
    ])
    return text, markup


@router.message(CartQuantity.qty)
async def set_qty(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return await message.answer("تعداد معتبر نیست.")
    qty = int(raw)
    data = await state.get_data()
    async with SessionLocal() as session:
        user = await allowed_user(message, message.from_user.id, session)
        if not user:
            await state.clear()
            return
        product = await session.get(Product, data.get("product_id"))
        if not product or not product.is_active or product.stock_status in {"unavailable", "out_of_stock"}:
            await state.clear()
            return await message.answer("این محصول موجود نیست.")
        if product.stock_quantity is not None and qty > product.stock_quantity:
            return await message.answer("تعداد از موجودی محصول بیشتر است.")
        cart = await get_or_create_cart(session, user.id)
        item = await session.scalar(select(CartItem).where(
            CartItem.cart_id == cart.id, CartItem.product_id == product.id
        ))
        if item:
            item.quantity = qty
        else:
            session.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=qty))
        await session.flush()
        item = await session.scalar(select(CartItem).where(CartItem.cart_id == cart.id, CartItem.product_id == product.id))
        text, markup = await cart_item_card(session, user, product, item)
        await session.commit()
    await state.clear()
    await message.answer(text, reply_markup=markup)


async def render_cart(target, tg_id):
    async with SessionLocal() as session:
        user = await allowed_user(target, tg_id, session)
        if not user:
            return
        cart = await get_or_create_cart(session, user.id)
        items = (await session.execute(select(CartItem).where(CartItem.cart_id == cart.id))).scalars().all()
        if not items:
            return await target.answer("سبد خرید خالی است.")
        total = 0
        for item in items:
            p = await session.get(Product, item.product_id)
            if not p:
                continue
            unit = await get_price_for_user(session, p, item.quantity, user)
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
    async with SessionLocal() as session:
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        item = await session.scalar(select(CartItem).where(CartItem.id == int(raw_id)).with_for_update())
        if not item:
            return await cb.answer("این آیتم دیگر در سبد نیست.", show_alert=True)
        cart = await session.get(Cart, item.cart_id)
        if not cart or cart.user_id != user.id:
            return await cb.answer("این آیتم متعلق به سبد شما نیست.", show_alert=True)
        if action == "cartdel":
            await session.delete(item)
            await session.commit()
            text = "🗑 محصول از سبد حذف شد."
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧺 مشاهده سبد", callback_data="menu:cart")]])
        else:
            product = await session.get(Product, item.product_id)
            if not product or not product.is_active or product.stock_status in {"unavailable", "out_of_stock"}:
                return await cb.answer("محصول در دسترس نیست؛ آن را از سبد حذف کنید.", show_alert=True)
            qty = item.quantity + (1 if action == "cartinc" else -1)
            if qty < 1:
                return await cb.answer("حداقل تعداد یک است؛ برای حذف، 🗑 را بزنید.")
            if product.stock_quantity is not None and qty > product.stock_quantity:
                return await cb.answer("تعداد از موجودی محصول بیشتر است.", show_alert=True)
            item.quantity = qty
            await session.flush()
            text, markup = await cart_item_card(session, user, product, item)
            await session.commit()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await cb.answer()


@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery):
    async with SessionLocal() as session:
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        try:
            order = await create_order_from_cart(session, user.id)
        except CheckoutError as exc:
            await session.rollback()
            return await cb.answer(str(exc), show_alert=True)
        if not order:
            return await cb.answer("سبد خالی است.", show_alert=True)
        account = await session.scalar(select(BankAccount).where(
            BankAccount.is_active.is_(True)).order_by(BankAccount.id))
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
        user = await allowed_user(target, tg_id, session)
        if not user:
            return
        rows = (await session.execute(
            select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(20)
        )).scalars().all()
    if not rows:
        return await target.answer("سفارشی ندارید.")
    await target.answer("\n".join(
        f"{o.order_no} | {order_status_label(o.status)} | {fa_num(o.total_amount)} تومان" for o in rows
    ))


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
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        data = await build_price_list(session, include_wholesale=customer_type_of(user) == WHOLESALE)
    await cb.message.answer_document(BufferedInputFile(data.read(), filename="price-list.xlsx"))
    await cb.answer()


@router.callback_query(F.data == "pricefmt:pdf")
async def price_pdf(cb: CallbackQuery):
    async with SessionLocal() as session:
        user = await allowed_user(cb.message, cb.from_user.id, session)
        if not user:
            return await cb.answer()
        data = await build_price_list_pdf(session, include_wholesale=customer_type_of(user) == WHOLESALE)
    await cb.message.answer_document(BufferedInputFile(data.read(), filename="price-list.pdf"))
    await cb.answer()


@router.callback_query(F.data == "menu:changes")
async def changes(cb: CallbackQuery):
    async with SessionLocal() as session:
        if not await allowed_user(cb.message, cb.from_user.id, session):
            return await cb.answer()
        rows = (await session.execute(
            select(PriceHistory).where(PriceHistory.price_type == "retail")
            .order_by(PriceHistory.id.desc()).limit(40)
        )).scalars().all()
        ups, downs = [], []
        for r in rows:
            p = await session.get(Product, r.product_id)
            if not p:
                continue
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
