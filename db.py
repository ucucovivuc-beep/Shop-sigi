import sqlite3
from datetime import datetime, timezone

DB_PATH = "shop.db"

# Статусы заказа:
#   new        — ожидает оплаты
#   confirm    — покупатель нажал «Я оплатил», ждёт проверки админом (только для ручных способов)
#   delivered  — оплачен и выдан
#   cancelled  — отменён (например, не удалось создать счёт)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column_if_missing(conn, table, column, ddl):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region_id INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            UNIQUE(region_id, name)
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price INTEGER NOT NULL,
            photo_file_id TEXT
        );
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_file_id TEXT NOT NULL,
            caption TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            qty INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Миграция для уже существующей БД
        _add_column_if_missing(conn, "products", "delivery_text", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "orders", "payment_method", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "orders", "invoice_id", "TEXT")
        _add_column_if_missing(conn, "order_items", "delivery_text", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "users", "balance", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "orders", "promo_code", "TEXT")
        _add_column_if_missing(conn, "orders", "discount", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "users", "referred_by", "INTEGER")
        _add_column_if_missing(conn, "orders", "ref_credited", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ref_bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            value INTEGER NOT NULL,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            invoice_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)


def get_regions():
    with connect() as conn:
        return conn.execute("SELECT * FROM regions ORDER BY name").fetchall()


def get_region(region_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone()


def get_or_create_region(name):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO regions (name) VALUES (?)", (name,))
        return conn.execute("SELECT id FROM regions WHERE name = ?", (name,)).fetchone()["id"]


def get_categories(region_id):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE region_id = ? ORDER BY name", (region_id,)
        ).fetchall()


def get_category(category_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def get_or_create_category(region_id, name):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories (region_id, name) VALUES (?, ?)", (region_id, name)
        )
        return conn.execute(
            "SELECT id FROM categories WHERE region_id = ? AND name = ?", (region_id, name)
        ).fetchone()["id"]


def get_products(category_id):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE category_id = ? ORDER BY id", (category_id,)
        ).fetchall()


def get_product(product_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def add_product(category_id, name, description, price, photo_file_id, delivery_text=""):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO products (category_id, name, description, price, photo_file_id, delivery_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (category_id, name, description, price, photo_file_id, delivery_text),
        )
        return cur.lastrowid


def update_product_price(product_id, price):
    with connect() as conn:
        conn.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))


def delete_product(product_id):
    with connect() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


def get_banners():
    with connect() as conn:
        return conn.execute("SELECT * FROM banners ORDER BY id").fetchall()


def add_banner(photo_file_id, caption):
    with connect() as conn:
        conn.execute(
            "INSERT INTO banners (photo_file_id, caption) VALUES (?, ?)", (photo_file_id, caption)
        )


def create_order(user_id, username, items, payment_method, promo_code=None, discount=0):
    """items: список (name, price, qty, delivery_text). Возвращает id заказа.
    total уже с учётом скидки по промокоду."""
    total = sum(price * qty for _, price, qty, _ in items) - discount
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (user_id, username, total, payment_method, promo_code, discount) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, total, payment_method, promo_code, discount),
        )
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, name, price, qty, delivery_text) "
            "VALUES (?, ?, ?, ?, ?)",
            [(order_id, name, price, qty, delivery) for name, price, qty, delivery in items],
        )
        return order_id


def set_order_invoice(order_id, invoice_id):
    with connect() as conn:
        conn.execute("UPDATE orders SET invoice_id = ? WHERE id = ?", (invoice_id, order_id))


def get_order(order_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def get_order_items(order_id):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()


def get_user_orders(user_id, limit=10):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()


def get_pending_crypto_orders():
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE payment_method = 'crypto' AND status = 'new' "
            "AND invoice_id IS NOT NULL"
        ).fetchall()


def transition_order(order_id, from_status, to_status):
    """Меняет статус только если он сейчас from_status. Возвращает True, если получилось.
    Защищает от двойной выдачи (например, когда одновременно срабатывают поллер и кнопка админа)."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = ? WHERE id = ? AND status = ?",
            (to_status, order_id, from_status),
        )
        return cur.rowcount == 1


def get_setting(key, default=""):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def upsert_user(user_id, username):
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
            (user_id, username),
        )


def get_all_user_ids():
    with connect() as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM users")]


def get_username(user_id):
    with connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["username"] if row else None


def add_admin(user_id, username):
    with connect() as conn:
        conn.execute(
            "INSERT INTO admins (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
            (user_id, username),
        )


def remove_admin(user_id):
    with connect() as conn:
        conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))


def get_admins():
    with connect() as conn:
        return conn.execute("SELECT * FROM admins ORDER BY user_id").fetchall()


def is_db_admin(user_id):
    with connect() as conn:
        return conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None


def get_balance(user_id):
    with connect() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0


def add_balance(user_id, amount):
    with connect() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def spend_balance(user_id, amount):
    """Списывает сумму, только если её хватает. Возвращает True при успехе."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        return cur.rowcount == 1


def create_topup(user_id, amount, method):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO topups (user_id, amount, method) VALUES (?, ?, ?)",
            (user_id, amount, method),
        )
        return cur.lastrowid


def set_topup_invoice(topup_id, invoice_id):
    with connect() as conn:
        conn.execute("UPDATE topups SET invoice_id = ? WHERE id = ?", (invoice_id, topup_id))


