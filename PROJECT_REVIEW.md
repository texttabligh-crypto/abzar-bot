# بررسی صفر تا صد — Abzar Bot (ربات بازرگانی)

> مرجع: کامیت `096bfab` — «Abzar Bot V2.3» — ۳۶ فایل، ۴٬۶۰۳ خط پایتون
> تاریخ بررسی: ۱۴۰۴/۰۸/۰۳ (2026-10-25)
>
> ⚠️ **به‌روزرسانی V2.4:** بخشی از یافته‌های این گزارش اصلاح شده است (از جمله بندهای ۱، ۲، ۴–۱۰، ۱۱، ۱۳، ۲۳، ۲۵، ۲۹، ۳۰، ۳۵، ۳۸، ۴۳). خلاصه‌ی تغییرات در `DELIVERY.md`. بندهای باقی‌مانده در همان فایل، بخش «کارهای باقی‌مانده»، فهرست شده‌اند.

---

## ۰) حکم کلی

| معیار | ارزیابی |
|---|---|
| بلوغ عملکردی | ۷۵٪ — همه‌ی جریان‌های اصلی (کاتالوگ، سبد، سفارش، فیش، اکسل، کانال، دسترسی) وجود دارند |
| کیفیت کد | ۷۰٪ — لایه‌بندی تمیز، اما `admin.py` با ۲٬۳۲۴ خط (۵۰٪ کل پروژه) یک «فایل خدا»ست |
| امنیت | ۴۵٪ — ۴۱ هندلر از ۸۸ هندلر دکمه‌ای بدون بررسی مجوز؛ گیت تأیید مشتری دور زده می‌شود |
| صحت داده | ۵۵٪ — Snapshot مشتری در سفارش نوشته نمی‌شود؛ Import قیمت فروش را با قیمت تأمین‌کننده بازنویسی می‌کند |
| پایداری/عملیات | ۴۰٪ — بدون تست، بدون لاگ، بدون Alembic، FSM در حافظه، بدون error handler |

**جمع‌بندی:** این یک «هسته‌ی V1 دقیقاً همان‌طور که README گفته» نیست؛ خیلی جلوتر از V1 است (V2.3 با لینک دعوت مدیر، تغییر گروهی قیمت، Publisher کانال). اما دقیقاً در همان نقاطی که README گفته بود «قابل تکمیل است»، هنوز تکمیل نشده — و دو-سه مورد آن **قبل از استفاده واقعی کسب‌وکار باید اصلاح شود** (بخش ۶.۱).

---

## ۱) نقشه پروژه

```
main.py                  بوت‌استرپ: init_db → seed → Bot UI → Dispatcher → polling
app/core/config.py       تنظیمات pydantic-settings از .env
app/core/redis.py        کلاینت Redis (فقط برای payload موقت Import)
app/db/base.py           DeclarativeBase
app/db/database.py       engine + async_sessionmaker
app/db/init_db.py        create_all + migrations
app/db/migrate.py        لیست SQL خام برای migration ایمن
app/models/models.py     ۲۵ مدل (کل اسکیمای دیتابیس)
app/services/            ← لایه منطق (به‌جز routerها، تنها جای «کد واقعی»)
    auth.py              بررسی کاربر/مجوز (۵ تابع)
    catalog.py           SKU، قیمت پلکانی، ثبت تاریخچه قیمت
    orders.py            سبد → سفارش + Snapshot آیتم‌ها
    publishing.py        ارسال/همگام‌سازی پست کانال
    excel_import.py      تشخیص سرستون با fuzzy (rapidfuzz)
    export.py            لیست قیمت Excel + PDF فارسی (RTL)
    audit.py             ثبت AuditLog (فقط add، بدون commit)
app/bot/keyboards.py     همه کیبوردهای سراسری
app/bot/setup.py         seed مجوزها/نقش‌ها/Owner + ثبت روترها + ست‌کردن منوها
app/bot/routers/
    common.py    (۲۵۵)  ثبت‌نام، منو، /admin، فعال‌سازی لینک دعوت
    admin.py    (۲۳۲۴)  پنل کامل: محصول، قیمت، اکسل، کانال، سفارش، کاربر، مدیر، تنظیمات
    customer.py  (۳۰۸)  مرور محصول/برند/دسته، جستجو، سبد، سفارش، لیست قیمت
    payments.py  (۱۳۱)  دریافت فیش و تأیید/رد دو مرحله‌ای
    imports.py   (۲۸۳)  Dry-run اکسل مشتری‌محور (تأمین‌کننده → فایل → بررسی → اعمال)
```

