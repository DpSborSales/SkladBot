import json
import logging
from datetime import datetime
from database import get_db_connection
from config import HUB_SELLER_ID

logger = logging.getLogger(__name__)

# ========== Парсинг JSON ==========
def parse_contact(contact_json):
    if isinstance(contact_json, dict):
        return contact_json
    try:
        return json.loads(contact_json)
    except:
        return {}

def parse_items(items_json):
    if isinstance(items_json, list):
        return items_json
    try:
        return json.loads(items_json)
    except:
        return []

# ========== Продавцы ==========
def get_seller_by_telegram_id(telegram_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sellers WHERE telegram_id = %s", (telegram_id,))
            return cur.fetchone()

def get_seller_by_id(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sellers WHERE id = %s", (seller_id,))
            return cur.fetchone()

# ========== Заказы ==========
def get_order_by_number(order_number: str):
    logger.info(f"🔍 get_order_by_number: ищем заказ с номером '{order_number}'")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_number = %s", (order_number,))
            order = cur.fetchone()
            if order:
                logger.info(f"✅ Заказ найден: id={order['id']}, status={order['status']}")
                order['contact'] = parse_contact(order['contact'])
                order['items'] = parse_items(order['items'])
            else:
                logger.warning(f"❌ Заказ '{order_number}' не найден в таблице orders")
            return order

def mark_order_as_processed(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET stock_processed = TRUE WHERE id = %s", (order_id,))
            conn.commit()

# ========== Товары ==========
def get_all_products():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, price, price_seller, purchase_price FROM products ORDER BY name")
            return cur.fetchall()

# ========== Остатки ==========
def get_seller_stock(seller_id: int, product_id: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s",
                (seller_id, product_id)
            )
            result = cur.fetchone()
            return result['quantity'] if result else 0

def decrease_seller_stock(seller_id: int, product_id: int, quantity: int, reason: str, order_id: int = None):
    if quantity <= 0:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s",
                (seller_id, product_id)
            )
            row = cur.fetchone()
            if not row or row['quantity'] < quantity:
                logger.warning(f"⚠️ Недостаточно товара (id {product_id}) у продавца {seller_id}: доступно {row['quantity'] if row else 0}, требуется {quantity}. Списание будет выполнено.")
            cur.execute(
                "UPDATE seller_stock SET quantity = quantity - %s WHERE seller_id = %s AND product_id = %s",
                (quantity, seller_id, product_id)
            )
            cur.execute("""
                INSERT INTO stock_movements (product_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (product_id, -quantity, reason, order_id, seller_id))
            conn.commit()

def increase_seller_stock(seller_id: int, product_id: int, quantity: int, reason: str, order_id: int = None):
    if quantity <= 0:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seller_stock (seller_id, product_id, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (seller_id, product_id)
                DO UPDATE SET quantity = seller_stock.quantity + EXCLUDED.quantity
            """, (seller_id, product_id, quantity))
            cur.execute("""
                INSERT INTO stock_movements (product_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (product_id, quantity, reason, order_id, seller_id))
            conn.commit()

def get_negative_stock_summary(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.name, ss.quantity
                FROM seller_stock ss
                JOIN products p ON ss.product_id = p.id
                WHERE ss.seller_id = %s AND ss.quantity < 0
                ORDER BY p.name
            """, (seller_id,))
            return cur.fetchall()

# ========== Заявки на перемещение ==========
def create_transfer_request(seller_id: int, product_id: int, quantity: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transfer_requests (from_seller_id, to_seller_id, product_id, quantity, status)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (HUB_SELLER_ID, seller_id, product_id, quantity, 'pending'))
            request_id = cur.fetchone()['id']
            conn.commit()
            return request_id

def get_transfer_request(request_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM transfer_requests WHERE id = %s", (request_id,))
            return cur.fetchone()

def update_transfer_request_status(request_id: int, status: str):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transfer_requests SET status = %s, processed_at = %s WHERE id = %s",
                (status, datetime.utcnow().isoformat(), request_id)
            )
            conn.commit()

# ========== Расчёты с продавцами ==========
def get_seller_debt(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Определяем поле цены в зависимости от того, хаб это или обычный продавец
            if seller_id == HUB_SELLER_ID:
                price_field = "p.price"  # для хаба - цена покупателя
            else:
                price_field = "p.price_seller"  # для остальных - цена продавца

            cur.execute(f"""
                SELECT COALESCE(SUM({price_field} * (i->>'quantity')::int), 0) as total_sales
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_sales = cur.fetchone()['total_sales']

            cur.execute("""
                SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid
                FROM seller_payments
                WHERE seller_id = %s AND status = 'confirmed'
            """, (seller_id,))
            total_paid = cur.fetchone()['total_paid']

            debt = total_sales - total_paid
            return debt, total_sales, total_paid

def get_seller_profit(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(p.price * (i->>'quantity')::int), 0) as total_buyer
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_buyer = cur.fetchone()['total_buyer']

            cur.execute("""
                SELECT COALESCE(SUM(p.price_seller * (i->>'quantity')::int), 0) as total_seller
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_seller = cur.fetchone()['total_seller']

            profit = total_buyer - total_seller
            return profit, total_buyer, total_seller

def create_payment_request(seller_id: int, amount: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seller_payments (seller_id, amount, status)
                VALUES (%s, %s, 'pending')
                RETURNING id
            """, (seller_id, amount))
            payment_id = cur.fetchone()['id']
            conn.commit()
            return payment_id

def get_payment_request(payment_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM seller_payments WHERE id = %s", (payment_id,))
            return cur.fetchone()

def update_payment_status(payment_id: int, status: str, confirmed_amount: int = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if confirmed_amount is not None:
                cur.execute(
                    "UPDATE seller_payments SET status = %s, confirmed_amount = %s, processed_at = %s WHERE id = %s",
                    (status, confirmed_amount, datetime.utcnow().isoformat(), payment_id)
                )
            else:
                cur.execute(
                    "UPDATE seller_payments SET status = %s, processed_at = %s WHERE id = %s",
                    (status, datetime.utcnow().isoformat(), payment_id)
                )
            conn.commit()

# ========== Закупки (только для админа) ==========
def create_purchase(seller_id: int, items: list, total: int, comment: str = "") -> int:
    """
    Создаёт закупку и возвращает её ID.
    items: список словарей [{'product_id': int, 'quantity': int, 'price_per_unit': int}]
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO purchases (seller_id, total, comment)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (seller_id, total, comment))
            purchase_id = cur.fetchone()['id']

            for item in items:
                cur.execute("""
                    INSERT INTO purchase_items (purchase_id, product_id, quantity, price_per_unit, total)
                    VALUES (%s, %s, %s, %s, %s)
                """, (purchase_id, item['product_id'], item['quantity'], item['price_per_unit'], item['price_per_unit'] * item['quantity']))

                # Увеличиваем остаток на хабе (HUB_SELLER_ID)
                increase_seller_stock(
                    seller_id=HUB_SELLER_ID,
                    product_id=item['product_id'],
                    quantity=item['quantity'],
                    reason='purchase',
                    order_id=None
                )
            conn.commit()
            return purchase_id

def get_purchase(purchase_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM purchases WHERE id = %s", (purchase_id,))
            purchase = cur.fetchone()
            if purchase:
                cur.execute("""
                    SELECT pi.*, p.name
                    FROM purchase_items pi
                    JOIN products p ON pi.product_id = p.id
                    WHERE pi.purchase_id = %s
                """, (purchase_id,))
                items = cur.fetchall()
                purchase['items'] = items
            return purchase

def get_purchases_history(limit: int = 20):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.purchase_date, p.total, p.comment,
                       COALESCE(s.name, 'Администратор') as seller_name
                FROM purchases p
                LEFT JOIN sellers s ON p.seller_id = s.id
                ORDER BY p.purchase_date DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

# ========== Расширенные функции для админа ==========
def get_all_sellers_stock():
    """Возвращает остатки всех продавцов и хаба сгруппированно."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.name, p.id as product_id, p.name as product_name, ss.quantity
                FROM seller_stock ss
                JOIN sellers s ON ss.seller_id = s.id
                JOIN products p ON ss.product_id = p.id
                ORDER BY s.name, p.name
            """)
            return cur.fetchall()

def get_total_payments_stats():
    """Общая сумма выплат и долгов по всем продавцам (кроме хаба)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Общая сумма всех подтверждённых выплат
            cur.execute("SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid FROM seller_payments WHERE status = 'confirmed'")
            total_paid = cur.fetchone()['total_paid']

            # Общий долг всех продавцов (кроме хаба) – сумма по price_seller
            cur.execute("""
                SELECT COALESCE(SUM(p.price_seller * (i->>'quantity')::int), 0) as total_debt
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.status = 'completed' AND o.stock_processed = TRUE
            """)
            total_debt = cur.fetchone()['total_debt']

            return total_paid, total_debt

def get_pending_payments():
    """Возвращает все неподтверждённые выплаты."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sp.*, s.name as seller_name
                FROM seller_payments sp
                JOIN sellers s ON sp.seller_id = s.id
                WHERE sp.status = 'pending'
                ORDER BY sp.created_at DESC
            """)
            return cur.fetchall()