def get_topup(topup_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM topups WHERE id = ?", (topup_id,)).fetchone()


def transition_topup(topup_id, from_status, to_status):
    with connect() as conn:
        cur = conn.execute(
            "UPDATE topups SET status = ? WHERE id = ? AND status = ?",
            (to_status, topup_id, from_status),
        )
        return cur.rowcount == 1


def _now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def create_promo(code, kind, value, max_uses, expires_at):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO promocodes (code, kind, value, max_uses, expires_at) VALUES (?, ?, ?, ?, ?)",
            (code, kind, value, max_uses, expires_at),
        )
        return cur.lastrowid


def get_promo(code):
    with connect() as conn:
        return conn.execute("SELECT * FROM promocodes WHERE code = ?", (code,)).fetchone()


def list_promos():
    with connect() as conn:
        return conn.execute("SELECT * FROM promocodes ORDER BY id DESC").fetchall()


def deactivate_promo(promo_id):
    with connect() as conn:
        conn.execute("UPDATE promocodes SET active = 0 WHERE id = ?", (promo_id,))


def promo_is_valid(promo):
    if not promo["active"]:
        return False
    if promo["max_uses"] is not None and promo["used_count"] >= promo["max_uses"]:
        return False
    if promo["expires_at"] is not None and promo["expires_at"] <= _now_utc_str():
        return False
    return True


def calc_discount(promo, subtotal):
    if promo["kind"] == "percent":
        return subtotal * promo["value"] // 100
    return min(promo["value"], subtotal)


def use_promo(code):
    """Атомарно увеличивает счётчик использований, если код ещё действует."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE promocodes SET used_count = used_count + 1 "
            "WHERE code = ? AND active = 1 "
            "AND (max_uses IS NULL OR used_count < max_uses) "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (code, _now_utc_str()),
        )
        return cur.rowcount == 1


def release_promo(code):
    """Откатывает использование, если заказ не состоялся."""
    with connect() as conn:
        conn.execute(
            "UPDATE promocodes SET used_count = used_count - 1 WHERE code = ? AND used_count > 0",
            (code,),
        )


def delete_banner(banner_id):
    with connect() as conn:
        conn.execute("DELETE FROM banners WHERE id = ?", (banner_id,))


# ---------- Рефералы ----------

def set_referrer(user_id, referrer_id):
    """Закрепляет пригласившего. Только для новых пользователей без заказов и без реферера."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET referred_by = ? "
            "WHERE user_id = ? AND referred_by IS NULL AND user_id <> ? "
            "AND EXISTS (SELECT 1 FROM users WHERE user_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM orders WHERE user_id = ?)",
            (referrer_id, user_id, referrer_id, referrer_id, user_id),
        )
        return cur.rowcount == 1


def get_referrer(user_id):
    with connect() as conn:
        row = conn.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["referred_by"] if row else None


def mark_ref_credited(order_id):
    """Возвращает True один раз для каждого заказа, чтобы бонус не начислялся дважды."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET ref_credited = 1 WHERE id = ? AND ref_credited = 0", (order_id,)
        )
        return cur.rowcount == 1


def add_ref_bonus(referrer_id, order_id, amount):
    with connect() as conn:
        conn.execute(
            "INSERT INTO ref_bonuses (referrer_id, order_id, amount) VALUES (?, ?, ?)",
            (referrer_id, order_id, amount),
        )
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, referrer_id))


def referral_summary(referrer_id):
    with connect() as conn:
        invited = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referred_by = ?", (referrer_id,)
        ).fetchone()["c"]
        earned = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM ref_bonuses WHERE referrer_id = ?",
            (referrer_id,),
        ).fetchone()["s"]
        return invited, earned


def ref_totals():
    with connect() as conn:
        invited = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referred_by IS NOT NULL"
        ).fetchone()["c"]
        paid = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM ref_bonuses").fetchone()["s"]
        return invited, paid


# ---------- Статистика ----------

def stats_summary(since):
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(total), 0) AS s FROM orders "
            "WHERE status = 'delivered' AND created_at >= ?",
            (since,),
        ).fetchone()
        count, revenue = row["c"], row["s"]
        new_users = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE first_seen >= ?", (since,)
        ).fetchone()["c"]
        topups = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM topups WHERE status = 'credited' AND created_at >= ?",
            (since,),
        ).fetchone()["s"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status IN ('new', 'confirm')"
        ).fetchone()["c"]
        return {
            "count": count,
            "revenue": revenue,
            "avg": revenue // count if count else 0,
            "new_users": new_users,
            "topups": topups,
            "pending": pending,
        }


def stats_by_method(since):
    with connect() as conn:
        return conn.execute(
            "SELECT payment_method, COUNT(*) AS cnt, SUM(total) AS sum FROM orders "
            "WHERE status = 'delivered' AND created_at >= ? GROUP BY payment_method",
            (since,),
        ).fetchall()


def stats_top_products(since, limit=5):
    with connect() as conn:
        return conn.execute(
            "SELECT oi.name AS name, SUM(oi.qty) AS qty, SUM(oi.price * oi.qty) AS sum "
            "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.status = 'delivered' AND o.created_at >= ? "
            "GROUP BY oi.name ORDER BY qty DESC LIMIT ?",
            (since, limit),
        ).fetchall()