نکته معماری: روترها **مستقیماً** با SQLAlchemy حرف می‌زنند؛ فقط بخشی از منطق در `services` است. عملاً `admin.py` هم‌زمان UI، اعتبارسنجی، تراکنش و منطق کسب‌وکار است.

---

## ۲) مدل داده (۲۵ جدول)

**هسته:** `users` → `carts`→`cart_items` → `orders`→`order_items` → `payments`
**کاتالوگ:** `products` ← `brands` / `categories` (درختی) / `suppliers` → `supplier_products` / `supplier_mappings` / `price_tiers` / `price_history` / `product_images`
**انتشار:** `publish_channels` ← `product_channel_posts`
**حاکمیت:** `permissions` ← `user_permissions` ، `admin_invites` ، `audit_logs` ، `system_settings` ، `bank_accounts` ، `import_batches`→`import_rows`
**مرده:** `roles` ، `user_roles` ، `role_permissions` — ساخته و seed می‌شوند ولی **هیچ‌جا استفاده نمی‌شوند**. سیستم مجوز فقط `user_permissions` است.

سه قیمت روی محصول: `purchase_price` (خرید/پایه) ، `base_price` (فروش خرده — نام legacy) ، `wholesale_price` (فروش عمده).

---

## ۳) چرخه اجرا

1. `main.py` → اگر توکن خالی باشد RuntimeError.
2. `init_db()` → `Base.metadata.create_all` + اجرای ۱۸ دستور SQL خام در `migrate.py`.
3. `seed_defaults()` → ۱۵ مجوز، ۳ نقش، Owner از `OWNER_TELEGRAM_ID`، یک حساب بانکی از `.env`، ۴ تنظیم پیش‌فرض.
4. `setup_bot_ui()` → ست‌کردن `/start /menu /products /cart /orders /price` (+ `/admin` برای Owner و هر کسی که یک `user_permissions` فعال دارد).
5. `Dispatcher()` **بدون Storage** → FSM در RAM؛ هر ری‌استارت همه‌ی جریان‌های نیمه‌کاره را پاک می‌کند.
6. `start_polling()` بدون `drop_pending_updates`، بدون error handler، بدون shutdown تمیز.

---

## ۴) جریان‌های کاری

**الف) ثبت‌نام مشتری** — `common.py:130`
`/start` → ساخت `User(is_approved=False)` → نام → فروشگاه → تلفن (دکمه‌ی Contact یا متن) → شهر → اگر تنظیم `registration_requires_approval=1` باشد «در انتظار تأیید»، وگرنه فعال.

**ب) محصول جدید** — `admin.py:188–414`
نام → برند (بله/خیر → انتخاب) → چند عکس → قیمت پایه → خرده → عمده → پلکانی؟ (بله → حلقه‌ی min/max/price با جلوگیری از هم‌پوشانی) → `finalize_new_product` (SKU + گالری + پلکان + Audit).

**ج) سبد و سفارش** — `customer.py` + `orders.py`
جستجو/مرور → «افزودن به سبد» → تعداد → `CartItem` (ادغام با آیتم موجود) → سبد با ±/حذف → `checkout` → `create_order_from_cart` (قیمت واحد از پلکان، وگرنه خرده؛ Snapshot نام/قیمت آیتم‌ها) → نمایش شماره کارت.

**د) فیش و تأیید** — `payments.py`
مشتری عکس می‌فرستد → آخرین سفارش `WAITING_PAYMENT` پیدا می‌شود → `Payment(PENDING)` + وضعیت `PAYMENT_REVIEW` + ارسال فیش برای همه‌ی دارندگان `payments.approve` → تأیید/رد با دلیل آماده → پیام خودکار به مشتری + Audit.

