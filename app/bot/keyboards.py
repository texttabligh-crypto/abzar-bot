from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def main_menu(is_admin=False, customer_type="retail"):
    rows = [
        [
            InlineKeyboardButton(text="🛍 محصولات", callback_data="menu:products"),
            InlineKeyboardButton(text="🔎 جستجو", callback_data="menu:search"),
        ],
        [
            InlineKeyboardButton(text="🗂 دسته‌بندی‌ها", callback_data="menu:categories"),
            InlineKeyboardButton(text="🏷 برندها", callback_data="menu:brands"),
        ],
        [
            InlineKeyboardButton(text="🧺 سبد خرید", callback_data="menu:cart"),
            InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="menu:orders"),
        ],
        [
            InlineKeyboardButton(text="📄 لیست قیمت روز", callback_data="menu:pricelist"),
            InlineKeyboardButton(text="📈 تغییرات قیمت", callback_data="menu:changes"),
        ],
        [InlineKeyboardButton(text="☎️ پشتیبانی", callback_data="menu:support")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_menu(allowed_codes=None, is_owner=False):
    allowed = set(allowed_codes or [])

    def can(*codes):
        return is_owner or any(code in allowed for code in codes)

    buttons = []
    candidates = [
        ("➕ افزودن محصول", "admin:add_product", ("products.create",)),
        ("✏️ ویرایش محصول", "admin:edit_product", ("products.edit",)),
        ("💰 تغییر قیمت", "admin:price_change", ("products.edit",)),
        ("🗑 حذف محصول", "admin:delete_product", ("products.delete",)),
        ("🗂 برندها", "admin:brands", ("products.edit",)),
        ("🗂 دسته‌بندی‌ها", "admin:categories", ("products.edit",)),
        ("🏭 تأمین‌کننده‌ها", "admin:suppliers", ("products.edit",)),
        ("📥 ورود Excel", "admin:excel", ("excel.import",)),
        ("📣 ارسال به کانال", "admin:publish", ("channel.publish",)),
        ("📡 اتصال به کانال", "admin:channel_settings", ("channel.manage",)),
        ("🧾 سفارش‌ها", "admin:orders", ("orders.view", "orders.manage")),
        ("💳 فیش‌های پرداخت", "admin:payments", ("payments.view", "payments.approve")),
        ("👥 کاربران", "admin:users", ("users.manage",)),
        ("🔐 مدیران و دسترسی‌ها", "admin:admins", ("admins.manage",)),
        ("📋 لاگ فعالیت", "admin:audit", ("reports.view",)),
        ("📊 گزارش‌ها", "admin:reports", ("reports.view",)),
        ("⚙️ تنظیمات پرداخت", "admin:settings", ("settings.manage",)),
        ("🛠 تنظیمات سیستم", "admin:system_settings", ("settings.manage",)),
    ]

    visible = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb, codes in candidates if can(*codes)]
    for i in range(0, len(visible), 2):
        buttons.append(visible[i:i+2])
    buttons.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def request_phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ارسال شماره من", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def customer_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 خرید خرد", callback_data="ctype:retail")],
        [InlineKeyboardButton(text="📦 خرید عمده (فروشگاهی)", callback_data="ctype:wholesale")],
    ])

def price_list_formats():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📗 Excel", callback_data="pricefmt:xlsx"),
        InlineKeyboardButton(text="📕 PDF", callback_data="pricefmt:pdf"),
    ]])

def channel_publish_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 ارسال یک محصول", callback_data="publish:single")],
        [InlineKeyboardButton(text="🏷 ارسال بر اساس برند", callback_data="publish:brand")],
        [InlineKeyboardButton(text="💰 ارسال بر اساس بازه قیمت", callback_data="publish:price")],
        [InlineKeyboardButton(text="📦 ارسال همه محصولات فعال", callback_data="publish:all")],
        [InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")],
    ])

