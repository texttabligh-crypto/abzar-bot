"""نسخه‌ی برنامه — برای اینکه از داخل تلگرام بتوانیم مطمئن شویم کدام نسخه روی سرور اجرا می‌شود."""

APP_VERSION = "2.4.0"
BUILD_DATE = "2026-10-26"

# قابلیت‌هایی که فقط در این نسخه وجود دارند. اگر روی سرور نباشند،
# یعنی کد جدید مستقر نشده است.
FEATURES = [
    ("تفکیک مشتری خرد و عمده", "app/services/catalog.py"),
    ("دسته‌بندی درختی + زیرمجموعه", "app/services/catalog.py"),
    ("اتصال به کانال + فوتر", "app/services/publishing.py"),
    ("ویرایش پشت‌سرهم سه قیمت", "app/bot/routers/admin.py"),
    ("افزایش درصدی روی سه قیمت", "app/bot/routers/admin.py"),
]


def short_version() -> str:
    return f"نسخه {APP_VERSION} ({BUILD_DATE})"


def version_text(is_admin: bool = False) -> str:
    lines = [f"🤖 Abzar Bot", f"نسخه: {APP_VERSION}", f"تاریخ ساخت: {BUILD_DATE}", ""]
    if is_admin:
        lines.append("قابلیت‌های نسخه جدید:")
        for title, _module in FEATURES:
            lines.append(f"✅ {title}")
        lines.append("")
        lines.append("اگر این فهرست را می‌بینید، کد جدید روی سرور مستقر شده است.")
    else:
        lines.append("ربات آماده است. برای شروع /menu را بزنید.")
    return "\n".join(lines)