**ه) Import اکسل** — `imports.py`
انتخاب تأمین‌کننده → فایل xlsx (≤۱۵MB) → تشخیص سرستون با fuzzy → دسته‌بندی هر ردیف (Supplier SKU → نام دقیق → شباهت ≥۹۴ → ۷۵–۹۴ «مشکوک» → جدید/تکراری/خطا) → ذخیره‌ی Batch و Rows + payload در Redis (TTL ۱ ساعت) → بررسی موارد مشکوک → `apply` که محصول می‌سازد/قیمت می‌زند و mapping را در `supplier_mappings` ذخیره می‌کند.

**و) انتشار در کانال** — `publishing.py`
تنظیم کانال (فقط یک کانال فعال) → ارسال یک/برند/بازه‌ی قیمت/همه → اگر پست قبلی وجود دارد ابتدا ویرایش می‌شود، وگرنه پست جدید (آلبوم تا ۱۰ عکس) → `post_url` در DB → هر تغییر قیمت خرده/عمده پست را sync می‌کند.

**ز) تغییر گروهی قیمت** — `admin.py:1215–1516`
درصد/مبلغ/قیمت دقیق → دامنه (چند محصول با جستجو و تیک، یک دسته، همه) → انتخاب فیلد (خرده/عمده/هر دو/پایه/هر سه) → پیش‌نمایش ۸ محصول اول → اعمال با `PriceHistory` (`source=bulk_*`) + sync کانال + Audit.

**ح) دسترسی مدیر** — `common.py:50` + `admin.py:1798–2140`
Owner لینک یک‌بارمصرف ۷ روزه با انتخاب دقیق مجوزها می‌سازد (`sha256` در DB، خود توکن ذخیره نمی‌شود — خوب است) → شخص روی لینک می‌زند (`/start adm_<token>`) → مجوزها اعمال و لینک باطل می‌شود. پنل مدیریت هم اجازه‌ی toggle تک‌تک مجوزها و «حذف دسترسی مدیریتی» را دارد.

---

## ۵) مدل دسترسی

- `is_owner` = همه‌چیز (و برخی موارد فقط Owner: حذف حساب بانکی، ریست محصولات).
- در غیر این صورت فقط مجوزهای `user_permissions.is_allowed = True` با کدهای `products.view/create/edit/delete`, `excel.import`, `orders.view/manage`, `payments.view/approve`, `users.manage`, `admins.manage`, `reports.view`, `settings.manage`, `channel.publish/manage`.
- منوی مدیر با `admin_menu(allowed_codes, is_owner)` فیلتر می‌شود؛ `_expand_permission_dependencies` هم وابستگی‌ها را اضافه می‌کند (`products.edit` → `products.view` …).
- **اما** فیلتر دکمه‌ها جای بررسی سمت سرور را نمی‌گیرد — بخش ۶.۳.

---

## ۶) یافته‌ها

### ۶.۱ بحرانی / مسدودکننده (قبل از تحویل به کاربر واقعی اصلاح شود)

1. **`.env.example` همان‌طور که هست اجرا نمی‌شود.**
   `OWNER_TELEGRAM_ID=` خالی + `owner_telegram_id: int | None` در pydantic v2 → `ValidationError: Input should be a valid integer`. تست شد و شکست خورد. کاربر تازه‌کار با یک خطای مبهم روبه‌رو می‌شود. (`app/core/config.py:6`, `.env.example:3`)

2. **Snapshot مشتری در سفارش هرگز نوشته نمی‌شود.**
   `Order(...)` در `app/services/orders.py:24` فقط `order_no/user_id/status/total` را می‌گذارد؛ `customer_name_snapshot`، `customer_phone_snapshot`، `shop_name_snapshot` هیچ‌جا مقدار نمی‌گیرند. نتیجه: در «جزئیات سفارش» ادمین (`admin.py:1710`) و در پیام فیش (`payments.py:65`) همه‌چیز `-` است. با اینکه مدل و migration برایش ساخته شده، عملاً بی‌اثر است.

