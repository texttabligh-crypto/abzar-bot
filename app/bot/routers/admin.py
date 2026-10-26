from decimal import Decimal
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
from sqlalchemy import select, func, text

from app.db.database import SessionLocal
from app.models import (
    User, Product, ProductImage, Brand, Category, Supplier, PriceTier, PriceHistory, Order, OrderItem, Payment,
    BankAccount, Permission, UserPermission, AuditLog, SystemSetting, AdminInvite, PublishChannel, ProductChannelPost,
)
from app.bot.keyboards import (
    yes_no, brand_picker, category_picker, finish_images,
    channel_publish_menu, order_status_menu,
)
from app.services.auth import has_permission, guard
from app.services.catalog import (
    next_sku, set_price, set_product_price, PRICE_LABELS, PRICE_ATTRS,
    category_tree, category_label, category_path, descendant_ids, WHOLESALE,
)
from app.services.audit import add_audit
from app.utils import normalize_digits, fa_num, order_status_label, parse_int
from app.version import short_version
from app.services.publishing import (
    publish_product, publish_many, sync_product_post,
    connect_channel, auto_publish_if_enabled, setting_value, set_setting_value,
    FOOTER_KEY, AUTO_PUBLISH_KEY,
)

router = Router()


class AddProduct(StatesGroup):
    name = State()
    brand_choice = State()
    category_choice = State()
    new_category = State()
    images = State()
    purchase_price = State()
    retail_price = State()
    wholesale_price = State()
    tier_min = State()
    tier_max = State()
    tier_price = State()


class EditProduct(StatesGroup):
    query = State()
    value = State()
    photo = State()


class EditPrices(StatesGroup):
    """ویرایش پشت‌سرهم همه قیمت‌ها + پلکان."""
    purchase = State()
    retail = State()
    wholesale = State()
    tier_menu = State()
    tier_min = State()
    tier_max = State()
    tier_price = State()


class CategoryFlow(StatesGroup):
    name = State()
    rename = State()
    product_name = State()
    product_price = State()
    product_retail = State()
    product_wholesale = State()


class BrandFlow(StatesGroup):
    name = State()
    rename = State()


class ChannelFlow(StatesGroup):
    chat_ref = State()
    footer = State()


class AddEntity(StatesGroup):
    value = State()
    phone = State()


class AddAdmin(StatesGroup):
    telegram_id = State()


class AdminInviteFlow(StatesGroup):
    selecting = State()


class AddBank(StatesGroup):
    title = State()
    card = State()
    holder = State()
    iban = State()

class TierFlow(StatesGroup):
    product = State()
    min_qty = State()
    max_qty = State()
    price = State()

class DeleteProductFlow(StatesGroup):
    query = State()

class PriceChangeFlow(StatesGroup):
    search = State()
    value = State()

class ResetProductsFlow(StatesGroup):
    confirm = State()

class SystemSettingsFlow(StatesGroup):
    support = State()

class CategoryParentFlow(StatesGroup):
    name = State()

class BankEditFlow(StatesGroup):
    value = State()


class ChannelSetup(StatesGroup):
    title = State()
    chat_ref = State()
    username = State()


class PublishFlow(StatesGroup):
    single_query = State()
    price_min = State()
    price_max = State()


async def require(cb, perm):
    if not await has_permission(cb.from_user.id, perm):
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return False
    return True


def parse_money(text: str | None) -> Decimal:
    raw = normalize_digits(text or "").replace(",", "").replace("٬", "").strip()
    value = Decimal(raw)
    if value < 0:
        raise ValueError
    return value

def format_tiers(tiers) -> str:
    if not tiers:
        return "بدون قیمت پلکانی"
    lines = []
    for t in tiers:
        hi = "بالاتر" if t.get("max_qty") is None else fa_num(t["max_qty"])
        lines.append(f"• از {fa_num(t['min_qty'])} تا {hi}: {fa_num(t['unit_price'])} تومان")
    return "\n".join(lines)

def tier_overlaps(tiers, min_qty: int, max_qty: int | None) -> bool:
    new_hi = max_qty if max_qty is not None else 10**18
    for t in tiers:
        lo = int(t["min_qty"])
        hi = int(t["max_qty"]) if t.get("max_qty") is not None else 10**18
        if min_qty <= hi and lo <= new_hi:
            return True
    return False

async def ask_product_category(message_or_cb, state: FSMContext, edit: bool = False):
    async with SessionLocal() as session:
        tree = await category_tree(session, only_active=True)
    if not tree:
        await state.update_data(category_id=None)
        await state.set_state(AddProduct.images)
        target = message_or_cb.message if isinstance(message_or_cb, CallbackQuery) else message_or_cb
        return await target.answer(
            "هیچ دسته‌بندی فعالی ثبت نشده؛ بدون دسته‌بندی ادامه می‌دهیم.\n\n"
            "عکس محصول را ارسال کنید. می‌توانید یک یا چند عکس بفرستید.\n"
            "وقتی تمام شد روی «پایان ارسال عکس‌ها» بزنید.",
            reply_markup=finish_images(),
        )
    await state.set_state(AddProduct.category_choice)
    target = message_or_cb.message if isinstance(message_or_cb, CallbackQuery) else message_or_cb
    await target.answer(
        "دسته‌بندی محصول را انتخاب کنید:\n"
        "زیرمجموعه‌ها با علامت └ نمایش داده می‌شوند.",
        reply_markup=category_picker(tree),
    )


async def finalize_new_product(target, tg_id: int, state: FSMContext):
    data = await state.get_data()
    image_ids = list(data.get("image_ids") or [])
    tiers = list(data.get("tiers") or [])
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == tg_id))
        sku = await next_sku(session)
        product = Product(
            internal_sku=sku,
            name=data["name"],
            brand_id=data.get("brand_id"),
            category_id=data.get("category_id"),
            purchase_price=data["purchase_price"],
            base_price=data["retail_price"],
            wholesale_price=data["wholesale_price"],
            telegram_file_id=image_ids[0] if image_ids else None,
        )
        session.add(product)
        await session.flush()
        for idx, file_id in enumerate(image_ids):
            session.add(ProductImage(product_id=product.id, telegram_file_id=file_id, sort_order=idx))
        for tier in tiers:
            session.add(PriceTier(
                product_id=product.id,
                min_qty=tier["min_qty"],
                max_qty=tier.get("max_qty"),
                unit_price=tier["unit_price"],
            ))
        await session.flush()
        product_id = product.id
        await add_audit(
            session, actor.id if actor else None, "product.create", "product", product.id,
            {
                "sku": sku, "name": product.name, "brand_id": product.brand_id,
                "category_id": product.category_id,
                "image_count": len(image_ids),
                "purchase_price_toman": str(product.purchase_price),
                "retail_price_toman": str(product.base_price),
                "wholesale_price_toman": str(product.wholesale_price),
                "tier_count": len(tiers),
            }
        )
        await session.commit()
    await state.clear()
    await target.answer(
        "✅ محصول کامل ثبت شد.\n\n"
        f"کد: {sku}\n"
        f"قیمت پایه: {fa_num(data['purchase_price'])} تومان\n"
        f"فروش خرده: {fa_num(data['retail_price'])} تومان\n"
        f"فروش عمده: {fa_num(data['wholesale_price'])} تومان\n"
        f"تعداد پلکان: {fa_num(len(tiers))}\n"
        f"تعداد عکس: {fa_num(len(image_ids))}"
    )
    published, info = await auto_publish_if_enabled(target.bot, product_id)
    if published:
        await target.answer(f"📣 محصول در کانال هم منتشر شد.\n{info}".strip())
    elif info:
        await target.answer(f"⚠️ ارسال خودکار به کانال انجام نشد:\n{info}")

# ---------------- PRODUCT CREATE ----------------

@router.callback_query(F.data == "admin:add_product")
@guard("products.create")
async def add_product(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.create"):
        return
    await state.clear()
    await state.set_state(AddProduct.name)
    await cb.message.answer("نام محصول را ارسال کنید.")
    await cb.answer()


@router.message(AddProduct.name)
async def add_product_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام محصول نمی‌تواند خالی باشد.")
    await state.update_data(name=name, image_ids=[], tiers=[])
    async with SessionLocal() as session:
        brands = (await session.execute(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name)
        )).scalars().all()
    if brands:
        await state.set_state(AddProduct.brand_choice)
        await message.answer("آیا این محصول برند دارد؟", reply_markup=yes_no("addp:brand"))
    else:
        await state.update_data(brand_id=None)
        await ask_product_category(message, state)


@router.callback_query(F.data == "addp:brand:yes")
@guard("products.create")
async def add_product_brand_yes(cb: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        brands = (await session.execute(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name)
        )).scalars().all()
    if not brands:
        await state.update_data(brand_id=None)
        await cb.message.answer("برندی ثبت نشده؛ بدون برند ادامه می‌دهیم.")
        await ask_product_category(cb, state)
    else:
        await cb.message.answer("برند محصول را انتخاب کنید:", reply_markup=brand_picker(brands))
    await cb.answer()


@router.callback_query(F.data == "addp:brand:no")
@guard("products.create")
async def add_product_brand_no(cb: CallbackQuery, state: FSMContext):
    await state.update_data(brand_id=None)
    await ask_product_category(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("addpbrand:"))
@guard("products.create")
async def add_product_brand_pick(cb: CallbackQuery, state: FSMContext):
    brand_id = int(cb.data.split(":")[1])
    await state.update_data(brand_id=brand_id or None)
    await ask_product_category(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("addpcat:"))
@guard("products.create")
async def add_product_category_pick(cb: CallbackQuery, state: FSMContext):
    category_id = int(cb.data.split(":")[1])
    await state.update_data(category_id=category_id or None)
    await state.set_state(AddProduct.images)
    await cb.message.answer(
        "عکس محصول را ارسال کنید. می‌توانید یک یا چند عکس بفرستید.\n"
        "وقتی تمام شد روی «پایان ارسال عکس‌ها» بزنید.",
        reply_markup=finish_images(),
    )
    await cb.answer()


@router.callback_query(F.data == "addp:category:add")
@guard("products.create")
async def add_product_new_category(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.new_category)
    await cb.message.answer(
        "نام دسته‌بندی جدید را ارسال کنید.\n"
        "اگر می‌خواهید زیرمجموعه یک دسته باشد، از «🗂 دسته‌بندی‌ها» در پنل مدیریت "
        "و گزینه «افزودن زیرمجموعه» استفاده کنید."
    )
    await cb.answer()


