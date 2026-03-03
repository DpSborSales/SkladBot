# models.py
import json
import logging
from datetime import datetime
from database import get_db_connection
from config import HUB_SELLER_ID, ADMIN_ID

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

# ========== Товары и варианты ==========
def get_all_products():
    """Возвращает список всех продуктов с их вариантами"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM products ORDER BY name")
            products = cur.fetchall()
            for p in products:
                cur.execute("""
                    SELECT id, name, price, price_seller, weight_kg
                    FROM product_variants
                    WHERE product_id = %s
                    ORDER BY sort_order
                """, (p['id'],))
                p['variants'] = cur.fetchall()
            return products

def get_product_variants(product_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, price, price_seller, weight_kg
                FROM product_variants
                WHERE product_id = %s
                ORDER BY sort_order
            """, (product_id,))
            return cur.fetchall()

def get_variant(variant_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.*, p.name as product_name, p.id as product_id
                FROM product_variants v
                JOIN products p ON v.product_id = p.id
                WHERE v.id = %s
            """, (variant_id,))
            return cur.fetchone()

# ========== Остатки продавцов (в упаковках) ==========
def get_seller_stock(seller_id: int, variant_id: int = None):
    """Если variant_id не указан, возвращает все варианты (в том числе с нулевым остатком)"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if variant_id:
                cur.execute("""
                    SELECT quantity FROM seller_stock
                    WHERE seller_id = %s AND variant_id = %s
                """, (seller_id, variant_id))
                row = cur.fetchone()
                return row['quantity'] if row else 0
            else:
                # Получаем все варианты (кроме россыпи) и их остатки
                cur.execute("""
                    SELECT v.id as variant_id, v.name as variant_name,
                           p.id as product_id, p.name as product_name,
                           COALESCE(ss.quantity, 0) as quantity
                    FROM product_variants v
                    JOIN products p ON v.product_id = p.id
                    LEFT JOIN seller_stock ss ON ss.variant_id = v.id AND ss.seller_id = %s
                    WHERE v.name != 'Россыпь'
                    ORDER BY p.name, v.sort_order
                """, (seller_id,))
                return cur.fetchall()

def decrease_seller_stock(seller_id: int, variant_id: int, quantity: int, reason: str, order_id: int = None):
    if quantity <= 0:
        return
    variant = get_variant(variant_id)
    if not variant:
        raise ValueError(f"Variant {variant_id} not found")
    product_id = variant['product_id']

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Обновляем существующую запись
            cur.execute(
                "UPDATE seller_stock SET quantity = quantity - %s WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (quantity, seller_id, product_id, variant_id)
            )
            # Если обновление не затронуло строки, значит записи не было – создаём с отрицательным значением
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO seller_stock (seller_id, product_id, variant_id, quantity)
                    VALUES (%s, %s, %s, %s)
                """, (seller_id, product_id, variant_id, -quantity))

            cur.execute("""
                INSERT INTO stock_movements (product_id, variant_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (product_id, variant_id, -quantity, reason, order_id, seller_id))
            conn.commit()

def increase_seller_stock(seller_id: int, variant_id: int, quantity: int, reason: str, order_id: int = None):
    if quantity <= 0:
        return
    variant = get_variant(variant_id)
    if not variant:
        raise ValueError(f"Variant {variant_id} not found")
    product_id = variant['product_id']

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seller_stock (seller_id, product_id, variant_id, quantity)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (seller_id, product_id, variant_id)
                DO UPDATE SET quantity = seller_stock.quantity + EXCLUDED.quantity
            """, (seller_id, product_id, variant_id, quantity))
            cur.execute("""
                INSERT INTO stock_movements (product_id, variant_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (product_id, variant_id, quantity, reason, order_id, seller_id))
            conn.commit()

def get_negative_stock_summary(seller_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.name as product_name, v.name as variant_name, ss.quantity
                FROM seller_stock ss
                JOIN product_variants v ON ss.variant_id = v.id
                JOIN products p ON v.product_id = p.id
                WHERE ss.seller_id = %s AND ss.quantity < 0
                ORDER BY p.name, v.sort_order
            """, (seller_id,))
            return cur.fetchall()

# ========== Остатки на хабе (в кг) ==========
def get_hub_stock(product_id: int = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if product_id:
                cur.execute("SELECT quantity_kg FROM hub_stock WHERE product_id = %s", (product_id,))
                row = cur.fetchone()
                return row['quantity_kg'] if row else 0
            else:
                cur.execute("""
                    SELECT p.id, p.name, hs.quantity_kg
                    FROM hub_stock hs
                    JOIN products p ON hs.product_id = p.id
                    ORDER BY p.name
                """)
                return cur.fetchall()

def increase_hub_stock(product_id: int, quantity_kg: float, reason: str, order_id: int = None):
    if quantity_kg <= 0:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hub_stock (product_id, quantity_kg)
                VALUES (%s, %s)
                ON CONFLICT (product_id)
                DO UPDATE SET quantity_kg = hub_stock.quantity_kg + EXCLUDED.quantity_kg
            """, (product_id, quantity_kg))
            conn.commit()

def decrease_hub_stock(product_id: int, quantity_kg: float, reason: str, order_id: int = None):
    if quantity_kg <= 0:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hub_stock SET quantity_kg = quantity_kg - %s WHERE product_id = %s",
                (quantity_kg, product_id)
            )
            conn.commit()