3. **Import قیمت فروش را با قیمت فایل تأمین‌کننده بازنویسی می‌کند.**
   برای محصول matched: `set_price(...)` که همان `base_price` (فروش خرده) است (`imports.py:224`). برای محصول جدید: `purchase_price = base_price = wholesale_price = price` (`imports.py:237–241`) یعنی **حاشیه سود صفر**. یعنی یک بار آپلود لیست خرید تأمین‌کننده، قیمت فروش همه‌ی مشتری‌ها را به قیمت خرید تغییر می‌دهد.

4. **تأیید خودکار Import برای همه ردیف‌ها؛ هیچ راهی برای رد کردن یک ردیف نیست.**
   `import_apply` هر ردیفی که `DUPLICATE/ERROR` نباشد اعمال می‌کند (`imports.py:209–212`). ردیف‌های `MATCH` و `PRICE_CHANGED` (شباهت ۹۴٪) قابل بررسی نیستند؛ یک تطبیق اشتباه، قیمت محصول اشتباه را عوض می‌کند و **undo هم وجود ندارد**.

5. **گیت تأیید مشتری دور زده می‌شود.**
   `is_approved` فقط در `/start` و `/menu` چک می‌شود. `/products`, `/cart`, `checkout`, جستجو و مرور برند/دسته هیچ بررسی‌ای ندارند (`customer.py`). یک کاربر تأییدنشده می‌تواند سفارش ثبت کند و فیش بفرستد.

6. **۴۱ هندلر از ۸۸ هندلر دکمه‌ای هیچ بررسی مجوزی ندارند** (فهرست کامل در پیوست ب).
   از این‌ها، این‌ها کاملاً مستقل و خطرناک‌اند: `user:approve`, `user:disable`, `user:detail`, `orders:status:`, `order:detail:`, `sys:toggle:`, `sys:support`, `bank:edit:`, `bank:toggle:`, `bank:add`, `brand:add/category:add/supplier:add`, `…:edit:<id>` (غیرفعال‌سازی برند/دسته/تأمین‌کننده), `tier:clear:`, `editp:*`, `pricechg:*`.
   کلیه‌ی هندلرهای `editp:*` و `pricechg:*` عملاً «ویرایش قیمت و پلکان» را بدون مجوز `products.edit` اجرا می‌کنند.

### ۶.۲ باگ‌های عملکردی

7. **کرش با ورودی غیرمتنی.** در ~۹ هندلر `message.text.strip()` بدون محافظ: `admin.py:430, 735, 1002, 1098, 1528, 1617, 2229` و `customer.py:124`. ارسال عکس/استیکر در حالت انتظار متن → `AttributeError` (aiogram فقط لاگ می‌کند، کاربر هیچ پاسخی نمی‌گیرد). همچنین این هندلرها فیلتر `F.text` ندارند، پس با `@router.message(F.photo)` در `payments.py:41` تداخل می‌کنند.

8. **هر عکسِ بی‌صاحب توسط روتر پرداخت بلعیده می‌شود.** `payments.py:41` روی همه‌ی عکس‌ها بدون شرط وضعیت/سفارش می‌نشیند و اگر سفارش `WAITING_PAYMENT` نباشد بی‌صدا `return` می‌کند.

9. **فیش به «آخرین» سفارش منتظر پرداخت می‌چسبد** (`payments.py:47–50`). مشتری با دو سفارش باز، نمی‌فهمد فیش روی کدام ثبت شد؛ و می‌تواند چند فیش تکراری برای یک سفارش بفرستد (چند `Payment` PENDING). تأیید دومی هم مسدود نیست چون فقط `payment.status` چک می‌شود نه `order.status` (`payments.py:83`).

10. **پیام تغییر وضعیت، کد خام انگلیسی می‌فرستد.** `admin.py:1730`: «وضعیت سفارش ORD-100001 به «CONFIRMED» تغییر کرد.» برچسب فارسی موجود است ولی استفاده نشده.

11. **`stock_status` خام («available») هم به مشتری و هم در Excel/PDF نمایش داده می‌شود** (`customer.py:44`, `export.py:48`).