@router.message(AddProduct.new_category)
async def add_product_new_category_save(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام دسته‌بندی خالی است.")
    async with SessionLocal() as session:
        existing = await session.scalar(select(Category).where(Category.name == name))
        if existing:
            category = existing
            if not category.is_active:
                category.is_active = True
        else:
            category = Category(name=name, is_active=True)
            session.add(category)
        await session.commit()
        await session.refresh(category)
        category_id = category.id
    await state.update_data(category_id=category_id)
    await state.set_state(AddProduct.images)
    await message.answer(
        f"✅ دسته‌بندی «{name}» ثبت شد.\n\n"
        "عکس محصول را ارسال کنید. می‌توانید یک یا چند عکس بفرستید.\n"
        "وقتی تمام شد روی «پایان ارسال عکس‌ها» بزنید.",
        reply_markup=finish_images(),
    )


@router.message(AddProduct.images, F.photo)
async def add_product_collect_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    image_ids = list(data.get("image_ids") or [])
    image_ids.append(message.photo[-1].file_id)
    image_ids = list(dict.fromkeys(image_ids))
    await state.update_data(image_ids=image_ids)
    await message.answer(
        f"✅ عکس دریافت شد. تعداد عکس‌های ثبت‌شده: {fa_num(len(image_ids))}",
        reply_markup=finish_images(),
    )


@router.callback_query(F.data.in_({"addp:images_done", "addp:no_images"}))
@guard("products.create")
async def add_product_images_done(cb: CallbackQuery, state: FSMContext):
    if cb.data == "addp:no_images":
        await state.update_data(image_ids=[])
    await state.set_state(AddProduct.purchase_price)
    await cb.message.answer(
        "💵 قیمت پایه محصول را به تومان وارد کنید.\n"
        "منظور قیمت مبنا/خرید داخلی شماست.\nمثال: ۱۲۰۰۰۰"
    )
    await cb.answer()


@router.message(AddProduct.purchase_price)
async def add_product_purchase_price(message: Message, state: FSMContext):
    try:
        value = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت پایه نامعتبر است. فقط مبلغ را به تومان وارد کنید.")
    await state.update_data(purchase_price=value)
    await state.set_state(AddProduct.retail_price)
    await message.answer("🛒 قیمت فروش خرده را به تومان وارد کنید.\nمثال: ۱۵۹۰۰۰")


@router.message(AddProduct.retail_price)
async def add_product_retail_price(message: Message, state: FSMContext):
    try:
        value = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت فروش خرده نامعتبر است. فقط مبلغ را به تومان وارد کنید.")
    await state.update_data(retail_price=value)
    await state.set_state(AddProduct.wholesale_price)
    await message.answer("📦 قیمت فروش عمده را به تومان وارد کنید.\nمثال: ۱۴۵۰۰۰")


@router.message(AddProduct.wholesale_price)
async def add_product_wholesale_price(message: Message, state: FSMContext):
    try:
        value = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت فروش عمده نامعتبر است. فقط مبلغ را به تومان وارد کنید.")
    await state.update_data(wholesale_price=value)
    await message.answer(
        "آیا این محصول شرط قیمت پلکانی بر اساس تعداد دارد؟\n\n"
        "مثال: تا ۹۹ عدد قیمت خرده؛ از ۱۰۰ تا ۱۹۹ یک قیمت؛ از ۲۰۰ به بالا یک قیمت دیگر.",
        reply_markup=yes_no("addp:tiers"),
    )


@router.callback_query(F.data == "addp:tiers:no")
@guard("products.create")
async def add_product_no_tiers(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await finalize_new_product(cb.message, cb.from_user.id, state)


@router.callback_query(F.data == "addp:tiers:yes")
@guard("products.create")
async def add_product_tiers_yes(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.tier_min)
    await cb.message.answer(
        "حداقل تعداد پلکان اول را وارد کنید.\n"
        "مثال: اگر تخفیف از ۱۰۰ عدد شروع می‌شود، عدد ۱۰۰ را بفرستید."
    )
    await cb.answer()


@router.callback_query(F.data == "addp:tier_more")
@guard("products.create")
async def add_product_tier_more(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tiers = list(data.get("tiers") or [])
    if any(t.get("max_qty") is None for t in tiers):
        return await cb.answer("پلکان نامحدود دارید؛ بعد از آن بازه دیگری نمی‌شود اضافه کرد.", show_alert=True)
    await state.set_state(AddProduct.tier_min)
    await cb.message.answer("حداقل تعداد پلکان بعدی را وارد کنید.")
    await cb.answer()


@router.callback_query(F.data == "addp:tier_done")
@guard("products.create")
async def add_product_tier_done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await finalize_new_product(cb.message, cb.from_user.id, state)


@router.message(AddProduct.tier_min)
async def add_product_tier_min(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return await message.answer("حداقل تعداد باید عددی بزرگ‌تر از صفر باشد.")
    await state.update_data(current_tier_min=int(raw))
    await state.set_state(AddProduct.tier_max)
    await message.answer(
        "حداکثر تعداد این بازه را وارد کنید.\n"
        "برای «این تعداد به بالا» عدد ۰ را بفرستید.\n"
        "مثال: بازه ۱۰۰ تا ۱۹۹ → عدد ۱۹۹"
    )


@router.message(AddProduct.tier_max)
async def add_product_tier_max(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("حداکثر تعداد باید عدد باشد؛ برای نامحدود ۰ بفرستید.")
    data = await state.get_data()
    min_qty = int(data["current_tier_min"])
    max_raw = int(raw)
    max_qty = None if max_raw == 0 else max_raw
    if max_qty is not None and max_qty < min_qty:
        return await message.answer("حداکثر نمی‌تواند از حداقل کمتر باشد. دوباره وارد کنید.")
    tiers = list(data.get("tiers") or [])
    if tier_overlaps(tiers, min_qty, max_qty):
        await state.set_state(AddProduct.tier_min)
        return await message.answer(
            "این بازه با یکی از پلکان‌های قبلی هم‌پوشانی دارد.\n"
            "حداقل تعداد یک بازه جدید و بدون هم‌پوشانی را وارد کنید."
        )
    await state.update_data(current_tier_max=max_qty)
    await state.set_state(AddProduct.tier_price)
    await message.answer("قیمت واحد برای این بازه را به تومان وارد کنید.")


@router.message(AddProduct.tier_price)
async def add_product_tier_price(message: Message, state: FSMContext):
    try:
        unit_price = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت پلکان نامعتبر است. فقط مبلغ را به تومان وارد کنید.")
    data = await state.get_data()
    tiers = list(data.get("tiers") or [])
    tier = {
        "min_qty": int(data["current_tier_min"]),
        "max_qty": data.get("current_tier_max"),
        "unit_price": unit_price,
    }
    tiers.append(tier)
    await state.update_data(tiers=tiers)
    rows = []
    if tier["max_qty"] is not None:
        rows.append([InlineKeyboardButton(text="➕ افزودن بازه دیگر", callback_data="addp:tier_more")])
    rows.append([InlineKeyboardButton(text="✅ پایان و ثبت محصول", callback_data="addp:tier_done")])
    await message.answer(
        "✅ این پلکان اضافه شد.\n\n" + format_tiers(tiers),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---------------- PRODUCT EDIT ----------------

@router.callback_query(F.data == "admin:edit_product")
@guard("products.edit")
async def edit_product_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.edit"):
        return
    await state.clear()
    await state.set_state(EditProduct.query)
    await cb.message.answer("نام یا کد محصول را ارسال کنید.")
    await cb.answer()


@router.message(EditProduct.query)
async def edit_product_query(message: Message, state: FSMContext):
    q = message.text.strip()
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).where(
            (Product.name.ilike(f"%{q}%")) | (Product.internal_sku.ilike(f"%{q}%"))
        ).limit(10))).scalars().all()
    if not rows:
        return await message.answer("محصولی پیدا نشد.")
    kb = [[
        InlineKeyboardButton(
            text=f"{p.internal_sku} | {p.name}",
            callback_data=f"editp:select:{p.id}"
        )
    ] for p in rows]
    await message.answer(
        "محصول را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@router.callback_query(F.data.startswith("editp:select:"))
@guard("products.edit")
async def edit_select(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        p = await session.get(Product, pid)

    await state.update_data(product_id=pid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="نام", callback_data="editp:field:name"),
            InlineKeyboardButton(text="💰 ویرایش قیمت‌ها (پشت سر هم)", callback_data="editp:pricing"),
        ],
        [
            InlineKeyboardButton(text="توضیحات", callback_data="editp:field:description"),
            InlineKeyboardButton(text="عکس اصلی", callback_data="editp:field:photo"),
        ],
        [InlineKeyboardButton(text="دسته‌بندی", callback_data="editmeta:category"),
         InlineKeyboardButton(text="برند", callback_data="editmeta:brand")],
        [InlineKeyboardButton(text="فعال/غیرفعال", callback_data="editp:toggle")],
    ])
    await cb.message.answer(
        f"محصول: {p.name}\n"
        f"کد: {p.internal_sku}\n"
        f"قیمت پایه: {fa_num(p.purchase_price)} تومان\n"
        f"فروش خرده: {fa_num(p.base_price)} تومان\n"
        f"فروش عمده: {fa_num(p.wholesale_price)} تومان\n\n"
        "چه چیزی ویرایش شود؟",
        reply_markup=kb
    )
    await cb.answer()


async def tier_summary_text(session, product_id: int) -> str:
    tiers = (await session.execute(
        select(PriceTier).where(PriceTier.product_id == product_id).order_by(PriceTier.min_qty)
    )).scalars().all()
    if not tiers:
        return "بدون قیمت پلکانی"
    return "\n".join(
        f"• از {fa_num(t.min_qty)} تا {'بالاتر' if t.max_qty is None else fa_num(t.max_qty)}: "
        f"{fa_num(t.unit_price)} تومان"
        for t in tiers
    )


async def save_pending_prices(cb_or_message, state: FSMContext):
    """ذخیره یکجای قیمت‌های واردشده و اطلاع‌رسانی."""
    data = await state.get_data()
    pid = data.get("product_id")
    pending = dict(data.get("pending_prices") or {})
    changes = []
    async with SessionLocal() as session:
        product = await session.get(Product, pid)
        if not product:
            await state.clear()
            await cb_or_message.answer("محصول پیدا نشد.")
            return None
        actor = await session.scalar(select(User).where(User.telegram_id == cb_or_message.from_user.id))
        for price_type, raw in pending.items():
            if raw is None:
                continue
            new_price = Decimal(raw)
            old = await set_product_price(
                session, product, new_price, actor.id if actor else None, "manual", price_type
            )
            if Decimal(old) != new_price:
                changes.append((price_type, Decimal(old), new_price))
        if changes:
            await add_audit(
                session, actor.id if actor else None, "product.edit.prices", "product", product.id,
                {pt: {"old": str(old), "new": str(new)} for pt, old, new in changes},
            )
        await session.commit()
        product_id = product.id
        name = product.name
        prices = (product.purchase_price, product.base_price, product.wholesale_price)
        tiers_text = await tier_summary_text(session, product_id)

    if changes:
        try:
            await sync_product_post(cb_or_message.bot, product_id)
        except Exception:
            pass

    lines = [f"✅ قیمت‌های «{name}» ذخیره شد.", ""]
    if changes:
        for price_type, old, new in changes:
            lines.append(f"{PRICE_LABELS[price_type]}: {fa_num(old)} ← {fa_num(new)} تومان")
    else:
        lines.append("تغییری ثبت نشد (همه مقادیر قبلی حفظ شدند).")
    lines.append("")
    lines.append(f"💰 قیمت پایه: {fa_num(prices[0])} تومان")
    lines.append(f"🛒 فروش خرده: {fa_num(prices[1])} تومان")
    lines.append(f"📦 فروش عمده: {fa_num(prices[2])} تومان")
    lines.append("")
    lines.append(f"پلکان‌ها:\n{tiers_text}")
    return "\n".join(lines), product_id


async def ask_price_step(target, state: FSMContext, field: str):
    data = await state.get_data()
    pid = data.get("product_id")
    async with SessionLocal() as session:
        product = await session.get(Product, pid)
        current = getattr(product, PRICE_ATTRS[field]) if product else 0
    step_no = {"purchase": 1, "retail": 2, "wholesale": 3}[field]
    extra = "\nاگر نمی‌خواهید تغییر کند، علامت - را بفرستید." if field != "purchase" else ""
    await target.answer(
        f"مرحله {fa_num(step_no)} از ۳ — {PRICE_LABELS[field]}\n\n"
        f"مقدار فعلی: {fa_num(current)} تومان\n"
        f"مقدار جدید را به تومان و فقط به عدد ارسال کنید.{extra}"
    )


@router.callback_query(F.data == "editp:pricing")
@guard("products.edit")
async def edit_pricing_menu(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pid = data.get("product_id")
    async with SessionLocal() as session:
        p = await session.get(Product, pid)
        if not p:
            return await cb.answer("محصول پیدا نشد.", show_alert=True)
        tiers_text = await tier_summary_text(session, pid)
    await state.update_data(pending_prices={})
    await state.set_state(EditPrices.purchase)
    await cb.message.answer(
        f"💰 ویرایش قیمت‌های «{p.name}»\n\n"
        f"قیمت پایه: {fa_num(p.purchase_price)} تومان\n"
        f"فروش خرده: {fa_num(p.base_price)} تومان\n"
        f"فروش عمده: {fa_num(p.wholesale_price)} تومان\n"
        f"پلکان‌ها:\n{tiers_text}\n\n"
        "همه قیمت‌ها را پشت سر هم می‌پرسم؛ هر کدام را خواستید تغییر دهید."
    )
    await ask_price_step(cb.message, state, "purchase")
    await cb.answer()


async def handle_price_step(message: Message, state: FSMContext, field: str):
    data = await state.get_data()
    pending = dict(data.get("pending_prices") or {})
    raw = (message.text or "").strip()
    if raw == "-":
        pending[field] = None
    else:
        try:
            pending[field] = str(parse_money(raw))
        except Exception:
            return await message.answer(
                f"{PRICE_LABELS[field]} نامعتبر است. فقط مبلغ به تومان یا - برای رد کردن."
            )
    await state.update_data(pending_prices=pending)
    nxt = {"purchase": "retail", "retail": "wholesale"}.get(field)
    if nxt:
        await state.set_state(getattr(EditPrices, nxt))
        return await ask_price_step(message, state, nxt)

    # آخرین مرحله: ذخیره یکجا
    result = await save_pending_prices(message, state)
    if not result:
        return
    text, product_id = result
    await message.answer(text)
    async with SessionLocal() as session:
        tiers_text = await tier_summary_text(session, product_id)
    await state.set_state(EditPrices.tier_menu)
    await message.answer(
        "می‌خواهید قیمت پلکانی این محصول را هم ویرایش کنید؟\n\n"
        f"{tiers_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن پلکان", callback_data="edittier:add")],
            [InlineKeyboardButton(text="🗑 حذف همه پلکان‌ها", callback_data="edittier:clear")],
            [InlineKeyboardButton(text="✅ پایان ویرایش", callback_data="edittier:done")],
        ]),
    )


@router.message(EditPrices.purchase)
async def edit_price_purchase(message: Message, state: FSMContext):
    await handle_price_step(message, state, "purchase")


@router.message(EditPrices.retail)
async def edit_price_retail(message: Message, state: FSMContext):
    await handle_price_step(message, state, "retail")


@router.message(EditPrices.wholesale)
async def edit_price_wholesale(message: Message, state: FSMContext):
    await handle_price_step(message, state, "wholesale")


@router.callback_query(F.data == "edittier:add")
@guard("products.edit")
async def edit_tier_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EditPrices.tier_min)
    await cb.message.answer("حداقل تعداد این پلکان را ارسال کنید. مثال: ۱۰۰")
    await cb.answer()


@router.callback_query(F.data == "edittier:clear")
@guard("products.edit")
async def edit_tier_clear(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pid = data.get("product_id")
    async with SessionLocal() as session:
        rows = (await session.execute(select(PriceTier).where(PriceTier.product_id == pid))).scalars().all()
        for row in rows:
            await session.delete(row)
        await session.commit()
    await cb.message.answer("✅ همه پلکان‌های این محصول حذف شد.")
    await cb.answer()


@router.callback_query(F.data == "edittier:done")
async def edit_tier_done(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("✅ ویرایش قیمت‌ها به پایان رسید.")
    await cb.answer()


@router.message(EditPrices.tier_min)
async def edit_tier_min(message: Message, state: FSMContext):
    value = parse_int(message.text)
    if not value or value < 1:
        return await message.answer("حداقل تعداد باید عددی بزرگ‌تر از صفر باشد.")
    await state.update_data(tier_min=value)
    await state.set_state(EditPrices.tier_max)
    await message.answer("حداکثر تعداد را وارد کنید؛ برای «این تعداد به بالا» عدد ۰ بفرستید.")


@router.message(EditPrices.tier_max)
async def edit_tier_max(message: Message, state: FSMContext):
    value = parse_int(message.text)
    if value is None:
        return await message.answer("عدد معتبر وارد کنید؛ برای نامحدود ۰.")
    data = await state.get_data()
    min_qty = int(data.get("tier_min") or 0)
    max_qty = None if value == 0 else value
    if max_qty is not None and max_qty < min_qty:
        return await message.answer("حداکثر نمی‌تواند از حداقل کمتر باشد. دوباره وارد کنید.")
    await state.update_data(tier_max=max_qty)
    await state.set_state(EditPrices.tier_price)
    await message.answer("قیمت واحد این بازه را به تومان وارد کنید.")


@router.message(EditPrices.tier_price)
async def edit_tier_price(message: Message, state: FSMContext):
    try:
        price = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت نامعتبر است.")
    data = await state.get_data()
    pid = data.get("product_id")
    min_qty = int(data.get("tier_min") or 0)
    max_qty = data.get("tier_max")
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(PriceTier).where(PriceTier.product_id == pid)
        )).scalars().all()
        existing_rows = [{"min_qty": t.min_qty, "max_qty": t.max_qty} for t in existing]
        if tier_overlaps(existing_rows, min_qty, max_qty):
            return await message.answer(
                "این بازه با پلکان‌های موجود هم‌پوشانی دارد. بازه دیگری وارد کنید."
            )
        session.add(PriceTier(
            product_id=pid, min_qty=min_qty, max_qty=max_qty, unit_price=price
        ))
        await session.commit()
        tiers_text = await tier_summary_text(session, pid)
    await state.set_state(EditPrices.tier_menu)
    await message.answer(
        "✅ پلکان اضافه شد.\n\n" + tiers_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن پلکان دیگر", callback_data="edittier:add")],
            [InlineKeyboardButton(text="✅ پایان ویرایش", callback_data="edittier:done")],
        ]),
    )


@router.callback_query(F.data.startswith("editp:pricefield:"))
@guard("products.edit")
async def edit_price_field(cb: CallbackQuery, state: FSMContext):
    price_type = cb.data.split(":")[2]
    data = await state.get_data()
    async with SessionLocal() as session:
        p = await session.get(Product, data["product_id"])
    current = {
        "purchase": p.purchase_price,
        "retail": p.base_price,
        "wholesale": p.wholesale_price,
    }[price_type]
    title = {"purchase":"قیمت پایه", "retail":"فروش خرده", "wholesale":"فروش عمده"}[price_type]
    await state.update_data(field=f"price_{price_type}")
    await state.set_state(EditProduct.value)
    await cb.message.answer(
        f"{title} فعلی: {fa_num(current)} تومان\n"
        f"{title} جدید را فقط به عدد و به تومان وارد کنید."
    )
    await cb.answer()


