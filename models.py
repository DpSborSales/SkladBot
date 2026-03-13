# models.py
import json
import logging
import math
from datetime import datetime
from database import get_db_connection
from config import HUB_SELLER_ID, ADMIN_ID

logger = logging.getLogger(__name__)

def round_up_to_tens(value):
    """Округляет число вверх до десятков"""
    return math.ceil(value / 10) * 10

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
    """Возвращает список всех продуктов с их вариантами и рассчитанными ценами продавца"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, purchase_price_kg FROM products ORDER BY name")
            products = cur.fetchall()
            for p in products:
                cur.execute("""
                    SELECT 
                        id, name, price, weight_kg, packaging_cost
                    FROM product_variants
                    WHERE product_id = %s
                    ORDER BY sort_order
                """, (p['id'],))
                variants = cur.fetchall()
                
                # Рассчитываем price_seller для каждого варианта
                for v in variants:
                    base_cost = (p['purchase_price_kg'] * v['weight_kg']) + (v['packaging_cost'] or 0)
                    avg_price = (v['price'] + base_cost) / 2
                    v['price_seller'] = round_up_to_tens(avg_price)
                
                p['variants'] = variants
            return products

def get_product_variants(product_id: int):
    """Возвращает варианты товара с рассчитанными ценами продавца"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Получаем закупочную цену товара
            cur.execute("SELECT purchase_price_kg FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            if not product:
                return []
            
            purchase_price_kg = product['purchase_price_kg']
            
            cur.execute("""
                SELECT id, name, price, weight_kg, packaging_cost
                FROM product_variants
                WHERE product_id = %s
                ORDER BY sort_order
            """, (product_id,))
            variants = cur.fetchall()
            
            # Рассчитываем price_seller для каждого варианта
            for v in variants:
                base_cost = (purchase_price_kg * v['weight_kg']) + (v['packaging_cost'] or 0)
                avg_price = (v['price'] + base_cost) / 2
                v['price_seller'] = round_up_to_tens(avg_price)
            
            return variants

def get_variant(variant_id: int):
    """Возвращает информацию о варианте товара с рассчитанной ценой продавца"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    v.*, 
                    p.name as product_name, 
                    p.id as product_id,
                    p.purchase_price_kg
                FROM product_variants v
                JOIN products p ON v.product_id = p.id
                WHERE v.id = %s
            """, (variant_id,))
            variant = cur.fetchone()
            
            if variant:
                # Рассчитываем price_seller
                base_cost = (variant['purchase_price_kg'] * variant['weight_kg']) + (variant['packaging_cost'] or 0)
                avg_price = (variant['price'] + base_cost) / 2
                variant['price_seller'] = round_up_to_tens(avg_price)
            
            return variant

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
                    SELECT 
                        v.id as variant_id, 
                        v.name as variant_name,
                        p.id as product_id, 
                        p.name as product_name,
                        p.purchase_price_kg,
                        v.price,
                        v.weight_kg,
                        v.packaging_cost,
                        COALESCE(ss.quantity, 0) as quantity,
                        s.name as seller_name
                    FROM product_variants v
                    JOIN products p ON v.product_id = p.id
                    LEFT JOIN seller_stock ss ON ss.variant_id = v.id AND ss.seller_id = %s
                    LEFT JOIN sellers s ON s.id = %s
                    WHERE v.name != 'Россыпь'
                    ORDER BY p.name, v.sort_order
                """, (seller_id, seller_id))
                
                stocks = cur.fetchall()
                
                # Добавляем рассчитанную цену продавца
                for row in stocks:
                    base_cost = (row['purchase_price_kg'] * row['weight_kg']) + (row['packaging_cost'] or 0)
                    avg_price = (row['price'] + base_cost) / 2
                    row['price_seller'] = round_up_to_tens(avg_price)
                
                return stocks

def get_seller_stock_with_check(seller_id: int, variant_id: int) -> int:
    """Возвращает текущий остаток и проверяет наличие записи"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT quantity FROM seller_stock 
                WHERE seller_id = %s AND variant_id = %s
            """, (seller_id, variant_id))
            row = cur.fetchone()
            return row['quantity'] if row else 0

def decrease_seller_stock(seller_id: int, variant_id: int, quantity: int, reason: str, order_id: int = None):
    """Уменьшает остаток товара у продавца (списание)"""
    if quantity <= 0:
        return
    variant = get_variant(variant_id)
    if not variant:
        raise ValueError(f"Variant {variant_id} not found")
    product_id = variant['product_id']

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Проверяем текущий остаток перед списанием (для логирования)
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (seller_id, product_id, variant_id)
            )
            current = cur.fetchone()
            current_qty = current['quantity'] if current else 0
            
            logger.info(f"💰 decrease_seller_stock: seller={seller_id}, variant={variant_id}, "
                       f"product={product_id}, current={current_qty}, decrease_by={quantity}, reason={reason}")
            
            # Пытаемся обновить существующую запись
            cur.execute(
                "UPDATE seller_stock SET quantity = quantity - %s WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (quantity, seller_id, product_id, variant_id)
            )
            # Если запись не существовала, создаём с отрицательным значением
            if cur.rowcount == 0:
                logger.info(f"💰 decrease_seller_stock: создаём новую запись с отрицательным значением -{quantity}")
                cur.execute("""
                    INSERT INTO seller_stock (seller_id, product_id, variant_id, quantity)
                    VALUES (%s, %s, %s, %s)
                """, (seller_id, product_id, variant_id, -quantity))

            # Записываем движение (отрицательное изменение)
            cur.execute("""
                INSERT INTO stock_movements (product_id, variant_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (product_id, variant_id, -quantity, reason, order_id, seller_id))
            conn.commit()
            
            # Проверяем результат после списания
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (seller_id, product_id, variant_id)
            )
            new = cur.fetchone()
            new_qty = new['quantity'] if new else 0
            logger.info(f"💰 decrease_seller_stock: после операции new_quantity={new_qty}")