12. **کدهای SKU/شماره سفارش غیراتمیک:** `max(id) + 100001` (`catalog.py:13`, `orders.py:23`). دو ثبت هم‌زمان → `UniqueViolation` و از دست رفتن سفارش/محصول.

13. **پلکان دوم به بعد بدون اعتبارسنجی.** `tier_overlaps` فقط در ساخت محصول جدید استفاده شده؛ مسیر «افزودن پلکان» از پنل (`admin.py:1585`) نه هم‌پوشانی را چک می‌کند نه `max >= min` را.

14. **خرید محصول غیرفعال/حذف‌شده کرش می‌دهد:** `orders.py:35–36` بدون چک `None`؛ `get_price_for_qty` هم به `product.base_price` دست می‌زند.

15. **`send_message` بدون try در تأیید/رد پرداخت** (`payments.py:94, 130`). اگر مشتری ربات را بلاک کرده باشد، بعد از commit استثنا پرتاب می‌شود، `cb.answer()` اجرا نمی‌شود و دکمه‌ی ادمین «هنگ» می‌ماند.

16. **`raw_data` با مقادیر datetime در JSON** (`imports.py:131`) → احتمال `TypeError` در فایل‌هایی که ستون تاریخ دارند. همچنین `price_value` پسوند «تومان»، فاصله و ممیز اعشاری را حذف نمی‌کند (`imports.py:29`) → ردیف‌های سالم «خطا» می‌شوند.

17. **`redis.setex` بعد از commit دیتابیس** (`imports.py:139`): اگر Redis پایین باشد، تحلیل فایل می‌شکند ولی Batch در DB می‌ماند و کاربر هیچ راهی برای ادامه ندارد.

18. **openpyxl در حالت read-only با `ws.cell()`** (`excel_import.py:17–51`) — دسترسی تصادفی به سلول در read-only بسیار کند است و `ws.max_row` ممکن است `None` باشد → فایل بزرگ/خاص = تایم‌اوت.

19. **`publish_many` بدون مدیریت FloodWait** (`publishing.py:177`): «ارسال همه» برای کاتالوگ چند صدتایی به `TelegramRetryAfter` می‌خورد، به‌عنوان «خطا» شمرده می‌شود و ارسال نیمه‌کاره می‌ماند. Caption هم اگر از ۱۰۲۴ کاراکتر بیشتر شود ارسال را می‌شکند.

20. **اگر ویرایش پست کانال شکست بخورد، پست تکراری ساخته می‌شود** و پست قدیمی در کانال می‌ماند (`publishing.py:98–100`).

21. **حذف محصول / ریست محصولات، پست‌های کانال را بی‌صاحب می‌کند.** `product_channel_posts` با CASCADE پاک می‌شود ولی پیام‌ها در تلگرام می‌مانند؛ هیچ‌جا پاک/آرشیو نمی‌شوند. `sys:reset_products_confirm` هم `setval` مخصوص Postgres را بدون try اجرا می‌کند.

22. **`MIGRATIONS[5..7]` در هر بوت اجرا می‌شوند** (`migrate.py:10–13`): هر محصولی که `purchase_price` یا `wholesale_price` آن NULL باشد، در هر ری‌استارت با `base_price` بازنویسی می‌شود.

### ۶.۳ امنیت و مجوزها

23. بدون بررسی مجوز: ۴۱ هندلر (بند ۶). ریشه‌اش این است که تابع `require()` وجود دارد (`admin.py:104`) ولی به‌صورت دستی و ناقص صدا زده می‌شود؛ دکوریتور سراسری وجود ندارد. کلاینت‌های تلگرام سفارشی می‌توانند `callback_data` دلخواه بفرستند، پس پنهان‌کردن دکمه کافی نیست.
24. مسیر قدیمی `admins:add` (`admin.py:835–867`) با گرفتن Telegram ID، **همه‌ی ۱۵ مجوز** را فعال می‌کند. گرچه از منوی جدید (V2.2) قابل دسترسی نیست، هندلرش زنده است.
25. `sys_toggle` و `sys_support` بدون مجوز `settings.manage`؛ `bank_edit` بدون `settings.manage`؛ `bank_delete` فقط Owner (خوب).
26. هیچ middleware‌ای برای «کاربر مجاز» یا rate-limit وجود ندارد؛ هر کاربر ناشناس می‌تواند به همه‌ی هندلرهای عمومی فشار بیاورد.
27. مثبت: توکن دعوت با `sha256` ذخیره می‌شود، یک‌بارمصرف است، `with_for_update()` دارد (Race امن)، `.env` در gitignore است، هیچ پسورد هاردکد نشده، هیچ SQL رشته‌ای با ورودی کاربر ساخته نمی‌شود.