# ========== Заказы ==========
def get_order_by_number(order_number: str):
    logger.info(f"🔍 get_order_by_number: ищем заказ с номером '{order_number}'")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_number = %s", (order_number,))
            order = cur.fetchone()
            if order:
                order['contact'] = parse_contact(order['contact'])
                order['items'] = parse_items(order['items'])
            return order

def mark_order_as_processed(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET stock_processed = TRUE WHERE id = %s", (order_id,))
            conn.commit()

# ========== Заявки на перемещение (с поддержкой нескольких позиций) ==========

def create_transfer_request(from_seller_id: int, to_seller_id: int) -> int:
    """Создаёт заголовок заявки и возвращает её ID"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transfer_requests (from_seller_id, to_seller_id, status)
                VALUES (%s, %s, 'pending')
                RETURNING id
            """, (from_seller_id, to_seller_id))
            request_id = cur.fetchone()['id']
            conn.commit()
            return request_id

def add_transfer_request_item(request_id: int, variant_id: int, quantity: int):
    """Добавляет позицию в заявку"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transfer_request_items (request_id, variant_id, quantity)
                VALUES (%s, %s, %s)
            """, (request_id, variant_id, quantity))
            conn.commit()

def get_transfer_request_with_items(request_id: int):
    """Возвращает заявку вместе со всеми позициями"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM transfer_requests WHERE id = %s", (request_id,))
            request = cur.fetchone()
            if not request:
                return None
            cur.execute("""
                SELECT tri.*, v.name as variant_name, p.name as product_name
                FROM transfer_request_items tri
                JOIN product_variants v ON tri.variant_id = v.id
                JOIN products p ON v.product_id = p.id
                WHERE tri.request_id = %s
            """, (request_id,))
            items = cur.fetchall()
            request['items'] = items
            return request

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
    """Долг продавца перед админом.
       Для обычных продавцов: (продажи по цене продавца) + (прямые продажи по цене продавца) - выплаты.
       Для кладовщика (HUB_SELLER_ID): (продажи по цене покупателя) + (прямые продажи по цене покупателя) - выплаты.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Определяем ценовое поле в зависимости от продавца
            if seller_id == HUB_SELLER_ID:
                price_field_orders = "v.price"
                price_field_direct = "(i->>'price')::int"
            else:
                price_field_orders = "v.price_seller"
                price_field_direct = "(i->>'price_seller')::int"

            # Продажи через заказы – только подтверждённые (stock_processed)
            cur.execute(f"""
                SELECT COALESCE(SUM({price_field_orders} * (i->>'quantity')::int), 0) as total_sales
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN product_variants v ON (i->>'variantId')::int = v.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_sales = cur.fetchone()['total_sales']

            # Прямые продажи
            cur.execute(f"""
                SELECT COALESCE(SUM({price_field_direct} * (i->>'quantity')::int), 0) as total_direct
                FROM direct_sales ds, jsonb_array_elements(ds.items) i
                WHERE ds.seller_id = %s
            """, (seller_id,))
            total_direct = cur.fetchone()['total_direct']

            # Выплаты (только подтверждённые)
            cur.execute("""
                SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid
                FROM seller_payments
                WHERE seller_id = %s AND status = 'confirmed'
            """, (seller_id,))
            total_paid = cur.fetchone()['total_paid']

            debt = total_sales + total_direct - total_paid
            return debt, total_sales, total_paid, total_direct

def get_seller_profit(seller_id: int):
    """Прибыль продавца = сумма продаж по цене покупателя - сумма продаж по цене продавца.
       Для кладовщика (HUB_SELLER_ID) обе суммы равны (используется цена покупателя), поэтому прибыль = 0.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Если это кладовщик, его прибыль всегда 0 (он не зарабатывает наценку)
            if seller_id == HUB_SELLER_ID:
                return 0, 0, 0

            # Продажи по цене покупателя (через заказы)
            cur.execute("""
                SELECT COALESCE(SUM(v.price * (i->>'quantity')::int), 0) as total_buyer
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN product_variants v ON (i->>'variantId')::int = v.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_buyer = cur.fetchone()['total_buyer']

            # Продажи по цене продавца (те же заказы)
            cur.execute("""
                SELECT COALESCE(SUM(v.price_seller * (i->>'quantity')::int), 0) as total_seller
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN product_variants v ON (i->>'variantId')::int = v.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_seller = cur.fetchone()['total_seller']

            # Добавляем прямые продажи (для прибыли – тоже разница между ценой покупателя и продавца)
            cur.execute("""
                SELECT COALESCE(SUM((i->>'price')::int * (i->>'quantity')::int), 0) as direct_buyer,
                       COALESCE(SUM((i->>'price_seller')::int * (i->>'quantity')::int), 0) as direct_seller
                FROM direct_sales ds, jsonb_array_elements(ds.items) i
                WHERE ds.seller_id = %s
            """, (seller_id,))
            row = cur.fetchone()
            total_buyer += row['direct_buyer']
            total_seller += row['direct_seller']

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
                cur.execute("""
                    UPDATE seller_payments SET status = %s, confirmed_amount = %s, processed_at = %s
                    WHERE id = %s
                """, (status, confirmed_amount, datetime.utcnow().isoformat(), payment_id))
            else:
                cur.execute("""
                    UPDATE seller_payments SET status = %s, processed_at = %s
                    WHERE id = %s
                """, (status, datetime.utcnow().isoformat(), payment_id))
            conn.commit()

# ========== Прямые продажи ==========
def create_direct_sale(seller_id: int, items: list, total: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Преобразуем items в JSON с нужными полями
            items_json = json.dumps(items)
            cur.execute("""
                INSERT INTO direct_sales (seller_id, items, total)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (seller_id, items_json, total))
            sale_id = cur.fetchone()['id']
            conn.commit()
            return sale_id

# ========== Закупки (только для админа) ==========
def create_purchase(seller_id: int, items: list, total: int, comment: str = "") -> int:
    """
    items: список словарей с полями:
        product_id, quantity_kg (сколько кг закуплено), price_per_kg
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
                    INSERT INTO purchase_items (purchase_id, product_id, quantity_kg, price_per_kg, total)
                    VALUES (%s, %s, %s, %s, %s)
                """, (purchase_id, item['product_id'], item['quantity_kg'], item['price_per_kg'], item['quantity_kg'] * item['price_per_kg']))
                increase_hub_stock(item['product_id'], item['quantity_kg'], 'purchase', None)
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

# ========== Фасовка ==========
def create_packing_operation(product_id: int, variant_id: int, quantity_packs: int, created_by: int):
    """Создаёт операцию фасовки: списывает кг с хаба и добавляет упаковки продавцу-кладовщику"""
    variant = get_variant(variant_id)
    if not variant:
        raise ValueError("Variant not found")
    weight_used = quantity_packs * variant['weight_kg']

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Проверяем достаточно ли кг на хабе
            cur.execute("SELECT quantity_kg FROM hub_stock WHERE product_id = %s", (product_id,))
            row = cur.fetchone()
            if not row or row['quantity_kg'] < weight_used:
                raise ValueError("Недостаточно товара на хабе")

            # Уменьшаем хаб
            cur.execute(
                "UPDATE hub_stock SET quantity_kg = quantity_kg - %s WHERE product_id = %s",
                (weight_used, product_id)
            )
            # Увеличиваем остатки кладовщика (HUB_SELLER_ID) по данному варианту
            cur.execute("""
                INSERT INTO seller_stock (seller_id, product_id, variant_id, quantity)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (seller_id, product_id, variant_id)
                DO UPDATE SET quantity = seller_stock.quantity + EXCLUDED.quantity
            """, (HUB_SELLER_ID, product_id, variant_id, quantity_packs))

            # Записываем операцию
            cur.execute("""
                INSERT INTO packing_operations (product_id, variant_id, quantity_packs, weight_used, created_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (product_id, variant_id, quantity_packs, weight_used, created_by))
            op_id = cur.fetchone()['id']
            conn.commit()
            return op_id

# ========== Расширенные функции для админа ==========
def get_all_sellers_stock():
    """Возвращает остатки всех продавцов сгруппированно"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id as seller_id, s.name as seller_name,
                       p.id as product_id, p.name as product_name,
                       v.id as variant_id, v.name as variant_name,
                       ss.quantity
                FROM seller_stock ss
                JOIN sellers s ON ss.seller_id = s.id
                JOIN product_variants v ON ss.variant_id = v.id
                JOIN products p ON v.product_id = p.id
                ORDER BY s.name, p.name, v.sort_order
            """)
            return cur.fetchall()

def get_total_payments_stats():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid FROM seller_payments WHERE status = 'confirmed'")
            total_paid = cur.fetchone()['total_paid']
            # Общий долг всех продавцов (сумма их долгов) – для кладовщика уже учтено в get_seller_debt
            cur.execute("""
                SELECT COALESCE(SUM(v.price_seller * (i->>'quantity')::int), 0) as total_debt
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN product_variants v ON (i->>'variantId')::int = v.id
                WHERE o.status = 'completed' AND o.stock_processed = TRUE
            """)
            total_debt = cur.fetchone()['total_debt']
            return total_paid, total_debt

def get_pending_payments():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sp.id, sp.seller_id, sp.amount, sp.status, sp.created_at, s.name as seller_name
                FROM seller_payments sp
                JOIN sellers s ON sp.seller_id = s.id
                WHERE sp.status = 'pending'
                ORDER BY sp.created_at DESC
            """)
            rows = cur.fetchall()
            for row in rows:
                if row['created_at']:
                    row['created_at'] = str(row['created_at'])
            return rows