def increase_seller_stock(seller_id: int, variant_id: int, quantity: int, reason: str, order_id: int = None):
    """Увеличивает остаток товара у продавца (поступление)"""
    if quantity <= 0:
        return
    variant = get_variant(variant_id)
    if not variant:
        raise ValueError(f"Variant {variant_id} not found")
    product_id = variant['product_id']

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Проверяем текущий остаток перед добавлением (для логирования)
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (seller_id, product_id, variant_id)
            )
            current = cur.fetchone()
            current_qty = current['quantity'] if current else 0
            
            logger.info(f"💰 increase_seller_stock: seller={seller_id}, variant={variant_id}, "
                       f"product={product_id}, current={current_qty}, increase_by={quantity}, reason={reason}")
            
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
            
            # Проверяем результат после добавления
            cur.execute(
                "SELECT quantity FROM seller_stock WHERE seller_id = %s AND product_id = %s AND variant_id = %s",
                (seller_id, product_id, variant_id)
            )
            new = cur.fetchone()
            new_qty = new['quantity'] if new else 0
            logger.info(f"💰 increase_seller_stock: после операции new_quantity={new_qty}")

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

def update_order_total(order_id: int, new_total: int):
    """Обновляет общую сумму заказа"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET total = %s WHERE id = %s",
                (new_total, order_id)
            )
            conn.commit()

# ========== Генерация номера заказа ==========
def generate_order_number(seller_id: int, delivery_type: str = None) -> str:
    """Генерирует номер заказа на основе префикса продавца (макс. 3 символа)"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Получаем префикс продавца
            cur.execute("SELECT seller_prefix FROM sellers WHERE id = %s", (seller_id,))
            result = cur.fetchone()
            
            if not result or not result['seller_prefix']:
                # Если префикс не задан, используем первую букву имени
                cur.execute("SELECT name FROM sellers WHERE id = %s", (seller_id,))
                name = cur.fetchone()['name']
                # Берём первый символ имени (может быть кириллица)
                prefix = name[0].upper()
                # Ограничиваем до 3 символов
                if len(prefix) > 3:
                    prefix = prefix[:3]
            else:
                prefix = result['seller_prefix']
                # Убеждаемся, что префикс не длиннее 3 символов
                if len(prefix) > 3:
                    prefix = prefix[:3]
            
            # Получаем последний номер для этого префикса
            cur.execute("""
                SELECT order_number FROM orders 
                WHERE order_number LIKE %s 
                ORDER BY id DESC LIMIT 1
            """, (prefix + '%',))
            
            last = cur.fetchone()
            if last:
                # Извлекаем числовую часть (всё после префикса)
                num_str = last['order_number'][len(prefix):]
                if num_str.isdigit():
                    new_num = int(num_str) + 1
                else:
                    new_num = 1
            else:
                new_num = 1
            
            return f"{prefix}{new_num}"

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