### ۶.۴ داده و منطق کسب‌وکار

28. **`wholesale_price` هیچ‌جا در قیمت‌گذاری استفاده نمی‌شود** — فقط نمایش در ربات/کانال/Excel. سبد همیشه خرده یا پلکان است. یعنی «قیمت عمده» تشریفاتی است.
29. `stock_quantity` و `stock_status` **هیچ مسیری برای نوشتن ندارند**؛ موجودی کم نمی‌شود، سفارش بیش از موجودی ممکن است.
30. `factory_code`, `model`, `unit` هم **هیچ مسیری برای نوشتن ندارند** — با اینکه جستجو روی `factory_code` و `model` انجام می‌شود (`customer.py:136–137`) و در مدل ایندکس دارند. عملاً «جستجو با کد کارخانه» هرگز نتیجه نمی‌دهد.
31. پلکان قیمت مخصوص مشتری است و برای همه اعمال می‌شود؛ هیچ تفکیک «خرده/عمده‌فروش» در سطح *کاربر* وجود ندارد (فقط سه قیمت روی محصول).
32. سفارش: بدون انصراف توسط مشتری، بدون آدرس/ارسال، بدون یادداشت، بدون تخفیف/مالیات.
33. تاریخ‌ها با `datetime.utcnow()` (۲۴ مورد، در ۳.۱۲ deprecated) در ستون `timestamptz` → مقایسه‌ی naive در گزارش «امروز/این ماه» (`admin.py:2241`) بر مبنای UTC است، نه تهران (+۳:۳۰).
34. `OrderItem.product_id` با `SET NULL` درست طراحی شده (Snapshot حفظ می‌شود) — ولی Snapshot نام محصول، همان لحظه ثبت می‌شود؛ خوب است.

### ۶.۵ UX / محصول

35. `/products` تا ۵۰ پیام جداگانه می‌فرستد (بدون صفحه‌بندی) — اسپم شدید چت.
36. جستجو در `customer.py:124` ارقام فارسی را نرمال نمی‌کند (تابع `normalize_digits` وجود دارد، استفاده نشده).
37. در `/start` پیام «منوی قدیمی حذف شد.» به کاربر نشان داده می‌شود — جمله‌ای برای توسعه‌دهنده، نه مشتری (`common.py:157, 167`).
38. Import: خروجی mapping خام انگلیسی (`{'name': 2, 'price': 3}`) به ادمین نمایش داده می‌شود (`imports.py:145`).
39. Audit Log فقط ۳۰ ردیف آخر و با کدهای انگلیسی (`product.edit.price.retail`) — قابل فهم برای حسابدار نیست.
40. بعد از حذف همه مجوزهای یک مدیر، دستور `/admin` از منوی او حذف نمی‌شود (`admin.py:2094–2106` فقط در حالت `any_allowed` ست می‌کند).
41. اگر کاربری در میانه‌ی ثبت‌نام ربات را ری‌استارت کند، state از بین می‌رود ولی پیام راهنما در چت می‌ماند → گیر کردن.
42. `MenuButtonCommands` و دستورها در هر بوت برای همه‌ی مدیران ست می‌شود (N فراخوانی API) — روی تعداد زیاد کند است.

### ۶.۶ معماری / عملیات

