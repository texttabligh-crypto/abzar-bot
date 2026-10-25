from sqlalchemy import text
from app.db.database import engine

MIGRATIONS = [
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS factory_code VARCHAR(150)",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS model VARCHAR(150)",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_quantity INTEGER",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS purchase_price NUMERIC(18,2)",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS wholesale_price NUMERIC(18,2)",
    "UPDATE products SET purchase_price = base_price WHERE purchase_price IS NULL",
    "UPDATE products SET wholesale_price = base_price WHERE wholesale_price IS NULL",
    "ALTER TABLE products ALTER COLUMN purchase_price SET DEFAULT 0",
    "ALTER TABLE products ALTER COLUMN wholesale_price SET DEFAULT 0",
    "ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_type VARCHAR(30) NOT NULL DEFAULT 'retail'",
    "ALTER TABLE order_items ALTER COLUMN product_id DROP NOT NULL",
    """DO $$
DECLARE current_delete_action "char";
BEGIN
    SELECT confdeltype INTO current_delete_action
    FROM pg_constraint
    WHERE conname = 'order_items_product_id_fkey';
    IF current_delete_action IS DISTINCT FROM 'n' THEN
        ALTER TABLE order_items DROP CONSTRAINT IF EXISTS order_items_product_id_fkey;
        ALTER TABLE order_items ADD CONSTRAINT order_items_product_id_fkey
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL;
    END IF;
END $$""",
    "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL",
    "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS last_import_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name_snapshot VARCHAR(220)",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone_snapshot VARCHAR(30)",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shop_name_snapshot VARCHAR(200)",
    "ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
]

async def run_safe_migrations():
    async with engine.begin() as conn:
        for sql in MIGRATIONS:
            await conn.execute(text(sql))