@router.callback_query(F.data.startswith("editp:field:"))
@guard("products.edit")
async def edit_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    data = await state.get_data()

    async with SessionLocal() as session:
        p = await session.get(Product, data["product_id"])

    await state.update_data(field=field)

    if field == "photo":
        await state.set_state(EditProduct.photo)
        await cb.message.answer("عکس اصلی جدید محصول را ارسال کنید.")
    elif field == "price":
        await state.set_state(EditProduct.value)
        await cb.message.answer(
            f"قیمت فعلی محصول: {fa_num(p.base_price)} تومان\n\n"
            "قیمت جدید را به «تومان» و فقط به عدد وارد کنید.\n"
            "مثال: ۱۷۵۰۰۰"
        )
    elif field == "name":
        await state.set_state(EditProduct.value)
        await cb.message.answer(f"نام فعلی: {p.name}\nنام جدید را ارسال کنید.")
    else:
        await state.set_state(EditProduct.value)
        await cb.message.answer(
            f"توضیحات فعلی:\n{p.description or '—'}\n\n"
            "توضیحات جدید را ارسال کنید."
        )
    await cb.answer()


@router.message(EditProduct.photo, F.photo)
async def edit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    async with SessionLocal() as session:
        p = await session.get(Product, data["product_id"])
        actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        p.telegram_file_id = file_id
        # عکس جدید به گالری هم اضافه می‌شود
        max_sort = await session.scalar(
            select(func.coalesce(func.max(ProductImage.sort_order), -1))
            .where(ProductImage.product_id == p.id)
        )
        session.add(ProductImage(
            product_id=p.id,
            telegram_file_id=file_id,
            sort_order=int(max_sort or -1) + 1,
        ))
        await add_audit(session, actor.id if actor else None, "product.photo", "product", p.id)
        await session.commit()
    await state.clear()
    await message.answer("✅ عکس اصلی محصول به‌روزرسانی شد.")


@router.message(EditProduct.value)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data["field"]

    async with SessionLocal() as session:
        p = await session.get(Product, data["product_id"])
        actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))

        if field == "name":
            p.name = message.text.strip()

        elif field == "description":
            p.description = message.text.strip()

        elif field.startswith("price_"):
            price_type = field.split("_", 1)[1]
            try:
                new_price = parse_money(message.text)
            except Exception:
                return await message.answer("قیمت نامعتبر است. مبلغ را فقط به عدد و به تومان وارد کنید.")
            old_price = await set_product_price(
                session, p, new_price, actor.id if actor else None, "manual", price_type
            )
            await add_audit(
                session, actor.id if actor else None, f"product.edit.price.{price_type}", "product", p.id,
                {"old_price_toman": str(old_price), "new_price_toman": str(new_price)}
            )
            await session.commit()
            product_id = p.id
            await state.clear()
            if price_type in {"retail", "wholesale"}:
                await sync_product_post(message.bot, product_id)
            title = {"purchase":"قیمت پایه", "retail":"فروش خرده", "wholesale":"فروش عمده"}[price_type]
            return await message.answer(
                f"✅ {title} ویرایش شد.\n"
                f"قیمت قبلی: {fa_num(old_price)} تومان\n"
                f"قیمت جدید: {fa_num(new_price)} تومان"
            )

        elif field == "price":
            try:
                raw = normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip()
                new_price = Decimal(raw)
                if new_price < 0:
                    raise ValueError
            except Exception:
                return await message.answer(
                    f"قیمت فعلی: {fa_num(p.base_price)} تومان\n"
                    "قیمت نامعتبر است.\n"
                    "لطفاً قیمت جدید را به «تومان» و فقط به عدد وارد کنید."
                )

            old_price = Decimal(p.base_price or 0)
            await set_price(session, p, new_price, actor.id if actor else None, "manual")
            await add_audit(
                session,
                actor.id if actor else None,
                "product.edit.price",
                "product",
                p.id,
                {"old_price_toman": str(old_price), "new_price_toman": str(new_price)}
            )
            await session.commit()
            product_id = p.id
            await state.clear()
            await sync_product_post(message.bot, product_id)
            return await message.answer(
                "✅ قیمت محصول ویرایش شد.\n\n"
                f"قیمت قبلی: {fa_num(old_price)} تومان\n"
                f"قیمت جدید: {fa_num(new_price)} تومان\n"
                "پست کانالِ محصول هم در صورت وجود همگام شد."
            )

        await add_audit(session, actor.id if actor else None, f"product.edit.{field}", "product", p.id)
        await session.commit()

    await state.clear()
    await message.answer("✅ محصول ویرایش شد.")


@router.callback_query(F.data == "editp:toggle")
@guard("products.edit")
async def edit_toggle(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with SessionLocal() as session:
        p = await session.get(Product, data["product_id"])
        p.is_active = not p.is_active
        await session.commit()
    await state.clear()
    await cb.message.answer(f"وضعیت محصول: {'فعال' if p.is_active else 'غیرفعال'}")
    await cb.answer()


# ---------------- BRANDS / CATEGORIES / SUPPLIERS ----------------

@router.callback_query(F.data == "admin:brands")
@guard("products.edit")
async def brands(cb: CallbackQuery):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Brand).order_by(Brand.name))).scalars().all()
        counts = {b.id: await session.scalar(
            select(func.count(Product.id)).where(Product.brand_id == b.id)
        ) for b in rows}
    kb = []
    for b in rows:
        status = "✅" if b.is_active else "⭕"
        kb.append([InlineKeyboardButton(
            text=f"{status} {b.name} ({fa_num(counts.get(b.id) or 0)} محصول)",
            callback_data=f"brand:view:{b.id}",
        )])
    kb.append([InlineKeyboardButton(text="➕ افزودن برند", callback_data="brand:add")])
    kb.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    await cb.message.answer(
        "🗂 برندها\n\nبرای تغییر نام یا فعال/غیرفعال کردن، روی برند بزنید. "
        "هیچ برندی با باز کردن، غیرفعال نمی‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("brand:view:"))
@guard("products.edit")
async def brand_view(cb: CallbackQuery):
    bid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        brand = await session.get(Brand, bid)
        if not brand:
            return await cb.answer("برند پیدا نشد.", show_alert=True)
        count = await session.scalar(select(func.count(Product.id)).where(Product.brand_id == bid))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییر نام", callback_data=f"brand:rename:{bid}")],
        [InlineKeyboardButton(
            text="⛔ غیرفعال کردن" if brand.is_active else "✅ فعال کردن",
            callback_data=f"brand:toggle:{bid}",
        )],
        [InlineKeyboardButton(text="⬅️ برندها", callback_data="admin:brands")],
    ])
    await cb.message.answer(
        f"🏷 {brand.name}\nوضعیت: {'فعال' if brand.is_active else 'غیرفعال'}\n"
        f"تعداد محصول: {fa_num(count or 0)}",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "brand:add")
@guard("products.edit")
async def brand_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BrandFlow.name)
    await cb.message.answer("نام برند جدید را ارسال کنید.")
    await cb.answer()


@router.message(BrandFlow.name)
async def brand_add_save(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام برند خالی است.")
    async with SessionLocal() as session:
        existing = await session.scalar(select(Brand).where(Brand.name == name))
        if existing:
            existing.is_active = True
            await session.commit()
            await message.answer(f"ℹ️ برند «{name}» از قبل بود و فعال شد.")
        else:
            session.add(Brand(name=name, is_active=True))
            await session.commit()
            await message.answer(f"✅ برند «{name}» ثبت شد.")
    await state.clear()


@router.callback_query(F.data.startswith("brand:rename:"))
@guard("products.edit")
async def brand_rename(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[2])
    await state.update_data(brand_id=bid)
    await state.set_state(BrandFlow.rename)
    await cb.message.answer("نام جدید برند را ارسال کنید.")
    await cb.answer()


@router.message(BrandFlow.rename)
async def brand_rename_save(message: Message, state: FSMContext):
    data = await state.get_data()
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام برند خالی است.")
    async with SessionLocal() as session:
        brand = await session.get(Brand, data.get("brand_id"))
        if not brand:
            await state.clear()
            return await message.answer("برند پیدا نشد.")
        brand.name = name
        await session.commit()
    await state.clear()
    await message.answer(f"✅ نام برند به «{name}» تغییر کرد.")


@router.callback_query(F.data.startswith("brand:toggle:"))
@guard("products.edit")
async def brand_toggle(cb: CallbackQuery):
    bid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        brand = await session.get(Brand, bid)
        if not brand:
            return await cb.answer("برند پیدا نشد.", show_alert=True)
        brand.is_active = not brand.is_active
        await session.commit()
        status = brand.is_active
    await cb.message.answer(f"وضعیت برند: {'فعال' if status else 'غیرفعال'}")
    await cb.answer()


@router.callback_query(F.data == "admin:categories")
@guard("products.edit")
async def categories(cb: CallbackQuery):
    async with SessionLocal() as session:
        tree = await category_tree(session)
        counts = {}
        for cat, _depth in tree:
            counts[cat.id] = await session.scalar(
                select(func.count(Product.id)).where(Product.category_id == cat.id)
            )
    if not tree:
        await cb.message.answer(
            "🗂 دسته‌بندی‌ها\n\nهنوز دسته‌بندی‌ای ثبت نشده است.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ افزودن دسته‌بندی اصلی", callback_data="cat:add")],
                [InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")],
            ]),
        )
        return await cb.answer()

    kb = []
    for cat, depth in tree:
        label = category_label(cat.name, depth, cat.is_active)
        kb.append([InlineKeyboardButton(
            text=f"{label} ({fa_num(counts.get(cat.id) or 0)})",
            callback_data=f"cat:view:{cat.id}",
        )])
    kb.append([InlineKeyboardButton(text="➕ افزودن دسته‌بندی اصلی", callback_data="cat:add")])
    kb.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    await cb.message.answer(
        "🗂 دسته‌بندی‌ها\n\n"
        "روی هر دسته بزنید تا زیرمجموعه یا محصول به آن اضافه کنید، نامش را تغییر دهید یا غیرفعالش کنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cat:view:"))
@guard("products.edit")
async def category_view(cb: CallbackQuery):
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        cat = await session.get(Category, cid)
        if not cat:
            return await cb.answer("دسته‌بندی پیدا نشد.", show_alert=True)
        path = await category_path(session, cat)
        children = (await session.execute(
            select(Category).where(Category.parent_id == cid).order_by(Category.name)
        )).scalars().all()
        products = (await session.execute(
            select(Product).where(Product.category_id == cid).order_by(Product.name).limit(10)
        )).scalars().all()
        product_count = await session.scalar(
            select(func.count(Product.id)).where(Product.category_id == cid)
        )
    lines = [f"🗂 {path}", f"وضعیت: {'فعال' if cat.is_active else 'غیرفعال'}"]
    if children:
        lines.append("زیرمجموعه‌ها: " + "، ".join(c.name for c in children))
    if products:
        lines.append("محصولات: " + "، ".join(p.name for p in products))
    if product_count and product_count > len(products):
        lines.append(f"… و {fa_num(product_count - len(products))} محصول دیگر")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن زیرمجموعه", callback_data=f"cat:sub:{cid}")],
        [InlineKeyboardButton(text="📦 افزودن محصول در این دسته", callback_data=f"cat:prod:{cid}")],
        [InlineKeyboardButton(text="✏️ تغییر نام", callback_data=f"cat:rename:{cid}")],
        [InlineKeyboardButton(
            text="⛔ غیرفعال کردن" if cat.is_active else "✅ فعال کردن",
            callback_data=f"cat:toggle:{cid}",
        )],
        [InlineKeyboardButton(text="⬅️ دسته‌بندی‌ها", callback_data="admin:categories")],
    ])
    await cb.message.answer("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "cat:add")
@guard("products.edit")
async def category_add(cb: CallbackQuery, state: FSMContext):
    await state.update_data(parent_id=None)
    await state.set_state(CategoryFlow.name)
    await cb.message.answer("نام دسته‌بندی اصلی را ارسال کنید.")
    await cb.answer()


@router.callback_query(F.data.startswith("cat:sub:"))
@guard("products.edit")
async def category_add_sub(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        parent = await session.get(Category, cid)
    await state.update_data(parent_id=cid)
    await state.set_state(CategoryFlow.name)
    await cb.message.answer(
        f"نام زیرمجموعه جدید برای «{parent.name if parent else '-'}» را ارسال کنید.\n"
        "مثال: سمند، پژو، ۴۰۵"
    )
    await cb.answer()


@router.message(CategoryFlow.name)
async def category_save(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام دسته‌بندی خالی است.")
    data = await state.get_data()
    parent_id = data.get("parent_id")
    async with SessionLocal() as session:
        dup = await session.scalar(select(Category).where(
            Category.name == name, Category.parent_id == parent_id
        ))
        if dup:
            dup.is_active = True
            await session.commit()
            await state.clear()
            return await message.answer(f"ℹ️ دسته‌بندی «{name}» از قبل بود و فعال شد.")
        cat = Category(name=name, parent_id=parent_id, is_active=True)
        session.add(cat)
        await session.commit()
        await session.refresh(cat)
        cat_id = cat.id
        path = await category_path(session, cat)
    await state.clear()
    await message.answer(
        f"✅ دسته‌بندی «{path}» ثبت شد.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن زیرمجموعه", callback_data=f"cat:sub:{cat_id}")],
            [InlineKeyboardButton(text="📦 افزودن محصول", callback_data=f"cat:prod:{cat_id}")],
            [InlineKeyboardButton(text="⬅️ دسته‌بندی‌ها", callback_data="admin:categories")],
        ]),
    )