def update_transfer_request_status_atomic(request_id: int, status: str) -> bool:
    """Атомарно обновляет статус заявки, возвращает True если обновление выполнено"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Пытаемся обновить только pending заявки
            cur.execute("""
                UPDATE transfer_requests 
                SET status = %s, processed_at = %s 
                WHERE id = %s AND status = 'pending'
                RETURNING id
            """, (status, datetime.utcnow().isoformat(), request_id))
            
            # Если обновление затронуло строку - значит успех
            updated = cur.fetchone() is not None
            conn.commit()
            return updated

def get_pending_transfer_requests_for_hub():
    """Возвращает все заявки на перемещение, где кладовщик является отправителем и статус 'pending'"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tr.id, tr.from_seller_id, tr.to_seller_id, tr.status,
                       tri.variant_id, tri.quantity,
                       v.name as variant_name, p.name as product_name
                FROM transfer_requests tr
                JOIN transfer_request_items tri ON tr.id = tri.request_id
                JOIN product_variants v ON tri.variant_id = v.id
                JOIN products p ON v.product_id = p.id
                WHERE tr.from_seller_id = %s AND tr.status = 'pending'
                ORDER BY tr.created_at DESC
            """, (HUB_SELLER_ID,))
            rows = cur.fetchall()
            requests = {}
            for row in rows:
                req_id = row['id']
                if req_id not in requests:
                    requests[req_id] = {
                        'id': req_id,
                        'from_seller_id': row['from_seller_id'],
                        'to_seller_id': row['to_seller_id'],
                        'status': row['status'],
                        'items': []
                    }
                requests[req_id]['items'].append({
                    'variant_id': row['variant_id'],
                    'quantity': row['quantity'],
                    'variant_name': row['variant_name'],
                    'product_name': row['product_name']
                })
            return list(requests.values())

def get_all_pending_transfer_requests():
    """Возвращает все заявки на перемещение со статусом 'pending' (для администратора)"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tr.id, tr.from_seller_id, tr.to_seller_id, tr.status,
                       tri.variant_id, tri.quantity,
                       v.name as variant_name, p.name as product_name,
                       fs.name as from_seller_name, ts.name as to_seller_name
                FROM transfer_requests tr
                JOIN transfer_request_items tri ON tr.id = tri.request_id
                JOIN product_variants v ON tri.variant_id = v.id
                JOIN products p ON v.product_id = p.id
                JOIN sellers fs ON tr.from_seller_id = fs.id
                JOIN sellers ts ON tr.to_seller_id = ts.id
                WHERE tr.status = 'pending'
                ORDER BY tr.created_at DESC
            """)
            rows = cur.fetchall()
            requests = {}
            for row in rows:
                req_id = row['id']
                if req_id not in requests:
                    requests[req_id] = {
                        'id': req_id,
                        'from_seller_id': row['from_seller_id'],
                        'from_seller_name': row['from_seller_name'],
                        'to_seller_id': row['to_seller_id'],
                        'to_seller_name': row['to_seller_name'],
                        'status': row['status'],
                        'items': []
                    }
                requests[req_id]['items'].append({
                    'variant_id': row['variant_id'],
                    'quantity': row['quantity'],
                    'variant_name': row['variant_name'],
                    'product_name': row['product_name']
                })
            return list(requests.values())

# ========== Расчёты с продавцами ==========
def get_seller_debt(seller_id: int):
    """Долг продавца перед админом.
       Для обычных продавцов: (продажи по цене продавца) + (прямые продажи по цене продавца) - выплаты.
       Для кладовщика (HUB_SELLER_ID): (продажи по цене покупателя) + (прямые продажи по цене покупателя) - выплаты.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if seller_id == HUB_SELLER_ID:
                price_field_orders = "(i->>'price')::int"
                price_field_direct = "(i->>'price')::int"
            else:
                price_field_orders = "(i->>'price_seller')::int"
                price_field_direct = "(i->>'price_seller')::int"

            # Продажи через заказы
            cur.execute(f"""
                SELECT COALESCE(SUM({price_field_orders} * (i->>'quantity')::int), 0) as total_sales
                FROM orders o, jsonb_array_elements(o.items) i
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

            # Выплаты
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
       Для кладовщика (HUB_SELLER_ID) прибыль считается по-другому.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if seller_id == HUB_SELLER_ID:
                # Для кладовщика считаем только продажи по цене покупателя (прибыль 0)
                cur.execute("""
                    SELECT COALESCE(SUM((i->>'price')::int * (i->>'quantity')::int), 0) as total_buyer
                    FROM orders o, jsonb_array_elements(o.items) i
                    WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
                """, (seller_id,))
                total_buyer = cur.fetchone()['total_buyer']

                cur.execute("""
                    SELECT COALESCE(SUM((i->>'price')::int * (i->>'quantity')::int), 0) as direct_buyer
                    FROM direct_sales ds, jsonb_array_elements(ds.items) i
                    WHERE ds.seller_id = %s
                """, (seller_id,))
                direct_buyer = cur.fetchone()['direct_buyer']
                total_buyer += direct_buyer
                return 0, total_buyer, 0

            # Для обычных продавцов
            # Продажи по цене покупателя (через заказы)
            cur.execute("""
                SELECT COALESCE(SUM((i->>'price')::int * (i->>'quantity')::int), 0) as total_buyer
                FROM orders o, jsonb_array_elements(o.items) i
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_buyer = cur.fetchone()['total_buyer']

            # Продажи по цене продавца (те же заказы)
            cur.execute("""
                SELECT COALESCE(SUM((i->>'price_seller')::int * (i->>'quantity')::int), 0) as total_seller
                FROM orders o, jsonb_array_elements(o.items) i
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_seller = cur.fetchone()['total_seller']

            # Прямые продажи (по цене покупателя и продавца)
            cur.execute("""
                SELECT 
                    COALESCE(SUM((i->>'price')::int * (i->>'quantity')::int), 0) as direct_buyer,
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
            # Все подтверждённые выплаты
            cur.execute("SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid FROM seller_payments WHERE status = 'confirmed'")
            total_paid = cur.fetchone()['total_paid']
            
            # Общий долг всех продавцов (сумма их долгов)
            # Сначала получаем всех продавцов (кроме администратора)
            cur.execute("SELECT id FROM sellers WHERE id != %s", (ADMIN_ID,))
            sellers = cur.fetchall()
            
            total_debt = 0
            for seller in sellers:
                debt, _, _, _ = get_seller_debt(seller['id'])
                total_debt += debt
            
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
