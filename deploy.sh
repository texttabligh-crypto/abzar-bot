#!/usr/bin/env bash
# استقرار نسخه جدید روی سرور
# استفاده:  ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "❌ فایل .env پیدا نشد. اول آن را از .env.example بسازید."
  exit 1
fi

# shellcheck disable=SC1091
set -a; . ./.env; set +a
DB_USER="${POSTGRES_USER:-tradingbot}"
DB_NAME="${POSTGRES_DB:-tradingbot}"

echo "→ بررسی وجود src جدید (تست‌ها اختیاری است ولی توصیه می‌شود)"
if [ -d .venv ]; then
  .venv/bin/python -m pytest tests/ -q || {
    echo "❌ تست‌ها شکست خوردند. استقرار متوقف شد."
    exit 1
  }
fi

echo "→ بکاپ دیتابیس"
mkdir -p backups
BACKUP="backups/db-$(date +%Y%m%d-%H%M%S).sql"
if docker compose exec -T postgres pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP" 2>/dev/null; then
  echo "  ✔ $BACKUP ($(du -h "$BACKUP" | cut -f1))"
else
  rm -f "$BACKUP"
  echo "  ⚠ بکاپ گرفته نشد (دیتابیس در حال اجرا نبود؟) — ادامه می‌دهیم"
fi

echo "→ ساخت ایمیج بدون کش"
docker compose build --no-cache bot

echo "→ اجرای نسخه جدید"
docker compose up -d --force-recreate bot

sleep 5
echo "→ لاگ‌های ربات (۳۰ خط آخر)"
docker compose logs --tail=30 bot || true

cat <<'MSG'

────────────────────────────────────────────
✅ استقرار تمام شد.

برای اطمینان از اینکه نسخه جدید واقعاً روی سرور است:
  ۱) در تلگرام به ربات /version بفرستید
     باید ببینید:  نسخه 2.4.0  و فهرست قابلیت‌ها
  ۲) بعد /admin را بزنید تا منوی تازه ساخته شود
     (دکمه‌های منوی قدیمی در چت کار نمی‌کنند)

اگر /version نسخه 2.4.0 را نشان نداد، یعنی ایمیج قدیمی اجرا شده است:
  docker compose down && ./deploy.sh
────────────────────────────────────────────
MSG