@router.callback_query(F.data.startswith("cat:rename:"))
@guard("products.edit")
async def category_rename(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await state.update_data(category_id=cid)
    await state.set_state(CategoryFlow.rename)
    await cb.message.answer("نام جدید دسته‌بندی را ارسال کنید.")
    await cb.answer()


@router.message(CategoryFlow.rename)
async def category_rename_save(message: Message, state: FSMContext):
    data = await state.get_data()
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام خالی است.")
    async with SessionLocal() as session:
        cat = await session.get(Category, data.get("category_id"))
        if not cat:
            await state.clear()
            return await message.answer("دسته‌بندی پیدا نشد.")
        cat.name = name
        await session.commit()
    await state.clear()
    await message.answer(f"✅ نام دسته‌بندی به «{name}» تغییر کرد.")


@router.callback_query(F.data.startswith("cat:toggle:"))
@guard("products.edit")
async def category_toggle(cb: CallbackQuery):
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        cat = await session.get(Category, cid)
        if not cat:
            return await cb.answer("دسته‌بندی پیدا نشد.", show_alert=True)
        cat.is_active = not cat.is_active
        await session.commit()
        status = cat.is_active
        name = cat.name
    await cb.message.answer(
        f"دسته‌بندی «{name}» {'فعال' if status else 'غیرفعال'} شد."
        + ("" if status else "\nمحصولات این دسته در لیست مشتری نمایش داده نمی‌شوند.")
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cat:prod:"))
@guard("products.edit")
async def category_add_product(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        cat = await session.get(Category, cid)
        if not cat:
            return await cb.answer("دسته‌بندی پیدا نشد.", show_alert=True)
        path = await category_path(session, cat)
    await state.update_data(category_id=cid)
    await state.set_state(CategoryFlow.product_name)
    await cb.message.answer(
        f"افزودن محصول در دسته «{path}»\n\nنام محصول را ارسال کنید."
    )
    await cb.answer()


@router.message(CategoryFlow.product_name)
async def category_product_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام محصول خالی است.")
    await state.update_data(product_name=name)
    await state.set_state(CategoryFlow.product_price)
    await message.answer("قیمت پایه محصول را به تومان وارد کنید. مثال: ۱۲۰۰۰۰")


@router.message(CategoryFlow.product_price)
async def category_product_price(message: Message, state: FSMContext):
    try:
        value = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت نامعتبر است. فقط مبلغ به تومان وارد کنید.")
    await state.update_data(purchase_price=value)
    await state.set_state(CategoryFlow.product_retail)
    await message.answer("قیمت فروش خرده را به تومان وارد کنید. مثال: ۱۵۹۰۰۰")


@router.message(CategoryFlow.product_retail)
async def category_product_retail(message: Message, state: FSMContext):
    try:
        value = parse_money(message.text)
    except Exception:
        return await message.answer("قیمت نامعتبر است.")
    await state.update_data(retail_price=value)
    await state.set_state(CategoryFlow.product_wholesale)
    await message.answer("قیمت فروش عمده را به تومان وارد کنید. اگر با خرده یکی است، - بفرستید.")


@router.message(CategoryFlow.product_wholesale)
async def category_product_wholesale(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    if raw == "-":
        wholesale = data["retail_price"]
    else:
        try:
            wholesale = parse_money(raw)
        except Exception:
            return await message.answer("قیمت نامعتبر است؛ مبلغ یا - بفرستید.")
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        sku = await next_sku(session)
        product = Product(
            internal_sku=sku,
            name=data["product_name"],
            category_id=data.get("category_id"),
            purchase_price=data["purchase_price"],
            base_price=data["retail_price"],
            wholesale_price=wholesale,
        )
        session.add(product)
        await session.flush()
        product_id = product.id
        await add_audit(session, actor.id if actor else None, "product.create", "product", product_id, {
            "sku": sku, "name": product.name, "category_id": product.category_id,
            "source": "category_quick_add",
        })
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ محصول «{product.name}» ثبت شد.\nکد: {sku}\n"
        f"خرده: {fa_num(product.base_price)} تومان\nعمده: {fa_num(product.wholesale_price)} تومان"
    )
    published, info = await auto_publish_if_enabled(message.bot, product_id)
    if published:
        await message.answer(f"📣 محصول در کانال هم منتشر شد.\n{info}".strip())
    elif info:
        await message.answer(f"⚠️ ارسال خودکار به کانال انجام نشد:\n{info}")


@router.callback_query(F.data == "admin:suppliers")
@guard("products.edit")
async def suppliers(cb: CallbackQuery):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Supplier).order_by(Supplier.name)
        )).scalars().all()
    kb = []
    for sup in rows:
        status = "✅" if sup.is_active else "⭕"
        kb.append([InlineKeyboardButton(
            text=f"{status} {sup.name}", callback_data=f"supplier:view:{sup.id}"
        )])
    kb.append([InlineKeyboardButton(text="➕ افزودن تأمین‌کننده", callback_data="supplier:add")])
    kb.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    await cb.message.answer(
        "🏭 تأمین‌کننده‌ها", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("supplier:view:"))
@guard("products.edit")
async def supplier_view(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        sup = await session.get(Supplier, sid)
        if not sup:
            return await cb.answer("تأمین‌کننده پیدا نشد.", show_alert=True)
    await cb.message.answer(
        f"🏭 {sup.name}\nتلفن: {sup.phone or '-'}\n"
        f"وضعیت: {'فعال' if sup.is_active else 'غیرفعال'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⛔ غیرفعال کردن" if sup.is_active else "✅ فعال کردن",
                callback_data=f"supplier:toggle:{sid}",
            )],
            [InlineKeyboardButton(text="⬅️ تأمین‌کننده‌ها", callback_data="admin:suppliers")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("supplier:toggle:"))
@guard("products.edit")
async def supplier_toggle(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        sup = await session.get(Supplier, sid)
        if not sup:
            return await cb.answer("تأمین‌کننده پیدا نشد.", show_alert=True)
        sup.is_active = not sup.is_active
        await session.commit()
        status = sup.is_active
    await cb.message.answer(f"وضعیت تأمین‌کننده: {'فعال' if status else 'غیرفعال'}")
    await cb.answer()


@router.callback_query(F.data == "supplier:add")
@guard("products.edit")
async def supplier_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddEntity.value)
    await state.update_data(entity="supplier")
    await cb.message.answer("نام تأمین‌کننده را ارسال کنید.")
    await cb.answer()


@router.message(AddEntity.value)
async def add_entity_value(message: Message, state: FSMContext):
    data = await state.get_data()
    entity = data.get("entity")
    name = (message.text or "").strip()
    if not name:
        return await message.answer("نام خالی است.")
    if entity == "supplier":
        await state.update_data(name=name)
        await state.set_state(AddEntity.phone)
        return await message.answer("شماره تماس تأمین‌کننده را ارسال کنید یا - بفرستید.")

    async with SessionLocal() as session:
        model = Brand if entity == "brand" else Category
        session.add(model(name=name))
        await session.commit()
    await state.clear()
    await message.answer("✅ ثبت شد.")


@router.message(AddEntity.phone)
async def add_supplier_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip()
    async with SessionLocal() as session:
        session.add(Supplier(name=data["name"], phone=None if text == "-" else text))
        await session.commit()
    await state.clear()
    await message.answer("✅ تأمین‌کننده ثبت شد.")


@router.callback_query(F.data == "legacy:admin:orders")
async def orders(cb: CallbackQuery):
    if not await require(cb, "orders.view"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Order).order_by(Order.id.desc()).limit(30)
        )).scalars().all()
    await cb.message.answer(
        "\n".join(f"{o.order_no} | {o.status} | {fa_num(o.total_amount)} تومان" for o in rows)
        if rows else "سفارشی ثبت نشده."
    )
    await cb.answer()


@router.callback_query(F.data == "admin:payments")
@guard("payments.view")
async def payments(cb: CallbackQuery):
    if not await require(cb, "payments.view"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Payment).order_by(Payment.id.desc()).limit(30)
        )).scalars().all()
    await cb.message.answer(
        "\n".join(f"#{p.id} | {p.status}" for p in rows) if rows else "فیشی ثبت نشده."
    )
    await cb.answer()


@router.callback_query(F.data == "legacy:admin:users")
async def users(cb: CallbackQuery):
    if not await require(cb, "users.manage"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(User).order_by(User.id.desc()).limit(50)
        )).scalars().all()
    await cb.message.answer(
        "\n".join(
            f"{u.telegram_id} | {u.first_name or '-'} | {'Owner' if u.is_owner else 'User'}"
            for u in rows
        )
    )
    await cb.answer()


@router.callback_query(F.data == "legacy:admin:admins")
async def admins(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    async with SessionLocal() as session:
        perms = (await session.execute(select(Permission).order_by(Permission.id))).scalars().all()
    kb = [[InlineKeyboardButton(text="➕ افزودن مدیر", callback_data="admins:add")]]
    kb += [[InlineKeyboardButton(text=p.title, callback_data=f"admins:perm:{p.id}")] for p in perms]
    await cb.message.answer(
        "🔐 مدیران و دسترسی‌ها",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await cb.answer()


@router.callback_query(F.data == "admins:add")
@guard("admins.manage")
async def admin_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddAdmin.telegram_id)
    await cb.message.answer("Telegram ID عددی کاربری که قبلاً /start زده را ارسال کنید.")
    await cb.answer()


@router.message(AddAdmin.telegram_id)
async def admin_add_id(message: Message, state: FSMContext):
    raw = normalize_digits(message.text.strip())
    if not raw.isdigit():
        return await message.answer("ID نامعتبر است.")
    tg_id = int(raw)
    async with SessionLocal() as session:
        u = await session.scalar(select(User).where(User.telegram_id == tg_id))
        if not u:
            return await message.answer("این کاربر هنوز /start نزده است.")
        perms = (await session.execute(select(Permission))).scalars().all()
        for p in perms:
            existing = await session.scalar(select(UserPermission).where(
                UserPermission.user_id == u.id,
                UserPermission.permission_id == p.id
            ))
            if not existing:
                session.add(UserPermission(
                    user_id=u.id,
                    permission_id=p.id,
                    is_allowed=True
                ))
        await session.commit()
    await state.clear()
    await message.answer("✅ مدیر اضافه شد و فعلاً همه دسترسی‌ها برایش فعال شد.")


@router.callback_query(F.data == "legacy:admin:settings")
@guard("settings.manage")
async def bank_settings(cb: CallbackQuery):
    if not await require(cb, "settings.manage"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(BankAccount).order_by(BankAccount.id)
        )).scalars().all()
    kb = [[InlineKeyboardButton(
        text=f"{'✅' if x.is_active else '⭕'} {x.title}",
        callback_data=f"bank:toggle:{x.id}"
    )] for x in rows]
    kb.append([InlineKeyboardButton(text="➕ افزودن حساب", callback_data="bank:add")])
    await cb.message.answer(
        "⚙️ حساب‌های بانکی",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await cb.answer()


@router.callback_query(F.data == "bank:add")
@guard("settings.manage")
async def bank_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddBank.title)
    await cb.message.answer("عنوان حساب را ارسال کنید.")
    await cb.answer()


@router.message(AddBank.title)
async def bank_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddBank.card)
    await message.answer("شماره کارت را ارسال کنید.")


@router.message(AddBank.card)
async def bank_card(message: Message, state: FSMContext):
    await state.update_data(card=message.text.strip())
    await state.set_state(AddBank.holder)
    await message.answer("نام صاحب حساب را ارسال کنید.")


@router.message(AddBank.holder)
async def bank_holder(message: Message, state: FSMContext):
    await state.update_data(holder=message.text.strip())
    await state.set_state(AddBank.iban)
    await message.answer("شماره شبا را ارسال کنید یا - بفرستید.")


@router.message(AddBank.iban)
async def bank_iban(message: Message, state: FSMContext):
    data = await state.get_data()
    async with SessionLocal() as session:
        session.add(BankAccount(
            title=data["title"],
            card_number=data["card"],
            holder_name=data["holder"],
            iban=None if message.text.strip() == "-" else message.text.strip(),
            is_active=True,
        ))
        await session.commit()
    await state.clear()
    await message.answer("✅ حساب بانکی ثبت شد.")


@router.callback_query(F.data.startswith("bank:toggle:"))
@guard("settings.manage")
async def bank_toggle(cb: CallbackQuery):
    bid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        b = await session.get(BankAccount, bid)
        b.is_active = not b.is_active
        await session.commit()
    await cb.message.answer(f"وضعیت حساب: {'فعال' if b.is_active else 'غیرفعال'}")
    await cb.answer()


@router.callback_query(F.data == "legacy:admin:reports")
async def reports(cb: CallbackQuery):
    if not await require(cb, "reports.view"):
        return
    async with SessionLocal() as session:
        product_count = await session.scalar(select(func.count(Product.id)))
        user_count = await session.scalar(select(func.count(User.id)))
        order_count = await session.scalar(select(func.count(Order.id)))
        revenue = await session.scalar(
            select(func.coalesce(func.sum(Order.total_amount), 0))
            .where(Order.status.in_(["CONFIRMED", "PREPARING", "SHIPPED", "DELIVERED"]))
        )
    await cb.message.answer(
        f"📊 گزارش خلاصه\n\n"
        f"محصولات: {fa_num(product_count)}\n"
        f"کاربران: {fa_num(user_count)}\n"
        f"سفارش‌ها: {fa_num(order_count)}\n"
        f"فروش تأییدشده: {fa_num(revenue)} تومان"
    )
    await cb.answer()


# ---------------- CHANNEL PUBLISHER V2 ----------------

@router.callback_query(F.data == "admin:channel_settings")
@guard("channel.manage", "settings.manage")
async def channel_settings(cb: CallbackQuery):
    async with SessionLocal() as session:
        channels = (await session.execute(
            select(PublishChannel).order_by(PublishChannel.id)
        )).scalars().all()
        footer = await setting_value(session, FOOTER_KEY, "")
        auto_publish = await setting_value(session, AUTO_PUBLISH_KEY, "1")
    active = next((c for c in channels if c.is_active), None)

    lines = ["📡 اتصال به کانال", ""]
    if active:
        lines.append(f"کانال فعال: {active.title}")
        lines.append(f"آیدی: {active.chat_ref}")
    else:
        lines.append("⚠️ هیچ کانالی متصل نیست.")
    lines.append(f"ارسال خودکار محصول جدید: {'✅ روشن' if auto_publish == '1' else '⭕ خاموش'}")
    lines.append("")
    lines.append(f"فوتر پیام‌ها:\n{footer or '— (خالی)'}")
    lines.append("")
    lines.append("ربات باید در کانال «ادمین» باشد تا پیام ارسال و ویرایش شود.")

    rows = [[InlineKeyboardButton(
        text="➕ اتصال / تغییر کانال" if not active else "🔄 تعویض کانال",
        callback_data="channel:add",
    )]]
    if channels:
        for c in channels:
            rows.append([InlineKeyboardButton(
                text=f"{'✅' if c.is_active else '⭕'} {c.title} | {c.chat_ref}",
                callback_data=f"channel:toggle:{c.id}",
            )])
    rows.append([InlineKeyboardButton(text="✏️ ویرایش فوتر پیام کانال", callback_data="channel:footer")])
    rows.append([InlineKeyboardButton(
        text=f"{'✅' if auto_publish == '1' else '⭕'} ارسال خودکار محصول جدید به کانال",
        callback_data="channel:toggle_auto",
    )])
    if active:
        rows.append([InlineKeyboardButton(text="🧪 ارسال پیام آزمایشی", callback_data="channel:test")])
    rows.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data == "channel:add")
@guard("channel.manage", "settings.manage")
async def channel_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ChannelFlow.chat_ref)
    await cb.message.answer(
        "آیدی کانال را ارسال کنید.\n\n"
        "مثال‌ها:\n"
        "• @my_shop_channel  (کانال عمومی)\n"
        "• -1001234567890  (کانال خصوصی؛ آیدی عددی کانال)\n\n"
        "ربات باید از قبل در کانال ادمین شده باشد."
    )
    await cb.answer()


