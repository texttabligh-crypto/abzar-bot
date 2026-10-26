#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  نصب / به‌روزرسانی خودکار Abzar Bot
#
#  استفاده (روی سرور، داخل ترمینال):
#     curl -fsSL -o install.sh https://raw.githubusercontent.com/texttabligh-crypto/abzar-bot/main/install.sh
#     bash install.sh
#
#  اگر می‌خواهید مسیر پروژه را خودتان بدهید:
#     bash install.sh /opt/trading-bot-v1
#
#  این اسکریپت:
#   • Docker و Docker Compose را بررسی می‌کند
#   • از دیتابیس فعلی بکاپ می‌گیرد (اگر نصب قبلی وجود داشته باشد)
#   • کد جدید را از GitHub می‌گیرد و جایگزین می‌کند (به‌جز .env و backups)
#   • اگر .env نبود، با پرسیدن چند سؤال می‌سازد
#   • ایمیج را بدون کش می‌سازد و ربات را ری‌استارت می‌کند
# ─────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ZIP="https://codeload.github.com/texttabligh-crypto/abzar-bot/zip/refs/heads/main"
DEFAULT_DIR="/opt/trading-bot-v1"
DRY_RUN="0"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN="1" ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
    *) TARGET_DIR="$arg" ;;
  esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m✖ %s\033[0m\n' "$*"; exit 1; }
run()   { if [ "$DRY_RUN" = "1" ]; then echo "  [dry-run] $*"; else "$@"; fi; }

bold "═══ نصب / به‌روزرسانی Abzar Bot ═══"

# ---------- ۱) بررسی پیش‌نیازها ----------
bold "۱) بررسی پیش‌نیازها"
if ! command -v docker >/dev/null 2>&1; then
  if [ "$DRY_RUN" = "1" ]; then warn "Docker در دسترس نیست (dry-run ادامه می‌دهد)"
  else fail "Docker نصب نیست. اول Docker را روی سرور نصب کنید."; fi
else
  ok "Docker: $(docker --version 2>/dev/null | head -1)"
fi

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  DC="docker compose"
  [ "$DRY_RUN" = "1" ] || fail "Docker Compose نصب نیست."
fi
ok "Compose: $($DC version 2>/dev/null | head -1 || echo 'docker compose')"

# ---------- ۲) تعیین مسیر پروژه ----------
bold "۲) مسیر پروژه"
if [ -n "${TARGET_DIR:-}" ]; then
  DEST="$TARGET_DIR"
elif [ -f "./docker-compose.yml" ] && [ -f "./main.py" ]; then
  DEST="$(pwd)"
elif [ -d "$DEFAULT_DIR" ]; then
  DEST="$DEFAULT_DIR"
else
  DEST="$DEFAULT_DIR"
fi
mkdir -p "$DEST"
ok "مسیر: $DEST"

UPGRADE="0"
[ -f "$DEST/docker-compose.yml" ] && UPGRADE="1"

# ---------- ۳) بکاپ دیتابیس ----------
bold "۳) بکاپ"
mkdir -p "$DEST/backups"
if [ "$UPGRADE" = "1" ] && [ -f "$DEST/.env" ]; then
  DB_USER="$(grep -E '^POSTGRES_USER=' "$DEST/.env" | cut -d= -f2- || true)"
  DB_NAME="$(grep -E '^POSTGRES_DB=' "$DEST/.env" | cut -d= -f2- || true)"
  DB_USER="${DB_USER:-tradingbot}"; DB_NAME="${DB_NAME:-tradingbot}"
  BACKUP_FILE="$DEST/backups/db-$(date +%Y%m%d-%H%M%S).sql"
  if ( cd "$DEST" && $DC ps postgres 2>/dev/null | grep -qi running ); then
    if run bash -c "cd '$DEST' && $DC exec -T postgres pg_dump -U '$DB_USER' '$DB_NAME' > '$BACKUP_FILE'" 2>/dev/null; then
      ok "بکاپ: $BACKUP_FILE"
    else
      rm -f "$BACKUP_FILE"; warn "بکاپ گرفته نشد؛ ادامه می‌دهیم"
    fi
  else
    warn "دیتابیس در حال اجرا نیست؛ بکاپ لازم نیست"
  fi
else
  warn "نصب تازه است؛ بکاپی لازم نیست"
fi

# ---------- ۴) گرفتن کد جدید ----------
bold "۴) دریافت کد جدید از GitHub"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ZIP_FILE="$TMP_DIR/repo.zip"
if [ "$DRY_RUN" = "1" ]; then
  echo "  [dry-run] curl $REPO_ZIP"
else
  command -v curl >/dev/null 2>&1 || fail "curl نصب نیست."
  curl -fsSL "$REPO_ZIP" -o "$ZIP_FILE" || fail "دانلود کد ناموفق بود. اتصال اینترنت سرور را بررسی کنید."
fi

if [ "$DRY_RUN" != "1" ]; then
  mkdir -p "$TMP_DIR/src"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$ZIP_FILE" -d "$TMP_DIR/src"
  else
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP_FILE" "$TMP_DIR/src" \
      || fail "برای باز کردن فایل، unzip یا python3 لازم است."
  fi
  SRC_DIR="$(find "$TMP_DIR/src" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -d "$SRC_DIR/app" ] || fail "ساختار فایل دانلودشده درست نیست."
  ok "کد دانلود شد: $(grep -m1 APP_VERSION "$SRC_DIR/app/version.py" | cut -d'"' -f2)"

  # جایگزینی فایل‌ها: .env و backups و .git دست نمی‌خورند
  if command -v rsync >/dev/null 2>&1; then
    run rsync -a --delete \
      --exclude '.env' --exclude 'backups' --exclude '.git' --exclude '.venv' \
      "$SRC_DIR/" "$DEST/"
  else
    ( cd "$SRC_DIR" && tar -cf - --exclude='.git' . ) | ( cd "$DEST" && tar -xf - )
  fi
  ok "فایل‌ها به‌روزرسانی شد"
fi

# ---------- ۵) فایل .env ----------
bold "۵) تنظیمات (.env)"
ENV_FILE="$DEST/.env"
random_password() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

if [ -f "$ENV_FILE" ]; then
  ok "فایل .env موجود است؛ دست نمی‌زنیم"
  TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
  if [ -z "${TOKEN:-}" ]; then
    warn "TELEGRAM_BOT_TOKEN خالی است — باید پر شود وگرنه ربات بالا نمی‌آید"
  fi
elif [ "$DRY_RUN" = "1" ]; then
  echo "  [dry-run] ساخت .env با پرسش از کاربر"
else
  echo
  echo "  فایل .env وجود ندارد؛ چند سؤال کوتاه:"
  printf "  توکن ربات (از @BotFather): "; read -r TOKEN
  [ -n "$TOKEN" ] || fail "توکن ربات الزامی است."
  printf "  آیدی عددی خودتان (از @userinfobot، می‌تواند خالی بماند): "; read -r OWNER_ID
  printf "  نام شرکت [شرکت بازرگانی]: "; read -r COMPANY; COMPANY="${COMPANY:-شرکت بازرگانی}"
  printf "  شماره کارت (اختیاری): "; read -r CARD
  printf "  نام صاحب حساب (اختیاری): "; read -r HOLDER
  printf "  شبا (اختیاری): "; read -r IBAN
  PW="$(random_password)"
  cat > "$ENV_FILE" <<ENVEOF
APP_ENV=production
TELEGRAM_BOT_TOKEN=$TOKEN
OWNER_TELEGRAM_ID=$OWNER_ID

POSTGRES_DB=tradingbot
POSTGRES_USER=tradingbot
POSTGRES_PASSWORD=$PW
DATABASE_URL=postgresql+asyncpg://tradingbot:$PW@postgres:5432/tradingbot
REDIS_URL=redis://redis:6379/0

COMPANY_NAME=$COMPANY
CARD_NUMBER=$CARD
CARD_HOLDER=$HOLDER
IBAN=$IBAN
ENVEOF
  chmod 600 "$ENV_FILE"
  ok "فایل .env ساخته شد (رمز دیتابیس تصادفی تولید شد)"
fi

# ---------- ۶) ساخت و اجرا ----------
bold "۶) ساخت ایمیج و اجرای ربات"
cd "$DEST"
run $DC build --no-cache bot
run $DC up -d --force-recreate bot

if [ "$DRY_RUN" = "1" ]; then
  bold "پایان dry-run (هیچ تغییری اعمال نشد)"; exit 0
fi

bold "۷) وضعیت"
sleep 5
$DC ps
echo
$DC logs --tail=25 bot || true

cat <<'MSG'

────────────────────────────────────────────────────────
✅ تمام شد.

حالا در تلگرام به ربات /version بفرستید:
   • اگر «نسخه 2.4.0» و فهرست قابلیت‌ها را دید → نصب درست است
   • اگر جواب نداد → هنوز نسخه قدیمی است:
        docker compose down && bash install.sh

بعد از آن یک‌بار /admin را بزنید تا منوی تازه ساخته شود.
────────────────────────────────────────────────────────
MSG
