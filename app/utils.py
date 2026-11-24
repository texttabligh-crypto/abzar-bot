FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

STOCK_LABELS = {
    "available": "موجود",
    "in_stock": "موجود",
    "unavailable": "ناموجود",
    "out_of_stock": "ناموجود",
    "preorder": "پیش‌سفارش",
    "unknown": "نامشخص",
}

ORDER_STATUS_LABELS = {
    "WAITING_PAYMENT": "منتظر پرداخت",
    "PAYMENT_REVIEW": "منتظر تأیید فیش",
    "CONFIRMED": "تأیید شده",
    "PREPARING": "در حال آماده‌سازی",
    "READY": "آماده",
    "SHIPPED": "ارسال شده",
    "DELIVERED": "تکمیل شده",
    "CANCELLED": "لغو شده",
}

# وضعیت‌هایی که مدیر می‌تواند از هر وضعیت به آن‌ها برود.
# (PAYMENT_REVIEW و CONFIRMED از مسیر فیش پرداخت تنظیم می‌شوند.)
ORDER_TRANSITIONS = {
    "WAITING_PAYMENT": ["CANCELLED"],
    "PAYMENT_REVIEW": ["WAITING_PAYMENT", "CANCELLED"],
    "CONFIRMED": ["PREPARING", "READY", "SHIPPED", "CANCELLED"],
    "PREPARING": ["READY", "SHIPPED", "CANCELLED"],
    "READY": ["SHIPPED", "DELIVERED", "CANCELLED"],
    "SHIPPED": ["DELIVERED"],
    "DELIVERED": [],
    "CANCELLED": [],
}


def allowed_order_transitions(current: str | None) -> list[str]:
    return list(ORDER_TRANSITIONS.get((current or "").strip(), []))


CUSTOMER_TYPE_LABELS = {"retail": "خرد", "wholesale": "عمده"}


def fa_num(value) -> str:
    try:
        return f"{int(value):,}".replace(",", "٬").translate(FA_DIGITS)
    except Exception:
        return "۰"


def normalize_digits(text: str) -> str:
    return (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def norm_text(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("ي", "ی").replace("ك", "ک").split())


def stock_label(value: str | None) -> str:
    return STOCK_LABELS.get((value or "").strip(), value or "نامشخص")


def order_status_label(value: str | None) -> str:
    return ORDER_STATUS_LABELS.get((value or "").strip(), value or "-")


def parse_int(text: str | None) -> int | None:
    raw = normalize_digits(text or "").replace(",", "").replace("٬", "").strip()
    return int(raw) if raw.isdigit() else None