@router.message(ChannelFlow.chat_ref)
async def channel_connect(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        return await message.answer("آیدی کانال خالی است.")
    await message.answer("⏳ در حال بررسی دسترسی ربات در کانال…")
    try:
        channel = await connect_channel(message.bot, raw)
    except ValueError as exc:
        return await message.answer(f"❌ {exc}")
    except Exception as exc:  # noqa: BLE001
        return await message.answer(f"❌ اتصال انجام نشد: {exc}")
    await state.clear()

    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        await add_audit(session, actor.id if actor else None, "channel.connect", "channel", channel.id, {
            "chat_ref": channel.chat_ref, "title": channel.title,
        })
        await session.commit()

    await message.answer(
        "✅ کانال با موفقیت متصل شد.\n\n"
        f"عنوان: {channel.title}\nآیدی: {channel.chat_ref}\n\n"
        "از این به بعد هر محصول جدید (در صورت روشن بودن ارسال خودکار) همراه عکس و توضیحات "
        "به کانال ارسال می‌شود. فوتر پیام‌ها را هم می‌توانید از همین بخش ویرایش کنید."
    )


@router.callback_query(F.data == "channel:footer")
@guard("channel.manage", "settings.manage")
async def channel_footer_edit(cb: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        footer = await setting_value(session, FOOTER_KEY, "")
    await state.set_state(ChannelFlow.footer)
    await cb.message.answer(
        f"فوتر فعلی:\n{footer or '— (خالی)'}\n\n"
        "متن جدید فوتر را ارسال کنید. این متن به انتهای همه پیام‌های کانال اضافه می‌شود.\n"
        "برای حذف فوتر، علامت - را بفرستید."
    )
    await cb.answer()


@router.message(ChannelFlow.footer)
async def channel_footer_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    value = "" if text == "-" else text
    async with SessionLocal() as session:
        await set_setting_value(session, FOOTER_KEY, value)
        actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        await add_audit(session, actor.id if actor else None, "channel.footer", "settings", FOOTER_KEY, {
            "value": value,
        })
        await session.commit()
        posts = (await session.execute(select(ProductChannelPost))).scalars().all()
        product_ids = sorted({p.product_id for p in posts})
    await state.clear()
    await message.answer("✅ فوتر ذخیره شد.")
    if product_ids:
        await message.answer(f"⏳ در حال به‌روزرسانی {fa_num(len(product_ids))} پست موجود در کانال…")
        updated = 0
        for pid in product_ids:
            try:
                await sync_product_post(message.bot, pid)
                updated += 1
            except Exception:
                pass
        await message.answer(f"✅ {fa_num(updated)} پست با فوتر جدید به‌روزرسانی شد.")


@router.callback_query(F.data == "channel:toggle_auto")
@guard("channel.manage", "settings.manage")
async def channel_toggle_auto(cb: CallbackQuery):
    async with SessionLocal() as session:
        current = await setting_value(session, AUTO_PUBLISH_KEY, "1")
        new_value = "0" if current == "1" else "1"
        await set_setting_value(session, AUTO_PUBLISH_KEY, new_value)
        await session.commit()
    await cb.message.answer(
        f"ارسال خودکار محصول جدید: {'✅ روشن' if new_value == '1' else '⭕ خاموش'}"
    )
    await cb.answer()


@router.callback_query(F.data == "channel:test")
@guard("channel.manage", "settings.manage")
async def channel_test(cb: CallbackQuery):
    async with SessionLocal() as session:
        channel = await session.scalar(
            select(PublishChannel).where(PublishChannel.is_active.is_(True))
        )
        footer = await setting_value(session, FOOTER_KEY, "")
    if not channel:
        return await cb.answer("کانال فعالی وجود ندارد.", show_alert=True)
    try:
        await cb.bot.send_message(
            channel.chat_ref,
            "🧪 پیام آزمایشی\nربات با موفقیت به کانال متصل است." + (f"\n\n{footer}" if footer else ""),
        )
        await cb.message.answer("✅ پیام آزمایشی ارسال شد. کانال را بررسی کنید.")
    except Exception as exc:  # noqa: BLE001
        await cb.message.answer(f"❌ ارسال ناموفق: {exc}")
    await cb.answer()


@router.callback_query(F.data.startswith("channel:toggle:"))
@guard("channel.manage", "settings.manage")
async def channel_toggle(cb: CallbackQuery):
    channel_id = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        channel = await session.get(PublishChannel, channel_id)
        if not channel:
            return await cb.answer("کانال پیدا نشد.", show_alert=True)
        new_state = not channel.is_active
        if new_state:
            for other in (await session.execute(select(PublishChannel))).scalars().all():
                other.is_active = False
        channel.is_active = new_state
        await session.commit()
    await cb.message.answer(f"وضعیت کانال: {'فعال' if new_state else 'غیرفعال'}")
    await cb.answer()


@router.callback_query(F.data == "admin:publish")
@guard("channel.publish")
async def publish_menu(cb: CallbackQuery):
    if not await require(cb, "channel.publish"):
        return
    async with SessionLocal() as session:
        channel = await session.scalar(select(PublishChannel).where(PublishChannel.is_active.is_(True)).limit(1))
    if not channel:
        await cb.message.answer("اول از «تنظیمات کانال» یک کانال فعال ثبت کنید.")
        return await cb.answer()
    await cb.message.answer(
        f"📣 ارسال به کانال\nکانال فعال: {channel.title}",
        reply_markup=channel_publish_menu(),
    )
    await cb.answer()


@router.callback_query(F.data == "publish:single")
@guard("channel.publish")
async def publish_single_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "channel.publish"):
        return
    await state.set_state(PublishFlow.single_query)
    await cb.message.answer("نام یا کد محصول را ارسال کنید.")
    await cb.answer()


@router.message(PublishFlow.single_query)
async def publish_single_query(message: Message, state: FSMContext):
    q = message.text.strip()
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).where(
            Product.is_active.is_(True),
            (Product.name.ilike(f"%{q}%")) | (Product.internal_sku.ilike(f"%{q}%"))
        ).limit(15))).scalars().all()
    if not rows:
        return await message.answer("محصولی پیدا نشد.")
    kb = [[InlineKeyboardButton(
        text=f"{p.internal_sku} | {p.name} | {fa_num(p.base_price)} تومان",
        callback_data=f"publish:product:{p.id}"
    )] for p in rows]
    await message.answer("محصول را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.clear()


@router.callback_query(F.data.startswith("publish:product:"))
@guard("channel.publish")
async def publish_product_cb(cb: CallbackQuery):
    if not await require(cb, "channel.publish"):
        return
    pid = int(cb.data.split(":")[2])
    try:
        row = await publish_product(cb.bot, pid)
        text = "✅ محصول به کانال ارسال شد."
        if row.post_url:
            text += f"\n🔗 {row.post_url}"
        await cb.message.answer(text)
    except Exception as e:
        await cb.message.answer(f"❌ ارسال ناموفق بود: {e}")
    await cb.answer()


@router.callback_query(F.data == "publish:brand")
@guard("channel.publish")
async def publish_brand(cb: CallbackQuery):
    if not await require(cb, "channel.publish"):
        return
    async with SessionLocal() as session:
        brands = (await session.execute(select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name))).scalars().all()
    if not brands:
        await cb.message.answer("برندی ثبت نشده.")
        return await cb.answer()
    kb = [[InlineKeyboardButton(text=b.name, callback_data=f"publish:brandgo:{b.id}")] for b in brands]
    await cb.message.answer("برند را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("publish:brandgo:"))
@guard("channel.publish")
async def publish_brand_go(cb: CallbackQuery):
    if not await require(cb, "channel.publish"):
        return
    brand_id = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        products = (await session.execute(select(Product).where(
            Product.brand_id == brand_id, Product.is_active.is_(True)
        ).order_by(Product.base_price))).scalars().all()
    success, errors = await publish_many(cb.bot, [p.id for p in products])
    await cb.message.answer(f"✅ ارسال برند تمام شد.\nموفق: {fa_num(success)}\nخطا: {fa_num(len(errors))}")
    await cb.answer()


@router.callback_query(F.data == "publish:price")
@guard("channel.publish")
async def publish_price_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "channel.publish"):
        return
    await state.set_state(PublishFlow.price_min)
    await cb.message.answer("حداقل قیمت را به تومان وارد کنید. برای بدون حداقل عدد ۰ بفرستید.")
    await cb.answer()


@router.message(PublishFlow.price_min)
async def publish_price_min(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip()
    if not raw.isdigit():
        return await message.answer("عدد معتبر وارد کنید؛ مبلغ به تومان.")
    await state.update_data(min_price=int(raw))
    await state.set_state(PublishFlow.price_max)
    await message.answer("حداکثر قیمت را به تومان وارد کنید. برای بدون سقف عدد ۰ بفرستید.")


@router.message(PublishFlow.price_max)
async def publish_price_max(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip()
    if not raw.isdigit():
        return await message.answer("عدد معتبر وارد کنید؛ مبلغ به تومان.")
    max_price = int(raw)
    data = await state.get_data()
    min_price = int(data.get("min_price", 0))
    async with SessionLocal() as session:
        stmt = select(Product).where(Product.is_active.is_(True), Product.base_price >= min_price)
        if max_price > 0:
            stmt = stmt.where(Product.base_price <= max_price)
        products = (await session.execute(stmt.order_by(Product.base_price))).scalars().all()
    await state.clear()
    success, errors = await publish_many(message.bot, [p.id for p in products])
    await message.answer(
        f"✅ ارسال بازه قیمت تمام شد.\n"
        f"تعداد انتخاب‌شده: {fa_num(len(products))}\n"
        f"موفق: {fa_num(success)}\nخطا: {fa_num(len(errors))}"
    )


@router.callback_query(F.data == "publish:all")
@guard("channel.publish")
async def publish_all(cb: CallbackQuery):
    if not await require(cb, "channel.publish"):
        return
    async with SessionLocal() as session:
        products = (await session.execute(select(Product).where(
            Product.is_active.is_(True)
        ).order_by(Product.brand_id, Product.base_price))).scalars().all()
    success, errors = await publish_many(cb.bot, [p.id for p in products])
    await cb.message.answer(f"✅ ارسال همه تمام شد.\nموفق: {fa_num(success)}\nخطا: {fa_num(len(errors))}")
    await cb.answer()


# ===== V2.3 BULK PRICE CHANGE =====

def price_change_scope_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 انتخاب یک/چند محصول", callback_data="pricechg:scope:selected")],
        [InlineKeyboardButton(text="🗂 یک دسته‌بندی", callback_data="pricechg:scope:category")],
        [InlineKeyboardButton(text="📦 همه محصولات فعال", callback_data="pricechg:scope:all")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="pricechg:cancel")],
    ])


def price_change_field_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="فروش خرده", callback_data="pricechg:field:retail"),
            InlineKeyboardButton(text="فروش عمده", callback_data="pricechg:field:wholesale"),
        ],
        [InlineKeyboardButton(text="خرده + عمده", callback_data="pricechg:field:both_sales")],
        [InlineKeyboardButton(text="قیمت پایه", callback_data="pricechg:field:purchase")],
        [InlineKeyboardButton(text="هر سه قیمت", callback_data="pricechg:field:all")],
    ])


@router.callback_query(F.data == "admin:price_change")
@guard("products.edit")
async def price_change_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.edit"):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📈 افزایش/کاهش درصدی روی هر سه قیمت",
            callback_data="pricechg:quick",
        )],
        [InlineKeyboardButton(text="٪ تغییر درصدی (انتخاب قیمت)", callback_data="pricechg:mode:percent")],
        [InlineKeyboardButton(text="➕➖ تغییر مبلغی", callback_data="pricechg:mode:amount")],
        [InlineKeyboardButton(text="🎯 تعیین قیمت دقیق", callback_data="pricechg:mode:exact")],
    ])
    await cb.message.answer(
        "💰 تغییر قیمت\n\n"
        "روش تغییر را انتخاب کنید. بعد می‌توانید روی یک محصول، چند محصول، یک دسته‌بندی یا همه محصولات اعمال کنید.",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "pricechg:quick")
@guard("products.edit")
async def price_change_quick(cb: CallbackQuery, state: FSMContext):
    await state.update_data(
        price_change_mode="percent",
        price_change_field="all",
        price_change_quick=True,
        selected_product_ids=[],
    )
    await cb.message.answer(
        "📈 افزایش/کاهش درصدی\n\n"
        "این تغییر روی «قیمت پایه، فروش خرده و فروش عمده» هم‌زمان اعمال می‌شود.\n"
        "روی کدام محصولات اعمال شود؟",
        reply_markup=price_change_scope_keyboard(),
    )
    await cb.answer()


async def after_scope_selected(target, state: FSMContext, ids):
    data = await state.get_data()
    await state.update_data(selected_product_ids=list(ids))
    if data.get("price_change_quick"):
        await state.set_state(PriceChangeFlow.value)
        return await target.answer(
            f"✅ {fa_num(len(ids))} محصول انتخاب شد.\n\n"
            "درصد تغییر را وارد کنید.\n"
            "برای افزایش ۱۰٪ عدد ۱۰ و برای کاهش ۵٪ عدد -۵ را بفرستید."
        )
    await target.answer(
        f"✅ {fa_num(len(ids))} محصول انتخاب شد. کدام قیمت تغییر کند؟",
        reply_markup=price_change_field_keyboard(),
    )


@router.callback_query(F.data.startswith("pricechg:mode:"))
@guard("products.edit")
async def price_change_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]
    await state.update_data(price_change_mode=mode, selected_product_ids=[])
    await cb.message.answer("تغییر روی کدام محصولات اعمال شود؟", reply_markup=price_change_scope_keyboard())
    await cb.answer()


@router.callback_query(F.data == "pricechg:scope:selected")
@guard("products.edit")
async def price_change_selected_scope(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PriceChangeFlow.search)
    await cb.message.answer(
        "نام یا کد محصول را جستجو کنید.\n"
        "از نتیجه‌ها یک یا چند محصول را انتخاب کنید؛ بعد می‌توانید جستجوی دیگری هم انجام بدهید."
    )
    await cb.answer()


async def build_price_selection_markup(product_ids, selected_ids):
    async with SessionLocal() as session:
        rows = []
        for pid in product_ids:
            p = await session.get(Product, pid)
            if p:
                mark = "✅" if pid in selected_ids else "⭕"
                rows.append([InlineKeyboardButton(
                    text=f"{mark} {p.internal_sku} | {p.name}", callback_data=f"pricechg:toggle:{p.id}"
                )])
    rows.append([InlineKeyboardButton(text="🔎 جستجوی بیشتر", callback_data="pricechg:more")])
    rows.append([InlineKeyboardButton(text="✅ ادامه با انتخاب‌ها", callback_data="pricechg:selected_done")])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="pricechg:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(PriceChangeFlow.search)