43. **بدون هیچ تست خودکار، بدون CI، بدون pyproject/ruff/mypy.** `requirements.txt` فقط رانتایم است.
44. **بدون logging و بدون error handler سراسری** (`main.py:23`) → خطاها فقط در لاگ پیش‌فرض aiogram؛ نه اعلان به Owner، نه پیام مهربان به کاربر.
45. **بدون Alembic.** `create_all` + ۱۸ دستور SQL خام. با اضافه‌شدن نسخه‌ها شکننده می‌شود (مثال بند ۲۲).
46. **Redis تقریباً بلااستفاده است** — فقط payload موقت Import؛ اما در `docker-compose` اجباری و health-check دارد. FSM روی Redis (که آماده است) گذاشته نشده.
47. N+1 شدید: `export._rows` برای هر محصول ۲ کوئری، `approver_telegram_ids` برای هر کاربر ۱ کوئری، `compact_product` برای هر محصول ۲ کوئری، `_manager_permission_markup` برای هر مجوز ۱ کوئری.
48. `admin.py` ۲۳۲۴ خط و ۸۸ هندلر — پرریسک برای تغییر. بعضی هندلرهای legacy (`legacy:admin:*`) کد مرده‌اند.
49. Docker: بدون non-root، بدون healthcheck برای bot، بدون volume برای لاگ/بکاپ؛ `restart: unless-stopped` خوب است.
50. README هنوز «Trading Bot V1» است و امکانات را «قابل تکمیل» می‌داند؛ با وضعیت واقعی V2.3 نمی‌خواند.

---

## ۷) نقاط قوت (که باید حفظ شوند)

- لایه‌بندی `models / services / routers` و استفاده‌ی درست از async SQLAlchemy 2.0 (`Mapped`, `async_sessionmaker`).
- **`PriceHistory` + `AuditLog`** برای هر تغییر قیمت و هر عملیات حساس؛ با `source` و `payload`. این پایه‌ی خوبی برای حسابرسی است.
- **Snapshot در `OrderItem`** و `ON DELETE SET NULL` — طراحی درست برای حفظ سابقه‌ی فروش بعد از حذف محصول.
- **لینک دعوت مدیر**: یک‌بارمصرف، هش‌شده، دارای انقضا، با انتخاب دقیق مجوز و لغو. این از اکثر ربات‌های مشابه بهتر است.
- **Dry Run اکسل** با تشخیص سرستون fuzzy و سه سطح تطبیق + پنل «موارد مشکوک».
- **Publisher کانال** با «ویرایش پست قبلی به‌جای ارسال تکراری» و ذخیره‌ی `post_url`.
- فارسی‌سازی اعداد (`fa_num`), PDF راست‌به‌چپ با reportlab+reshaper+bidi، و خروجی Excel آماده.
- تنظیمات از `.env`، رمزها بیرون از کد، Compose با healthcheck دیتابیس/ردیس.
- کد کامپایل می‌شود، بدون syntax error، بدون وابستگی گمشده در `requirements.txt`.

---

## ۸) نقشه راه پیشنهادی

### فاز ۰ — «قبل از تحویل» (~۱ روز)
- دکوریتور `@guard("perm")` و پوشش ۱۰۰٪ هندلرهای ادمین؛ حذف `admins:add`.
- middleware تأیید مشتری برای همه‌ی مسیرهای خرید؛ حذف گیت‌های پراکنده.
- `(message.text or "")` در همه‌جا + `F.text` روی هندلرهای متنی.
- اصلاح `.env.example` (حذف خط خالی `OWNER_TELEGRAM_ID` یا `int | None` با `BeforeValidator`).
- نوشتن Snapshot مشتری در `create_order_from_cart`.
- اصلاح پیام وضعیت سفارش با برچسب فارسی؛ فارسی‌سازی `stock_status`.

### فاز ۱ — صحت داده (~۳ روز)
- Import: ستون‌های «قیمت خرید / قیمت فروش» جدا، منوی انتخاب نوع قیمت، امکان رد/ویرایش هر ردیف، Undo با `import_batches`، مدیریت خطای Redis، `raw_data` JSON-safe.
- پلکان: اعتبارسنجی مشترک برای هر دو مسیر؛ استفاده از `wholesale_price` بر اساس سطح مشتری.
- موجودی: ورودی و کسر آن در ثبت سفارش؛ جلوگیری از سفارش بیش از موجودی.
- شماره‌گذاری اتمیک SKU/سفارش (sequence یا `SELECT ... FOR UPDATE`).
- فیش: انتخاب سفارش، جلوگیری از فیش تکراری، `try/except` روی همه‌ی ارسال‌ها.

