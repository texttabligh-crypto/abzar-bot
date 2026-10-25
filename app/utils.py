FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

def fa_num(value) -> str:
    try:
        return f"{int(value):,}".replace(",", "٬").translate(FA_DIGITS)
    except Exception:
        return "۰"

def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))

def norm_text(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("ي","ی").replace("ك","ک").split())