async def price_change_search(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    async with SessionLocal() as session:
        products = (await session.execute(select(Product).where(
            Product.is_active.is_(True),
            (Product.name.ilike(f"%{q}%")) | (Product.internal_sku.ilike(f"%{q}%"))
        ).order_by(Product.name).limit(20))).scalars().all()
    if not products:
        return await message.answer("محصولی پیدا نشد؛ عبارت دیگری جستجو کنید.")
    data = await state.get_data()
    ids = [p.id for p in products]
    await state.update_data(last_result_ids=ids)
    await message.answer(
        f"نتیجه جستجو — انتخاب‌شده‌ها: {fa_num(len(data.get('selected_product_ids') or []))}",
        reply_markup=await build_price_selection_markup(ids, set(data.get("selected_product_ids") or [])),
    )


@router.callback_query(F.data.startswith("pricechg:toggle:"))
@guard("products.edit")
async def price_change_toggle(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    data = await state.get_data()
    selected = set(data.get("selected_product_ids") or [])
    if pid in selected:
        selected.remove(pid)
    else:
        selected.add(pid)
    await state.update_data(selected_product_ids=sorted(selected))
    ids = list(data.get("last_result_ids") or [])
    try:
        await cb.message.edit_reply_markup(reply_markup=await build_price_selection_markup(ids, selected))
    except Exception:
        pass
    await cb.answer(f"{len(selected)} محصول انتخاب شده")


@router.callback_query(F.data == "pricechg:more")
@guard("products.edit")
async def price_change_more(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PriceChangeFlow.search)
    await cb.message.answer("عبارت بعدی را برای جستجوی محصول بفرستید.")
    await cb.answer()


@router.callback_query(F.data == "pricechg:selected_done")
@guard("products.edit")
async def price_change_selected_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ids = list(data.get("selected_product_ids") or [])
    if not ids:
        return await cb.answer("حداقل یک محصول انتخاب کنید.", show_alert=True)
    await after_scope_selected(cb.message, state, ids)
    await cb.answer()


@router.callback_query(F.data == "pricechg:scope:category")
@guard("products.edit")
async def price_change_category_scope(cb: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        cats = (await session.execute(select(Category).where(Category.is_active.is_(True)).order_by(Category.name))).scalars().all()
    kb = [[InlineKeyboardButton(text=c.name, callback_data=f"pricechg:category:{c.id}")] for c in cats]
    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="pricechg:cancel")])
    await cb.message.answer("دسته‌بندی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("pricechg:category:"))
@guard("products.edit")
async def price_change_category_pick(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        ids = (await session.execute(select(Product.id).where(
            Product.is_active.is_(True), Product.category_id.in_(await descendant_ids(session, cid))
        ))).scalars().all()
    if not ids:
        return await cb.answer("در این دسته محصول فعالی نیست.", show_alert=True)
    await after_scope_selected(cb.message, state, ids)
    await cb.answer()


@router.callback_query(F.data == "pricechg:scope:all")
@guard("products.edit")
async def price_change_all_scope(cb: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        ids = (await session.execute(select(Product.id).where(Product.is_active.is_(True)))).scalars().all()
    if not ids:
        return await cb.answer("محصول فعالی وجود ندارد.", show_alert=True)
    await after_scope_selected(cb.message, state, ids)
    await cb.answer()


@router.callback_query(F.data.startswith("pricechg:field:"))
@guard("products.edit")
async def price_change_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await state.update_data(price_change_field=field)
    data = await state.get_data()
    mode = data.get("price_change_mode")
    await state.set_state(PriceChangeFlow.value)
    if mode == "percent":
        text = "درصد تغییر را وارد کنید. برای افزایش ۱۰٪ عدد ۱۰ و برای کاهش ۵٪ عدد -۵ را بفرستید."
    elif mode == "amount":
        text = "مبلغ تغییر را به تومان وارد کنید. مثال: ۵۰۰۰۰ برای افزایش یا -۲۰۰۰۰ برای کاهش."
    else:
        text = "قیمت دقیق جدید را به تومان وارد کنید."
    await cb.message.answer(text)
    await cb.answer()


def calculate_changed_price(old: Decimal, mode: str, value: Decimal) -> Decimal:
    if mode == "percent":
        result = old * (Decimal("1") + (value / Decimal("100")))
    elif mode == "amount":
        result = old + value
    else:
        result = value
    if result < 0:
        result = Decimal("0")
    return result.quantize(Decimal("1"))


def selected_price_types(field: str):
    return {
        "purchase": ["purchase"],
        "retail": ["retail"],
        "wholesale": ["wholesale"],
        "both_sales": ["retail", "wholesale"],
        "all": ["purchase", "retail", "wholesale"],
    }[field]


@router.message(PriceChangeFlow.value)
async def price_change_value(message: Message, state: FSMContext):
    try:
        value = Decimal(normalize_digits(message.text or "").replace(",", "").replace("٬", "").strip())
    except Exception:
        return await message.answer("عدد معتبر وارد کنید.")
    data = await state.get_data()
    mode = data["price_change_mode"]
    if mode == "exact" and value < 0:
        return await message.answer("قیمت دقیق نمی‌تواند منفی باشد.")
    ids = list(data.get("selected_product_ids") or [])
    price_types = selected_price_types(data["price_change_field"])
    async with SessionLocal() as session:
        products = (await session.execute(select(Product).where(Product.id.in_(ids)))).scalars().all()
    lines = []
    for p in products[:8]:
        pieces = []
        for pt in price_types:
            old = Decimal({"purchase":p.purchase_price,"retail":p.base_price,"wholesale":p.wholesale_price}[pt] or 0)
            new = calculate_changed_price(old, mode, value)
            title = {"purchase":"پایه","retail":"خرده","wholesale":"عمده"}[pt]
            pieces.append(f"{title}: {fa_num(old)} ← {fa_num(new)}")
        lines.append(f"• {p.name}\n  " + " | ".join(pieces))
    await state.update_data(price_change_value=str(value))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ اعمال تغییر", callback_data="pricechg:confirm")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="pricechg:cancel")],
    ])
    if data.get("price_change_quick"):
        await message.answer("ℹ️ قیمت پایه، خرده و عمده همگی تغییر می‌کنند.")
    await message.answer(
        f"پیش‌نمایش تغییر روی {fa_num(len(products))} محصول:\n\n" + "\n".join(lines) +
        ("\n\n… و محصولات دیگر" if len(products) > 8 else ""),
        reply_markup=kb,
    )


@router.callback_query(F.data == "pricechg:confirm")
@guard("products.edit")
async def price_change_confirm(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.edit"):
        return
    data = await state.get_data()
    ids = list(data.get("selected_product_ids") or [])
    if not ids:
        return await cb.answer("محصولی انتخاب نشده.", show_alert=True)
    mode = data["price_change_mode"]
    value = Decimal(data["price_change_value"])
    price_types = selected_price_types(data["price_change_field"])
    changed = 0
    sync_ids = []
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        products = (await session.execute(select(Product).where(Product.id.in_(ids)))).scalars().all()
        for p in products:
            product_changed = False
            for pt in price_types:
                old = Decimal({"purchase":p.purchase_price,"retail":p.base_price,"wholesale":p.wholesale_price}[pt] or 0)
                new = calculate_changed_price(old, mode, value)
                if old != new:
                    await set_product_price(session, p, new, actor.id if actor else None, f"bulk_{mode}", pt)
                    changed += 1
                    product_changed = True
            if product_changed and any(x in price_types for x in ("retail", "wholesale")):
                sync_ids.append(p.id)
        await add_audit(session, actor.id if actor else None, "price.bulk_change", "products", None, {
            "product_count": len(products), "field": data["price_change_field"], "mode": mode, "value": str(value)
        })
        await session.commit()
    for pid in sync_ids:
        try:
            await sync_product_post(cb.bot, pid)
        except Exception:
            pass
    await state.clear()
    await cb.message.answer(
        f"✅ تغییر قیمت انجام شد.\nمحصولات هدف: {fa_num(len(ids))}\nتعداد قیمت‌های تغییرکرده: {fa_num(changed)}"
    )
    await cb.answer()


@router.callback_query(F.data == "admin:noop_cancel")
async def admin_noop_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("عملیات لغو شد.")
    await cb.answer()


@router.callback_query(F.data == "pricechg:cancel")
@guard("products.edit")
async def price_change_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("تغییر قیمت لغو شد.")
    await cb.answer()


# ===== V2.2 GAP COMPLETION HANDLERS =====

@router.callback_query(F.data == "admin:tiers")
@guard("products.edit")
async def tier_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.edit"):
        return
    await state.set_state(TierFlow.product)
    await cb.message.answer("نام یا کد محصول برای قیمت پلکانی را ارسال کنید.")
    await cb.answer()

@router.message(TierFlow.product)
async def tier_product(message: Message, state: FSMContext):
    q = message.text.strip()
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).where(
            (Product.name.ilike(f"%{q}%")) | (Product.internal_sku.ilike(f"%{q}%"))
        ).limit(10))).scalars().all()
    if not rows:
        return await message.answer("محصولی پیدا نشد.")
    kb = [[InlineKeyboardButton(text=f"{p.internal_sku} | {p.name}", callback_data=f"tier:select:{p.id}")] for p in rows]
    await message.answer("محصول را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("tier:select:"))
@guard("products.edit")
async def tier_select(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        p = await session.get(Product, pid)
        tiers = (await session.execute(select(PriceTier).where(PriceTier.product_id == pid).order_by(PriceTier.min_qty))).scalars().all()
    await state.update_data(product_id=pid)
    text = (f"قیمت پایه: {fa_num(p.purchase_price)} تومان\n"
            f"فروش خرده: {fa_num(p.base_price)} تومان\n"
            f"فروش عمده: {fa_num(p.wholesale_price)} تومان")
    if tiers:
        text += "\n\nپلکان‌های فعلی:\n" + "\n".join(
            f"{t.min_qty} تا {t.max_qty or '+'}: {fa_num(t.unit_price)} تومان" for t in tiers
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن پلکان", callback_data="tier:add")],
        [InlineKeyboardButton(text="🗑 پاک کردن همه پلکان‌ها", callback_data=f"tier:clear:{pid}")],
    ])
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "tier:add")
@guard("products.edit")
async def tier_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TierFlow.min_qty)
    await cb.message.answer("حداقل تعداد این پلکان را وارد کنید.")
    await cb.answer()

