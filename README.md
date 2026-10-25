# Trading Bot V1

ربات مدیریت کاتالوگ، قیمت و سفارش برای شرکت بازرگانی.

## امکانات V1
- Owner / Admin / Customer foundation
- Product / Brand / Category / Supplier
- Internal SKU
- Price history
- Tier pricing data model
- Cart + Order + immutable order price snapshots
- Payment receipt data model
- Permission + Audit Log data model
- Excel smart header detection
- Excel Dry Run
- Supplier mapping model
- Excel price list export
- PostgreSQL + Redis + Docker Compose

## نصب روی سرور

```bash
cd /opt
unzip trading-bot-v1.zip -d trading-bot-v1
cd trading-bot-v1
cp .env.example .env
```

پسورد تصادفی:
```bash
openssl rand -hex 24
```

فایل `.env` را ویرایش کنید و موارد زیر را پر کنید:
- TELEGRAM_BOT_TOKEN
- OWNER_TELEGRAM_ID
- POSTGRES_PASSWORD
- DATABASE_URL
- اطلاعات کارت در صورت نیاز

سپس:
```bash
docker compose build
docker compose up -d
docker compose logs -f bot
```

## نکته
این نسخه هسته V1 قابل اجراست و ساختار همه قابلیت‌های اصلی را دارد. بخش‌هایی مثل UI کامل ویرایش محصول، تأیید فیش با دکمه‌های چندمدیره و Import نهایی پس از Dry Run در همین معماری قابل تکمیل هستند.