### فاز ۲ — پایداری (~۱ هفته)
- Alembic به‌جای `migrate.py`؛ حذف UPDATEهای تکراری.
- `RedisStorage` برای FSM + `drop_pending_updates=False` + shutdown تمیز.
- `logging` ساختاریافته + error handler سراسری که خطا را برای Owner می‌فرستد.
- صفحه‌بندی محصولات/سفارش‌ها/کاربران؛ حذف N+1.
- pytest + `aiosqlite`/testcontainers برای سرویس‌ها وتست سناریوی «سبد→سفارش→فیش»; CI روی GitHub Actions.

### فاز ۳ — تکمیل محصول
- ورودی `factory_code` / `model` / `unit` در ساخت و ویرایش محصول (تا جستجو معنا پیدا کند).
- انصراف سفارش توسط مشتری، آدرس/روش ارسال، کد رهگیری.
- کانال: مدیریت FloodWait، محدودکردن caption، آرشیو پست‌های حذف‌شده، sync گروهی.
- گزارش‌ها بر مبنای منطقه‌زمانی تهران + خروجی Excel از سفارش‌ها.

---

## ۹) پیوست الف — چک‌لیست تست دستی پیشنهادی

1. نصب تمیز با `.env.example` → آیا بوت بالا می‌آید؟
2. مشتری جدید با `registration_requires_approval=1` → آیا `/products` و `checkout` بسته است؟ (الان نه)
3. ثبت سفارش → «جزئیات سفارش» در پنل: نام/تلفن/مغازه مشتری پر است؟ (الان نه)
4. ارسال فیش با دو سفارش باز → کدام سفارش؟ ارسال فیش دوم → چه می‌شود؟
5. Import فایل با قیمت خرید → قیمت فروش محصولات چه تغییری کرد؟ (الان برابر می‌شود)
6. Import ردیفی با شباهت ۹۶٪ و محصول اشتباه → قابل جلوگیری است؟ (الان نه)
7. کلیک روی `user:approve` با اکانت بدون مجوز (کلاینت سفارشی) → تأیید می‌شود؟ (الان بله)
8. ارسال عکس در حالت «نام محصول را وارد کنید» → پاسخ یا سکوت؟
9. افزودن پلکان ۱۰۰–۱۹۹ و سپس ۱۵۰–۲۵۰ → پذیرفته می‌شود؟ (الان بله)
10. «ارسال همه محصولات» با ۳۰۰ محصول → FloodWait؟
11. ری‌استارت وسط «افزودن محصول» → وضعیت گم می‌شود؟
12. حذف محصول منتشرشده → وضعیت پست کانال؟

## پیوست ب — ۴۱ هندلر بدون بررسی مجوز (`admin.py`)

```
add_product_brand_yes/no, addpbrand:*, addp:images_done|no_images,
addp:tiers:yes/no, addp:tier_more, addp:tier_done,
editp:select:*, editp:pricing, editp:pricefield:*, editp:field:*, editp:toggle,
brand|category|supplier:add, brand|category|supplier:edit:<id>,
admins:add, bank:add, bank:toggle:*, bank:edit:*,
pricechg:mode:*, pricechg:scope:*, pricechg:toggle:*, pricechg:more,
pricechg:selected_done, pricechg:category:*, pricechg:field:*, pricechg:cancel,
admin:noop_cancel, tier:select:*, tier:add, tier:clear:*,
orders:status:*, order:detail:*, user:detail:*, user:approve:*, user:disable:*,
admininvite:cancel, sys:toggle:*, sys:support
```

> توضیح: بخشی از این‌ها «ادامه‌ی جریان» هستند و نقطه‌ی ورودشان مجوز دارد، ولی چون دکمه‌ها در چت باقی می‌مانند و `callback_data` قابل جعل است، همه باید بررسی مجوز داشته باشند (یا حداقل state-bound باشند).