@router.message(TierFlow.min_qty)
async def tier_min(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return await message.answer("عدد معتبر وارد کنید.")
    await state.update_data(min_qty=int(raw))
    await state.set_state(TierFlow.max_qty)
    await message.answer("حداکثر تعداد را وارد کنید؛ برای نامحدود ۰ بفرستید.")

@router.message(TierFlow.max_qty)
async def tier_max(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("عدد معتبر وارد کنید.")
    await state.update_data(max_qty=(None if int(raw)==0 else int(raw)))
    await state.set_state(TierFlow.price)
    await message.answer("قیمت واحد این پلکان را به تومان وارد کنید.")

@router.message(TierFlow.price)
async def tier_price(message: Message, state: FSMContext):
    raw = normalize_digits(message.text or "").replace(",","").replace("٬","").strip()
    if not raw.isdigit():
        return await message.answer("قیمت معتبر به تومان وارد کنید.")
    data = await state.get_data()
    async with SessionLocal() as session:
        session.add(PriceTier(product_id=data["product_id"], min_qty=data["min_qty"], max_qty=data["max_qty"], unit_price=int(raw)))
        await session.commit()
    await state.clear()
    await message.answer("✅ پلکان قیمت ثبت شد.")

@router.callback_query(F.data.startswith("tier:clear:"))
@guard("products.edit")
async def tier_clear(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        rows = (await session.execute(select(PriceTier).where(PriceTier.product_id == pid))).scalars().all()
        for r in rows:
            await session.delete(r)
        await session.commit()
    await cb.message.answer("✅ همه پلکان‌های قیمت حذف شدند.")
    await cb.answer()

@router.callback_query(F.data == "admin:delete_product")
@guard("products.delete")
async def delete_product_start(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "products.delete"):
        return
    await state.set_state(DeleteProductFlow.query)
    await cb.message.answer("نام یا کد محصولی را که می‌خواهید حذف کنید ارسال کنید.")
    await cb.answer()


@router.message(DeleteProductFlow.query)
async def delete_product_query(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    async with SessionLocal() as session:
        products = (await session.execute(select(Product).where(
            (Product.name.ilike(f"%{q}%")) | (Product.internal_sku.ilike(f"%{q}%"))
        ).limit(15))).scalars().all()
    if not products:
        return await message.answer("محصولی پیدا نشد.")
    kb = [[InlineKeyboardButton(
        text=f"🗑 {p.internal_sku} | {p.name}", callback_data=f"prod:delete:{p.id}"
    )] for p in products]
    await message.answer(
        "محصول را انتخاب کنید.\n"
        "حذف واقعی انجام می‌شود؛ اگر محصول سابقه سفارش داشته باشد، نام/تعداد/قیمت ثبت‌شده در سفارش قبلی به‌صورت Snapshot محفوظ می‌ماند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await state.clear()


@router.callback_query(F.data.startswith("prod:delete:"))
@guard("products.delete")
async def product_delete_confirm(cb: CallbackQuery):
    if not await require(cb, "products.delete"):
        return
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        p = await session.get(Product, pid)
        if not p:
            return await cb.answer("محصول پیدا نشد.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"prod:delete_confirm:{pid}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin:noop_cancel")],
    ])
    await cb.message.answer(f"از حذف «{p.name}» مطمئن هستید؟", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("prod:delete_confirm:"))
@guard("products.delete")
async def product_delete_apply(cb: CallbackQuery):
    if not await require(cb, "products.delete"):
        return
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        p = await session.get(Product, pid)
        if not p:
            return await cb.answer("محصول پیدا نشد.", show_alert=True)
        used_in_orders = await session.scalar(select(func.count(OrderItem.id)).where(OrderItem.product_id == pid))
        # DB migration makes order_items.product_id nullable/SET NULL, so the product
        # can be truly deleted while order snapshot data remains intact.
        await session.delete(p)
        await add_audit(session, actor.id if actor else None, "product.deleted", "product", pid, {
            "historical_order_items": int(used_in_orders or 0)
        })
        await session.commit()
    await cb.message.answer(
        "✅ محصول کامل حذف شد." +
        (" سابقه سفارش‌های قبلی از طریق Snapshot حفظ شده است." if used_in_orders else "")
    )
    await cb.answer()


@router.callback_query(F.data == "admin:orders")
@guard("orders.view")
async def orders_v22(cb: CallbackQuery):
    if not await require(cb, "orders.view"):
        return
    statuses = [
        ("WAITING_PAYMENT","منتظر پرداخت"),("PAYMENT_REVIEW","منتظر تأیید"),
        ("CONFIRMED","تأیید شده"),("PREPARING","در حال آماده‌سازی"),
        ("READY","آماده"),("SHIPPED","ارسال شده"),("DELIVERED","تکمیل شده"),("CANCELLED","لغو شده")
    ]
    kb = [[InlineKeyboardButton(text=title, callback_data=f"orders:status:{code}")] for code,title in statuses]
    await cb.message.answer("🧾 سفارش‌ها - وضعیت را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("orders:status:"))
@guard("orders.view")
async def orders_by_status(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    async with SessionLocal() as session:
        rows = (await session.execute(select(Order).where(Order.status == code).order_by(Order.id.desc()).limit(30))).scalars().all()
    if not rows:
        await cb.message.answer("سفارشی در این وضعیت نیست.")
    else:
        kb = [[InlineKeyboardButton(text=f"{o.order_no} | {fa_num(o.total_amount)}", callback_data=f"order:detail:{o.id}")] for o in rows]
        await cb.message.answer("سفارش‌ها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("order:detail:"))
@guard("orders.view")
async def order_detail(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        o = await session.get(Order, oid)
        items = (await session.execute(select(OrderItem).where(OrderItem.order_id == oid))).scalars().all()
    text = (
        f"سفارش: {o.order_no}\nمشتری: {o.customer_name_snapshot or '-'}\n"
        f"شماره: {o.customer_phone_snapshot or '-'}\nمغازه: {o.shop_name_snapshot or '-'}\n"
        f"جمع: {fa_num(o.total_amount)} تومان\n\n"
        + "\n".join(f"{i.product_name_snapshot} × {i.quantity} = {fa_num(i.line_total)}" for i in items)
    )
    await cb.message.answer(text, reply_markup=order_status_menu(oid))
    await cb.answer()

@router.callback_query(F.data.startswith("orderstatus:"))
@guard("orders.manage")
async def order_status_change(cb: CallbackQuery):
    if not await require(cb, "orders.manage"):
        return
    _, oid, status = cb.data.split(":")
    async with SessionLocal() as session:
        o = await session.get(Order, int(oid))
        user = await session.get(User, o.user_id)
        o.status = status
        session.add(AuditLog(action="order.status", entity_type="order", entity_id=str(o.id), payload={"status":status}))
        await session.commit()
    try:
        await cb.bot.send_message(user.telegram_id, f"وضعیت سفارش {o.order_no} به «{order_status_label(status)}» تغییر کرد.")
    except Exception:
        pass
    await cb.answer("وضعیت تغییر کرد.")

@router.callback_query(F.data == "admin:audit")
@guard("reports.view")
async def audit_view(cb: CallbackQuery):
    if not await require(cb, "reports.view"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(30))).scalars().all()
    text = "\n".join(f"#{x.id} | {x.action} | {x.entity_type or '-'}:{x.entity_id or '-'} | {x.created_at:%Y-%m-%d %H:%M}" for x in rows)
    await cb.message.answer(text or "لاگی ثبت نشده.")
    await cb.answer()

@router.callback_query(F.data == "admin:users")
@guard("users.manage")
async def users_v22(cb: CallbackQuery):
    if not await require(cb, "users.manage"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(select(User).order_by(User.id.desc()).limit(50))).scalars().all()
    kb = []
    for u in rows:
        status = "✅" if u.is_approved and u.is_active else ("⏳" if not u.is_approved else "⛔")
        kind = "عمده" if u.customer_type == WHOLESALE else "خرد"
        kb.append([InlineKeyboardButton(
            text=f"{status} {u.first_name or '-'} | {kind} | {u.shop_name or '-'}",
            callback_data=f"user:detail:{u.id}",
        )])
    await cb.message.answer("👥 کاربران:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("user:detail:"))
@guard("users.manage")
async def user_detail(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        approved_count = await session.scalar(
            select(func.count(Order.id)).where(Order.user_id == uid)
        )
    kind = "عمده" if u.customer_type == WHOLESALE else "خرد"
    lines = [
        f"👤 {u.first_name or ''} {u.last_name or ''}",
        f"نوع حساب: {kind}",
        f"فروشگاه: {u.shop_name or '-'}",
        f"تلفن: {u.phone or '-'}",
    ]
    if u.address:
        lines.append(f"آدرس: {u.address}")
    lines += [
        f"شهر: {u.city or '-'}",
        f"وضعیت: {'✅ تأیید شده' if u.is_approved else '⏳ منتظر تأیید'} / "
        f"{'فعال' if u.is_active else 'غیرفعال'}",
        f"سفارش‌ها: {fa_num(approved_count or 0)}",
    ]
    rows = []
    if not u.is_approved:
        rows.append([InlineKeyboardButton(text="✅ تأیید حساب", callback_data=f"user:approve:{uid}")])
        rows.append([InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"user:reject:{uid}")])
    if u.is_active:
        rows.append([InlineKeyboardButton(text="⛔ غیرفعال کردن", callback_data=f"user:disable:{uid}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ فعال کردن", callback_data=f"user:enable:{uid}")])
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def set_user_approved(bot, uid: int, approved: bool, actor_tg_id: int | None = None):
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u:
            return None, None
        u.is_approved = approved
        u.is_active = True if approved else False
        u.approved_at = datetime.now(timezone.utc) if approved else None
        await add_audit(
            session, None, "user.approve" if approved else "user.reject", "user", uid,
            {"telegram_id": u.telegram_id, "actor_tg_id": actor_tg_id},
        )
        await session.commit()
        await session.refresh(u)
        tg_id = u.telegram_id
    message = (
        "✅ حساب شما تأیید شد.\n\n"
        "از این به بعد قیمت عمده، قیمت خرده و مزایای قیمت پلکانی را می‌بینید.\n"
        "برای شروع /menu را بزنید."
        if approved else
        "❌ درخواست حساب فروشگاهی شما تأیید نشد.\nبرای پیگیری با پشتیبانی تماس بگیرید."
    )
    try:
        await bot.send_message(tg_id, message)
    except Exception:
        pass
    return tg_id


@router.callback_query(F.data.startswith("user:approve:"))
@guard("users.manage")
async def user_approve(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    await set_user_approved(cb.bot, uid, True, cb.from_user.id)
    await cb.answer("تأیید شد.", show_alert=False)
    await cb.message.answer("✅ حساب کاربر تأیید شد و اطلاع‌رسانی انجام شد.")


@router.callback_query(F.data.startswith("user:reject:"))
@guard("users.manage")
async def user_reject(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    await set_user_approved(cb.bot, uid, False, cb.from_user.id)
    await cb.answer("رد شد.")
    await cb.message.answer("❌ درخواست کاربر رد و حساب غیرفعال شد.")


@router.callback_query(F.data.startswith("user:disable:"))
@guard("users.manage")
async def user_disable(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        u.is_active = False
        await session.commit()
    await cb.answer("غیرفعال شد.")
    await cb.message.answer("⛔ حساب کاربر غیرفعال شد.")


@router.callback_query(F.data.startswith("user:enable:"))
@guard("users.manage")
async def user_enable(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        u.is_active = True
        await session.commit()
    await cb.answer("فعال شد.")
    await cb.message.answer("✅ حساب کاربر فعال شد.")


@router.callback_query(F.data.startswith("wsapprove:"))
@guard("users.manage")
async def wholesale_approve(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    await set_user_approved(cb.bot, uid, True, cb.from_user.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("حساب عمده تأیید شد.")
    await cb.message.answer("✅ حساب عمده تأیید شد.")


@router.callback_query(F.data.startswith("wsreject:"))
@guard("users.manage")
async def wholesale_reject(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    await set_user_approved(cb.bot, uid, False, cb.from_user.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("رد شد.")
    await cb.message.answer("❌ درخواست حساب عمده رد شد.")


@router.callback_query(F.data == "admin:admins")
@guard("admins.manage")
async def admins_v22(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    async with SessionLocal() as session:
        manager_ids = (await session.execute(
            select(User.id)
            .join(UserPermission, UserPermission.user_id == User.id)
            .where(
                User.is_active.is_(True),
                User.is_owner.is_(False),
                UserPermission.is_allowed.is_(True),
            )
            .distinct()
        )).scalars().all()
        managers = []
        if manager_ids:
            managers = (await session.execute(
                select(User).where(User.id.in_(manager_ids)).order_by(User.first_name, User.id)
            )).scalars().all()
        active_invites = (await session.execute(
            select(AdminInvite).where(
                AdminInvite.is_active.is_(True),
                AdminInvite.used_at.is_(None),
            ).order_by(AdminInvite.id.desc()).limit(20)
        )).scalars().all()

    rows = [[InlineKeyboardButton(text="➕ افزودن مدیر با لینک اختصاصی", callback_data="admininvite:new")]]
    if managers:
        rows.append([InlineKeyboardButton(text="👥 مدیران فعلی", callback_data="adminmanagers:list")])
    if active_invites:
        rows.append([InlineKeyboardButton(text=f"🔗 دعوت‌های فعال ({len(active_invites)})", callback_data="admininvites:list")])
    rows.append([InlineKeyboardButton(text="⬅️ پنل مدیریت", callback_data="menu:admin")])
    await cb.message.answer(
        "🔐 مدیران و دسترسی‌ها\n\n"
        "برای مدیر جدید، روی «افزودن مدیر با لینک اختصاصی» بزنید، دسترسی‌ها را انتخاب کنید و لینک ساخته‌شده را برای همان شخص بفرستید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


def _invite_permissions_markup(perms, selected_codes):
    selected = set(selected_codes or [])
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if p.code in selected else '⭕'} {p.title}",
            callback_data=f"admininvite:perm:{p.id}",
        )]
        for p in perms
    ]
    rows.append([InlineKeyboardButton(text="✅ ساخت لینک دعوت", callback_data="admininvite:create")])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="admininvite:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admininvite:new")
@guard("admins.manage")
async def admin_invite_new(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "admins.manage"):
        return
    async with SessionLocal() as session:
        perms = (await session.execute(select(Permission).order_by(Permission.title))).scalars().all()
    await state.clear()
    await state.set_state(AdminInviteFlow.selecting)
    await state.update_data(selected_perm_codes=[])
    await cb.message.answer(
        "➕ افزودن مدیر جدید\n\n"
        "دسترسی‌هایی را که می‌خواهید این مدیر داشته باشد انتخاب کنید.\n"
        "هر گزینه‌ای که ✅ شود، بعد از استفاده از لینک برای آن مدیر فعال خواهد شد.\n\n"
        "در پایان روی «ساخت لینک دعوت» بزنید.",
        reply_markup=_invite_permissions_markup(perms, []),
    )
    await cb.answer()


@router.callback_query(AdminInviteFlow.selecting, F.data.startswith("admininvite:perm:"))
@guard("admins.manage")
async def admin_invite_toggle_permission(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "admins.manage"):
        return
    pid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        perm = await session.get(Permission, pid)
        perms = (await session.execute(select(Permission).order_by(Permission.title))).scalars().all()
    if not perm:
        return await cb.answer("دسترسی پیدا نشد.", show_alert=True)
    data = await state.get_data()
    selected = set(data.get("selected_perm_codes") or [])
    if perm.code in selected:
        selected.remove(perm.code)
    else:
        selected.add(perm.code)
    await state.update_data(selected_perm_codes=sorted(selected))
    await cb.message.edit_reply_markup(reply_markup=_invite_permissions_markup(perms, selected))
    await cb.answer("انتخاب شد." if perm.code in selected else "برداشته شد.")


def _expand_permission_dependencies(codes: set[str]) -> set[str]:
    result = set(codes)
    if result & {"products.create", "products.edit", "products.delete"}:
        result.add("products.view")
    if "orders.manage" in result:
        result.add("orders.view")
    if "payments.approve" in result:
        result.add("payments.view")
    return result


@router.callback_query(AdminInviteFlow.selecting, F.data == "admininvite:create")
@guard("admins.manage")
async def admin_invite_create(cb: CallbackQuery, state: FSMContext):
    if not await require(cb, "admins.manage"):
        return
    data = await state.get_data()
    selected = _expand_permission_dependencies(set(data.get("selected_perm_codes") or []))
    if not selected:
        return await cb.answer("حداقل یک دسترسی انتخاب کنید.", show_alert=True)

    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        perms = (await session.execute(
            select(Permission).where(Permission.code.in_(sorted(selected))).order_by(Permission.title)
        )).scalars().all()
        invite = AdminInvite(
            token_hash=token_hash,
            permissions=sorted(selected),
            created_by_user_id=actor.id if actor else None,
            expires_at=expires_at,
            is_active=True,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)

    me = await cb.bot.get_me()
    invite_url = f"https://t.me/{me.username}?start=adm_{token}"
    titles = "\n".join(f"• {p.title}" for p in perms)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 باز کردن لینک دعوت", url=invite_url)],
        [InlineKeyboardButton(text="🔐 مدیران و دسترسی‌ها", callback_data="admin:admins")],
    ])
    await state.clear()
    await cb.message.answer(
        "✅ لینک اختصاصی مدیر ساخته شد.\n\n"
        "این لینک را فقط برای همان شخص بفرستید.\n"
        "لینک یک‌بار مصرف است و ۷ روز اعتبار دارد.\n\n"
        f"دسترسی‌های مدیر:\n{titles}\n\n"
        f"لینک:\n{invite_url}\n\n"
        "وقتی شخص روی لینک بزند، بدون نیاز به ارسال Telegram ID، به‌عنوان مدیر ثبت می‌شود و فقط همین دسترسی‌ها را می‌گیرد.",
        reply_markup=kb,
    )
    await cb.answer("لینک ساخته شد.")


@router.callback_query(AdminInviteFlow.selecting, F.data == "admininvite:cancel")
@guard("admins.manage")
async def admin_invite_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("ساخت لینک دعوت لغو شد.")
    await cb.answer()


@router.callback_query(F.data == "admininvites:list")
@guard("admins.manage")
async def admin_invites_list(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        invites = (await session.execute(
            select(AdminInvite).where(
                AdminInvite.is_active.is_(True),
                AdminInvite.used_at.is_(None),
            ).order_by(AdminInvite.id.desc()).limit(30)
        )).scalars().all()
    rows = []
    for inv in invites:
        exp = inv.expires_at
        expired = exp < (now if exp and exp.tzinfo else now.replace(tzinfo=None)) if exp else False
        if not expired:
            rows.append([InlineKeyboardButton(
                text=f"❌ لغو دعوت #{inv.id}",
                callback_data=f"admininvite:revoke:{inv.id}",
            )])
    rows.append([InlineKeyboardButton(text="⬅️ مدیران و دسترسی‌ها", callback_data="admin:admins")])
    await cb.message.answer(
        "🔗 دعوت‌های فعال\n\nهر دعوت یک‌بار مصرف است. اگر لینک را اشتباهی فرستادید، از همین‌جا لغوش کنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admininvite:revoke:"))
@guard("admins.manage")
async def admin_invite_revoke(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    invite_id = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        inv = await session.get(AdminInvite, invite_id)
        if inv and inv.used_at is None:
            inv.is_active = False
            await session.commit()
    await cb.answer("دعوت لغو شد.", show_alert=True)
    await cb.message.answer("✅ لینک دعوت دیگر قابل استفاده نیست.")


@router.callback_query(F.data == "adminmanagers:list")
@guard("admins.manage")
async def admin_managers_list(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    async with SessionLocal() as session:
        manager_ids = (await session.execute(
            select(User.id)
            .join(UserPermission, UserPermission.user_id == User.id)
            .where(
                User.is_active.is_(True),
                User.is_owner.is_(False),
                UserPermission.is_allowed.is_(True),
            ).distinct()
        )).scalars().all()
        managers = []
        if manager_ids:
            managers = (await session.execute(
                select(User).where(User.id.in_(manager_ids)).order_by(User.first_name, User.id)
            )).scalars().all()
    rows = [[InlineKeyboardButton(
        text=f"👤 {u.first_name or ''} {u.last_name or ''} | {u.telegram_id}",
        callback_data=f"permuser:{u.id}",
    )] for u in managers]
    rows.append([InlineKeyboardButton(text="⬅️ مدیران و دسترسی‌ها", callback_data="admin:admins")])
    await cb.message.answer(
        "👥 مدیران فعلی:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


async def _manager_permission_markup(session, uid: int):
    perms = (await session.execute(select(Permission).order_by(Permission.title))).scalars().all()
    rows = []
    for p in perms:
        up = await session.scalar(select(UserPermission).where(
            UserPermission.user_id == uid,
            UserPermission.permission_id == p.id,
        ))
        allowed = bool(up and up.is_allowed)
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if allowed else '⭕'} {p.title}",
            callback_data=f"permtoggle:{uid}:{p.id}",
        )])
    rows.append([InlineKeyboardButton(text="⛔ حذف دسترسی مدیریتی", callback_data=f"manager:revoke:{uid}")])
    rows.append([InlineKeyboardButton(text="⬅️ مدیران فعلی", callback_data="adminmanagers:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("permuser:"))
@guard("admins.manage")
async def perm_user(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    uid = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u or u.is_owner:
            return await cb.answer("مدیر پیدا نشد.", show_alert=True)
        markup = await _manager_permission_markup(session, uid)
    await cb.message.answer(
        f"دسترسی‌های مدیر:\n{u.first_name or ''} {u.last_name or ''}\nTelegram ID: {u.telegram_id}\n\n"
        "برای روشن/خاموش کردن هر دسترسی روی همان دکمه بزنید.",
        reply_markup=markup,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("permtoggle:"))
@guard("admins.manage")
async def perm_toggle(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    _, uid, pid = cb.data.split(":")
    uid, pid = int(uid), int(pid)
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u or u.is_owner:
            return await cb.answer("این دسترسی قابل تغییر نیست.", show_alert=True)
        row = await session.scalar(select(UserPermission).where(
            UserPermission.user_id == uid,
            UserPermission.permission_id == pid,
        ))
        if row:
            row.is_allowed = not row.is_allowed
        else:
            session.add(UserPermission(user_id=uid, permission_id=pid, is_allowed=True))
        await session.commit()
        markup = await _manager_permission_markup(session, uid)
        any_allowed = await session.scalar(select(UserPermission.id).where(
            UserPermission.user_id == uid,
            UserPermission.is_allowed.is_(True),
        ).limit(1))
    if any_allowed:
        await cb.bot.set_my_commands(
            [
                BotCommand(command="start", description="شروع"),
                BotCommand(command="menu", description="منوی اصلی"),
                BotCommand(command="products", description="محصولات"),
                BotCommand(command="cart", description="سبد خرید"),
                BotCommand(command="orders", description="سفارش‌های من"),
                BotCommand(command="price", description="لیست قیمت"),
                BotCommand(command="admin", description="پنل مدیریت"),
            ],
            scope=BotCommandScopeChat(chat_id=u.telegram_id),
        )
    await cb.message.edit_reply_markup(reply_markup=markup)
    await cb.answer("دسترسی تغییر کرد.")


@router.callback_query(F.data.startswith("manager:revoke:"))
@guard("admins.manage")
async def manager_revoke(cb: CallbackQuery):
    if not await require(cb, "admins.manage"):
        return
    uid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        u = await session.get(User, uid)
        if not u or u.is_owner:
            return await cb.answer("این عملیات مجاز نیست.", show_alert=True)
        rows = (await session.execute(select(UserPermission).where(UserPermission.user_id == uid))).scalars().all()
        for row in rows:
            row.is_allowed = False
        await session.commit()
    await cb.bot.set_my_commands(
        [
            BotCommand(command="start", description="شروع"),
            BotCommand(command="menu", description="منوی اصلی"),
            BotCommand(command="products", description="محصولات"),
            BotCommand(command="cart", description="سبد خرید"),
            BotCommand(command="orders", description="سفارش‌های من"),
            BotCommand(command="price", description="لیست قیمت"),
        ],
        scope=BotCommandScopeChat(chat_id=u.telegram_id),
    )
    try:
        await cb.bot.send_message(u.telegram_id, "⛔ دسترسی مدیریتی شما توسط مدیریت سیستم غیرفعال شد.")
    except Exception:
        pass
    await cb.message.answer("✅ همه دسترسی‌های مدیریتی این کاربر غیرفعال شد.")
    await cb.answer()

@router.callback_query(F.data == "admin:system_settings")
@guard("settings.manage")
async def sys_settings(cb: CallbackQuery):
    if not await require(cb, "settings.manage"):
        return
    async with SessionLocal() as session:
        reg = await session.get(SystemSetting, "registration_requires_approval")
        support = await session.get(SystemSetting, "support_text")
        notify = await session.get(SystemSetting, "notify_price_changes")
        footer = await setting_value(session, FOOTER_KEY, "")
        auto_publish = await setting_value(session, AUTO_PUBLISH_KEY, "1")
        active_channel = await session.scalar(
            select(PublishChannel).where(PublishChannel.is_active.is_(True))
        )
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
    rows = [
        [InlineKeyboardButton(text=f"{'✅' if reg and reg.value=='1' else '⭕'} تأیید اجباری ثبت‌نام", callback_data="sys:toggle:registration_requires_approval")],
        [InlineKeyboardButton(text=f"{'✅' if notify and notify.value=='1' else '⭕'} اعلان تغییر قیمت", callback_data="sys:toggle:notify_price_changes")],
        [InlineKeyboardButton(text="✏️ متن پشتیبانی", callback_data="sys:support")],
        [InlineKeyboardButton(
            text="📡 اتصال به کانال" + (f" (✅ {active_channel.title})" if active_channel else " (⚠️ متصل نیست)"),
            callback_data="admin:channel_settings",
        )],
        [InlineKeyboardButton(text="✏️ فوتر پیام کانال", callback_data="channel:footer")],
        [InlineKeyboardButton(
            text=f"{'✅' if auto_publish == '1' else '⭕'} ارسال خودکار محصول جدید به کانال",
            callback_data="channel:toggle_auto",
        )],
    ]
    if actor and actor.is_owner:
        rows.append([InlineKeyboardButton(text="🧨 ریست محصولات", callback_data="sys:reset_products")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.answer(
        f"🛠 تنظیمات سیستم\n{short_version()}\n\n"
        f"متن پشتیبانی فعلی:\n{support.value if support else '-'}\n\n"
        f"فوتر پیام کانال:\n{footer or '— (خالی)'}",
        reply_markup=kb,
    )
    await cb.answer()

@router.callback_query(F.data == "sys:reset_products")
@guard("settings.manage")
async def system_reset_products_start(cb: CallbackQuery):
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        count = await session.scalar(select(func.count(Product.id)).where(Product.is_active.is_(True)))
    if not actor or not actor.is_owner:
        return await cb.answer("این گزینه فقط برای Owner است.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧨 بله، محصولات ریست شوند", callback_data="sys:reset_products_confirm")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin:noop_cancel")],
    ])
    await cb.message.answer(
        f"⚠️ ریست محصولات\n\nمحصول فعال فعلی: {fa_num(count)}\n\n"
        "این عملیات همه محصولات را کامل حذف می‌کند. سابقه سفارش‌های قبلی پاک نمی‌شود و Snapshot هر سفارش باقی می‌ماند. شمارنده کد محصول نیز از اول شروع می‌شود.\n\n"
        "ادامه می‌دهید؟",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "sys:reset_products_confirm")
@guard("settings.manage")
async def system_reset_products_confirm(cb: CallbackQuery):
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
        if not actor or not actor.is_owner:
            return await cb.answer("این گزینه فقط برای Owner است.", show_alert=True)
        products = (await session.execute(select(Product))).scalars().all()
        deleted_count = len(products)
        historical_items = await session.scalar(select(func.count(OrderItem.id)).where(OrderItem.product_id.is_not(None)))
        for product in products:
            await session.delete(product)
        await session.flush()
        # Reset product identity so the next generated SKU starts from PRD-100001 again.
        await session.execute(text("SELECT setval(pg_get_serial_sequence('products','id'), 1, false)"))
        await add_audit(session, actor.id, "products.reset", "products", None, {
            "deleted": deleted_count, "preserved_order_items": int(historical_items or 0)
        })
        await session.commit()
    await cb.message.answer(
        f"✅ ریست کامل محصولات انجام شد.\nمحصول حذف‌شده: {fa_num(deleted_count)}\n"
        f"ردیف‌های تاریخی سفارش حفظ‌شده: {fa_num(historical_items or 0)}\n"
        "کد محصول بعدی دوباره از PRD-100001 شروع می‌شود."
    )
    await cb.answer()


@router.callback_query(F.data.startswith("sys:toggle:"))
@guard("settings.manage")
async def sys_toggle(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    async with SessionLocal() as session:
        row = await session.get(SystemSetting, key)
        if not row:
            row = SystemSetting(key=key, value="1")
            session.add(row)
        else:
            row.value = "0" if row.value == "1" else "1"
        await session.commit()
    await cb.answer("تنظیم تغییر کرد.")

@router.callback_query(F.data == "sys:support")
@guard("settings.manage")
async def sys_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SystemSettingsFlow.support)
    await cb.message.answer("متن جدید پشتیبانی را ارسال کنید.")
    await cb.answer()

@router.message(SystemSettingsFlow.support)
async def sys_support_save(message: Message, state: FSMContext):
    async with SessionLocal() as session:
        row = await session.get(SystemSetting, "support_text")
        if not row:
            session.add(SystemSetting(key="support_text", value=message.text.strip()))
        else:
            row.value = message.text.strip()
        await session.commit()
    await state.clear()
    await message.answer("✅ متن پشتیبانی ذخیره شد.")

@router.callback_query(F.data == "admin:reports")
@guard("reports.view")
async def reports_v22(cb: CallbackQuery):
    if not await require(cb, "reports.view"):
        return
    from datetime import datetime
    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    month_start = datetime(now.year, now.month, 1)
    async with SessionLocal() as session:
        total_products = await session.scalar(select(func.count(Product.id)))
        total_users = await session.scalar(select(func.count(User.id)))
        today_orders = await session.scalar(select(func.count(Order.id)).where(Order.created_at >= day_start))
        month_orders = await session.scalar(select(func.count(Order.id)).where(Order.created_at >= month_start))
        today_sales = await session.scalar(select(func.coalesce(func.sum(Order.total_amount),0)).where(Order.created_at >= day_start, Order.status.in_(["CONFIRMED","PREPARING","READY","SHIPPED","DELIVERED"])))
        month_sales = await session.scalar(select(func.coalesce(func.sum(Order.total_amount),0)).where(Order.created_at >= month_start, Order.status.in_(["CONFIRMED","PREPARING","READY","SHIPPED","DELIVERED"])))
        avg_order = await session.scalar(select(func.coalesce(func.avg(Order.total_amount),0)).where(Order.status.in_(["CONFIRMED","PREPARING","READY","SHIPPED","DELIVERED"])))
        price_changes = await session.scalar(select(func.count(PriceHistory.id)).where(PriceHistory.created_at >= day_start))
        top = (await session.execute(
            select(OrderItem.product_name_snapshot, func.sum(OrderItem.quantity).label("q"))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status.in_(["CONFIRMED","PREPARING","READY","SHIPPED","DELIVERED"]))
            .group_by(OrderItem.product_name_snapshot).order_by(func.sum(OrderItem.quantity).desc()).limit(5)
        )).all()
    text = (
        f"📊 گزارش مدیریتی\n\nمحصولات: {fa_num(total_products)}\nکاربران: {fa_num(total_users)}\n"
        f"سفارش‌های امروز: {fa_num(today_orders)}\nفروش امروز: {fa_num(today_sales)} تومان\n"
        f"سفارش‌های ماه: {fa_num(month_orders)}\nفروش ماه: {fa_num(month_sales)} تومان\n"
        f"میانگین سفارش: {fa_num(avg_order)} تومان\nتغییرات قیمت امروز: {fa_num(price_changes)}"
    )
    if top:
        text += "\n\n🔥 پرفروش‌ها:\n" + "\n".join(f"{name}: {fa_num(q)} عدد" for name,q in top)
    await cb.message.answer(text)
    await cb.answer()

@router.callback_query(F.data == "admin:settings")
@guard("settings.manage")
async def bank_settings_v22(cb: CallbackQuery):
    if not await require(cb, "settings.manage"):
        return
    async with SessionLocal() as session:
        rows = (await session.execute(select(BankAccount).order_by(BankAccount.id))).scalars().all()
    kb = []
    for x in rows:
        kb.append([InlineKeyboardButton(text=f"{'✅' if x.is_active else '⭕'} {x.title}", callback_data=f"bank:toggle:{x.id}")])
        kb.append([
            InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"bank:edit:{x.id}"),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"bank:delete:{x.id}")
        ])
    kb.append([InlineKeyboardButton(text="➕ افزودن حساب", callback_data="bank:add")])
    await cb.message.answer("⚙️ حساب‌های بانکی", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("bank:delete:"))
async def bank_delete(cb: CallbackQuery):
    async with SessionLocal() as session:
        actor = await session.scalar(select(User).where(User.telegram_id == cb.from_user.id))
    if not actor or not actor.is_owner:
        return await cb.answer("حذف حساب فقط برای Owner مجاز است.", show_alert=True)
    bid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        b = await session.get(BankAccount, bid)
        if b:
            await session.delete(b)
            await session.commit()
    await cb.answer("حذف شد.")

@router.callback_query(F.data.startswith("bank:edit:"))
@guard("settings.manage")
async def bank_edit(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[2])
    await state.update_data(bank_id=bid)
    await state.set_state(BankEditFlow.value)
    await cb.message.answer("مقادیر را با این قالب بفرستید:\nعنوان | شماره کارت | نام صاحب حساب | شبا")
    await cb.answer()

@router.message(BankEditFlow.value)
async def bank_edit_save(message: Message, state: FSMContext):
    parts = [x.strip() for x in message.text.split("|")]
    if len(parts) != 4:
        return await message.answer("قالب صحیح نیست.")
    data = await state.get_data()
    async with SessionLocal() as session:
        b = await session.get(BankAccount, data["bank_id"])
        b.title, b.card_number, b.holder_name, b.iban = parts
        await session.commit()
    await state.clear()
    await message.answer("✅ حساب ویرایش شد.")


@router.callback_query(F.data.in_({"editmeta:category", "editmeta:brand"}))
@guard("products.edit")
async def edit_metadata_menu(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    async with SessionLocal() as session:
        if kind == "category":
            options = [(c.id, category_label(c.name, depth)) for c, depth in await category_tree(session, only_active=True)]
        else:
            options = [(b.id, b.name) for b in (await session.execute(select(Brand).where(Brand.is_active.is_(True)))).scalars()]
    rows = [[InlineKeyboardButton(text=name, callback_data=f"editmeta:set:{kind}:{oid}")] for oid, name in options[:80]]
    rows.append([InlineKeyboardButton(text="بدون انتخاب", callback_data=f"editmeta:set:{kind}:0")])
    await cb.message.answer("گزینه جدید را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("editmeta:set:"))
@guard("products.edit")
async def edit_metadata_save(cb: CallbackQuery, state: FSMContext):
    _, _, kind, raw_id = cb.data.split(":")
    if kind not in {"category", "brand"}:
        return await cb.answer("درخواست نامعتبر است.", show_alert=True)
    data = await state.get_data()
    async with SessionLocal() as session:
        product = await session.get(Product, data.get("product_id")) if data.get("product_id") else None
        if not product:
            return await cb.answer("دوباره محصول را برای ویرایش انتخاب کنید.", show_alert=True)
        oid = int(raw_id)
        entity = await session.get(Category if kind == "category" else Brand, oid) if oid else None
        if oid and (not entity or not entity.is_active):
            return await cb.answer("گزینه فعال نیست.", show_alert=True)
        setattr(product, f"{kind}_id", oid or None)
        await session.commit()
        pid = product.id
    await sync_product_post(cb.bot, pid)
    await cb.message.answer("✅ تغییر ذخیره شد.")
    await cb.answer()