def channel_settings_menu(channels):
    rows = []
    for c in channels:
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if c.is_active else '⭕'} {c.title} | {c.chat_ref}",
            callback_data=f"channel:toggle:{c.id}",
        )])
    rows.append([InlineKeyboardButton(text="➕ اتصال / تغییر کانال", callback_data="channel:add")])
    rows.append([InlineKeyboardButton(text="✏️ ویرایش فوتر پیام‌های کانال", callback_data="channel:footer")])
    rows.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def yes_no(prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton(text="❌ خیر", callback_data=f"{prefix}:no"),
    ]])

def brand_picker(brands, prefix="addpbrand"):
    rows = [[InlineKeyboardButton(text=b.name, callback_data=f"{prefix}:{b.id}")] for b in brands]
    rows.append([InlineKeyboardButton(text="⬅️ بدون برند", callback_data=f"{prefix}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def category_picker(tree_rows, prefix="addpcat"):
    """tree_rows: [(category, depth), ...] flattened tree."""
    rows = []
    for cat, depth in tree_rows:
        label = ("└ " * depth if depth else "") + cat.name
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{cat.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بدون دسته‌بندی", callback_data=f"{prefix}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def finish_images():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ پایان ارسال عکس‌ها", callback_data="addp:images_done")],
        [InlineKeyboardButton(text="⏭ بدون عکس ادامه بده", callback_data="addp:no_images")],
    ])

def product_list_actions(product_id: int, post_url: str | None):
    rows = []
    if post_url:
        rows.append([InlineKeyboardButton(text="🔗 مشاهده محصول در کانال", url=post_url)])
    rows.append([InlineKeyboardButton(text="🛒 افزودن به سبد", callback_data=f"addcart:{product_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def product_actions(product_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 انتخاب تعداد و افزودن", callback_data=f"addcart:{product_id}")]
    ])

def cart_actions(item_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➖", callback_data=f"cartdec:{item_id}"),
        InlineKeyboardButton(text="➕", callback_data=f"cartinc:{item_id}"),
        InlineKeyboardButton(text="🗑 حذف", callback_data=f"cartdel:{item_id}"),
    ]])

def checkout_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ثبت نهایی سفارش", callback_data="checkout")]
    ])

def payment_review(payment_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید", callback_data=f"payok:{payment_id}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"payreject:{payment_id}"),
    ]])

def reject_reasons(payment_id: int):
    reasons = [
        ("amount","مبلغ اشتباه"),
        ("unreadable","فیش ناخوانا"),
        ("notfound","واریز مشاهده نشد"),
        ("other","سایر"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=f"payreason:{payment_id}:{code}")]
        for code, title in reasons
    ])

def entity_list(prefix: str, rows):
    kb = [[InlineKeyboardButton(text=f"✏️ {row.name}", callback_data=f"{prefix}:edit:{row.id}")] for row in rows]
    kb.append([InlineKeyboardButton(text="➕ افزودن", callback_data=f"{prefix}:add")])
    kb.append([InlineKeyboardButton(text="⬅️ مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def confirm_import(batch_id=None):
    rows = [[
        InlineKeyboardButton(text="✅ اعمال Import", callback_data="import:apply"),
        InlineKeyboardButton(text="❌ لغو", callback_data="import:cancel"),
    ]]
    if batch_id:
        rows.insert(0, [InlineKeyboardButton(text="⚠️ بررسی موارد مشکوک", callback_data=f"import:review:{batch_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def order_status_menu(order_id: int):
    statuses = [
        ("CONFIRMED","تأیید شده"),
        ("PREPARING","در حال آماده‌سازی"),
        ("READY","آماده"),
        ("SHIPPED","ارسال شده"),
        ("DELIVERED","تکمیل شده"),
        ("CANCELLED","لغو شده"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=f"orderstatus:{order_id}:{code}")]
        for code, title in statuses
    ])

def wholesale_review_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید حساب عمده", callback_data=f"wsapprove:{user_id}")],
        [InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"wsreject:{user_id}")],
    ])
