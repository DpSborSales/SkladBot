import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
PORT = int(os.getenv('PORT', 10000))
HUB_SELLER_ID = 5  # ID продавца-кладовщика (хаб)

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы обязательные переменные окружения")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://skladbot-rhoo.onrender.com')
WEBHOOK_URL = f"{BASE_URL}/webhook"

# Хранилище сессий редактирования и платежей
edit_sessions = {}
payment_sessions = {}

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

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

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

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

def get_all_products():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, price, price_seller FROM products ORDER BY name")
            return cur.fetchall()

def mark_order_as_processed(order_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET stock_processed = TRUE WHERE id = %s", (order_id,))
            conn.commit()

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ОСТАТКАМИ ====================

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

def send_negative_stock_warning(chat_id, seller_id):
    negatives = get_negative_stock_summary(seller_id)
    if not negatives:
        return
    lines = [f"• {row['name']}: {abs(row['quantity'])} упаковок" for row in negatives]
    summary = "\n".join(lines)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📦 Создать заявку на перемещение", callback_data="create_transfer_request"))
    bot.send_message(
        chat_id,
        f"⚠️ *Внимание!* Вы продали товаров больше, чем было на вашем складе.\n"
        f"Необходимо произвести перераспределение товаров на ваш склад.\n"
        f"Сейчас Ваши остатки ушли в минус:\n{summary}",
        parse_mode='Markdown',
        reply_markup=markup
    )

# ==================== ФУНКЦИИ ДЛЯ ЗАЯВОК НА ПЕРЕМЕЩЕНИЕ ====================

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

# ==================== ФУНКЦИИ ДЛЯ РАСЧЁТОВ С ПРОДАВЦАМИ ====================

def get_seller_debt(seller_id: int):
    """Возвращает текущий долг продавца перед администратором (сумма price_seller проданных товаров - сумма внесённых платежей)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Сумма всех продаж по цене продавца
            cur.execute("""
                SELECT COALESCE(SUM(p.price_seller * (i->>'quantity')::int), 0) as total_sales
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_sales = cur.fetchone()['total_sales']

            # Сумма всех выплат
            cur.execute("""
                SELECT COALESCE(SUM(confirmed_amount), 0) as total_paid
                FROM seller_payments
                WHERE seller_id = %s AND status = 'confirmed'
            """, (seller_id,))
            total_paid = cur.fetchone()['total_paid']

            debt = total_sales - total_paid
            return debt, total_sales, total_paid

def get_seller_profit(seller_id: int):
    """Возвращает чистую прибыль продавца (продажи по цене покупателя - продажи по цене продавца)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Сумма продаж по цене покупателя
            cur.execute("""
                SELECT COALESCE(SUM(p.price * (i->>'quantity')::int), 0) as total_buyer
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_buyer = cur.fetchone()['total_buyer']

            # Сумма продаж по цене продавца
            cur.execute("""
                SELECT COALESCE(SUM(p.price_seller * (i->>'quantity')::int), 0) as total_seller
                FROM orders o, jsonb_array_elements(o.items) i
                JOIN products p ON (i->>'productId')::int = p.id
                WHERE o.seller_id = %s AND o.status = 'completed' AND o.stock_processed = TRUE
            """, (seller_id,))
            total_seller = cur.fetchone()['total_seller']

            profit = total_buyer - total_seller
            return profit, total_buyer, total_seller

def create_payment_request(seller_id: int, amount: int):
    """Создаёт заявку на выплату."""
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

# ==================== КЛАВИАТУРЫ И ФОРМАТИРОВАНИЕ ====================

def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Ожидают обработки"))
    keyboard.add(types.KeyboardButton("📦 Мои остатки"), types.KeyboardButton("🔄 Заявка на перемещение"))
    keyboard.add(types.KeyboardButton("💰 Выплата админу"))
    return keyboard

def format_selected_summary(selected_items, product_names):
    if not selected_items:
        return ""
    lines = []
    for pid, qty in selected_items.items():
        name = product_names.get(pid, f"Товар {pid}")
        lines.append(f"{name} – {qty} упаковок")
    if len(lines) == 1:
        items_lines = lines[0] + "."
    else:
        items_lines = "\n".join([f"{line}," for line in lines[:-1]] + [f"{lines[-1]}."])
    return f"Вы продали:\n{items_lines}\n\nВерно?"

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ У вас нет доступа к этому боту.")
        return
    bot.send_message(
        message.chat.id,
        "👋 Добро пожаловать в складской учёт!\n\n"
        "Когда заказ завершён, вы получите уведомление для фиксации продажи.\n"
        "Используйте кнопки ниже для навигации.",
        reply_markup=main_keyboard()
    )

@bot.message_handler(commands=['stock'])
def handle_stock(message):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ У вас нет доступа к этому боту.")
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.name, ss.quantity
                FROM seller_stock ss
                JOIN products p ON ss.product_id = p.id
                WHERE ss.seller_id = %s
                ORDER BY p.name
            """, (seller['id'],))
            stocks = cur.fetchall()
    if not stocks:
        bot.reply_to(message, "📦 У вас нет товаров на складе.")
        return
    lines = []
    for row in stocks:
        if row['quantity'] > 0:
            lines.append(f"• {row['name']}: {row['quantity']} шт")
        elif row['quantity'] < 0:
            lines.append(f"• {row['name']}: {row['quantity']} шт (❗ минус)")
        else:
            lines.append(f"• {row['name']}: 0 шт")
    bot.reply_to(message, "📦 *Ваши остатки:*\n" + "\n".join(lines), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text == "📋 Ожидают обработки")
def handle_pending_orders(message):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ У вас нет доступа.")
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_number, items FROM orders
                WHERE seller_id = %s AND status = 'completed' AND stock_processed = FALSE
                ORDER BY id DESC
            """, (seller['id'],))
            pending = cur.fetchall()
    if not pending:
        bot.reply_to(message, "✅ Нет заказов, ожидающих обработки.")
        return
    for order in pending:
        order_number = order['order_number']
        items = order['items']
        items_text = "\n".join([f"• {item['name']}: {item['quantity']} шт" for item in items])
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{order_number}"),
            types.InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{order_number}")
        )
        bot.send_message(
            message.chat.id,
            f"📦 *Заказ {order_number}*\n\n{items_text}",
            parse_mode='Markdown',
            reply_markup=markup
        )

@bot.message_handler(func=lambda m: m.text == "📦 Мои остатки")
def handle_my_stock(message):
    handle_stock(message)

@bot.message_handler(func=lambda m: m.text == "🔄 Заявка на перемещение")
def handle_transfer_request_start(message):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ У вас нет доступа.")
        return
    products = get_all_products()
    if not products:
        bot.reply_to(message, "❌ Нет товаров в каталоге.")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for p in products:
        buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
    markup.add(*buttons)
    bot.send_message(
        message.chat.id,
        "🔄 *Создание заявки на перемещение*\n\nВыберите товар, который хотите получить:",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_prod_'))
def transfer_product_selected(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[2])
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.answer_callback_query(call.id, "❌ Ошибка доступа")
        return
    edit_sessions[user_id] = {
        'transfer_product_id': product_id,
        'chat_id': call.message.chat.id,
        'message_id': call.message.message_id
    }
    bot.edit_message_text(
        f"Введите количество для товара:",
        call.message.chat.id,
        call.message.message_id
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_transfer_quantity, user_id, product_id)
    bot.answer_callback_query(call.id)

def process_transfer_quantity(message, user_id, product_id):
    session = edit_sessions.pop(user_id, None)
    if not session:
        bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
        return
    try:
        qty = int(message.text.strip())
        if qty <= 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите положительное целое число.")
        return
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ Ошибка доступа.")
        return
    request_id = create_transfer_request(seller['id'], product_id, qty)
    hub_seller = get_seller_by_id(HUB_SELLER_ID)
    if hub_seller:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
            types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
        )
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), f"Товар {product_id}")
        try:
            bot.send_message(
                hub_seller['telegram_id'],
                f"📦 *Новая заявка на перемещение*\n\n"
                f"От: {seller['name']}\n"
                f"Товар: {product_name}\n"
                f"Количество: {qty}",
                parse_mode='Markdown',
                reply_markup=markup
            )
            logger.info(f"Заявка {request_id} отправлена кладовщику")
        except Exception as e:
            logger.error(f"Ошибка отправки кладовщику: {e}")
    bot.reply_to(message, f"✅ Заявка на перемещение создана (№{request_id}). Ожидайте подтверждения.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_approve_'))
def approve_transfer(call):
    user_id = call.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller or seller['id'] != HUB_SELLER_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для подтверждения.")
        return
    request_id = int(call.data.split('_')[2])
    req = get_transfer_request(request_id)
    if not req:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена")
        return
    if req['status'] != 'pending':
        bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
        return
    try:
        decrease_seller_stock(
            seller_id=HUB_SELLER_ID,
            product_id=req['product_id'],
            quantity=req['quantity'],
            reason='transfer_out',
            order_id=None
        )
        increase_seller_stock(
            seller_id=req['to_seller_id'],
            product_id=req['product_id'],
            quantity=req['quantity'],
            reason='transfer_in',
            order_id=None
        )
    except Exception as e:
        logger.error(f"Ошибка при перемещении: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при перемещении (возможно, недостаточно товара на хабе).", show_alert=True)
        return
    update_transfer_request_status(request_id, 'approved')
    seller_to = get_seller_by_id(req['to_seller_id'])
    if seller_to:
        try:
            products = get_all_products()
            product_name = next((p['name'] for p in products if p['id'] == req['product_id']), f"Товар {req['product_id']}")
            bot.send_message(
                seller_to['telegram_id'],
                f"✅ Ваша заявка на перемещение (№{request_id}) подтверждена!\n"
                f"Товар: {product_name}\n"
                f"Количество: {req['quantity']}"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления продавца: {e}")
    bot.edit_message_text(
        f"✅ Заявка {request_id} подтверждена, перемещение выполнено.",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id, "✅ Заявка подтверждена")

@bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_reject_'))
def reject_transfer(call):
    user_id = call.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller or seller['id'] != HUB_SELLER_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав.")
        return
    request_id = int(call.data.split('_')[2])
    req = get_transfer_request(request_id)
    if not req:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена")
        return
    if req['status'] != 'pending':
        bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
        return
    update_transfer_request_status(request_id, 'rejected')
    seller_to = get_seller_by_id(req['to_seller_id'])
    if seller_to:
        try:
            products = get_all_products()
            product_name = next((p['name'] for p in products if p['id'] == req['product_id']), f"Товар {req['product_id']}")
            bot.send_message(
                seller_to['telegram_id'],
                f"❌ Ваша заявка на перемещение (№{request_id}) отклонена кладовщиком.\n"
                f"Товар: {product_name}\n"
                f"Количество: {req['quantity']}"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления продавца: {e}")
    bot.edit_message_text(
        f"❌ Заявка {request_id} отклонена.",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id, "✅ Заявка отклонена")

# ==================== ОБРАБОТЧИК ВЫПЛАТ ====================

@bot.message_handler(func=lambda m: m.text == "💰 Выплата админу")
def handle_payment(message):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ У вас нет доступа.")
        return
    debt, total_sales, total_paid = get_seller_debt(seller['id'])
    profit, total_buyer, total_seller = get_seller_profit(seller['id'])
    msg = (
        f"💰 *Ваш расчётный счёт*\n\n"
        f"Вы должны перевести Админу: *{debt} руб.*\n"
        f"___________________________________________\n"
        f"Ваша чистая прибыль за всё время: *{profit} руб.*"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Произвести выплату", callback_data="make_payment"))
    bot.send_message(message.chat.id, msg, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "make_payment")
def make_payment(call):
    user_id = call.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.answer_callback_query(call.id, "❌ Ошибка доступа")
        return
    debt, _, _ = get_seller_debt(seller['id'])
    bot.edit_message_text(
        f"💳 Ваш долг: *{debt} руб.*\n\nВведите сумму, которую передаёте Админу:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_payment_amount, seller['id'], call.message.chat.id)
    bot.answer_callback_query(call.id)

def process_payment_amount(message, seller_id, original_chat_id):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите положительное целое число.")
        return
    payment_id = create_payment_request(seller_id, amount)
    seller = get_seller_by_id(seller_id)
    debt, _, _ = get_seller_debt(seller_id)
    # Отправляем админу
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"payment_confirm_{payment_id}_{amount}"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"payment_edit_{payment_id}")
    )
    try:
        bot.send_message(
            ADMIN_ID,
            f"💸 *Запрос на выплату*\n\n"
            f"Продавец: {seller['name']}\n"
            f"Долг: {debt} руб.\n"
            f"Передаёт: {amount} руб.\n\n"
            f"Всё верно?",
            parse_mode='Markdown',
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Ошибка отправки админу: {e}")
        bot.reply_to(message, "❌ Не удалось уведомить администратора.")
        return
    bot.reply_to(message, f"✅ Запрос на выплату {amount} руб. отправлен администратору. Ожидайте подтверждения.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('payment_confirm_'))
def payment_confirm(call):
    user_id = call.from_user.id
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав.")
        return
    parts = call.data.split('_')
    payment_id = int(parts[2])
    amount = int(parts[3])
    payment = get_payment_request(payment_id)
    if not payment:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена")
        return
    if payment['status'] != 'pending':
        bot.answer_callback_query(call.id, f"✅ Заявка уже {payment['status']}")
        return
    # Подтверждаем
    update_payment_status(payment_id, 'confirmed', confirmed_amount=amount)
    # Уведомляем продавца
    seller = get_seller_by_id(payment['seller_id'])
    if seller:
        debt, _, _ = get_seller_debt(payment['seller_id'])
        try:
            bot.send_message(
                seller['telegram_id'],
                f"✅ Админ подтвердил получение *{amount} руб.*\n"
                f"Ваш долг составляет *{debt} руб.*",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления продавца: {e}")
    bot.edit_message_text(
        f"✅ Вы подтвердили получение {amount} руб. от продавца.",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id, "✅ Подтверждено")

@bot.callback_query_handler(func=lambda call: call.data.startswith('payment_edit_'))
def payment_edit(call):
    user_id = call.from_user.id
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав.")
        return
    payment_id = int(call.data.split('_')[2])
    payment = get_payment_request(payment_id)
    if not payment:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена")
        return
    if payment['status'] != 'pending':
        bot.answer_callback_query(call.id, f"✅ Заявка уже {payment['status']}")
        return
    bot.edit_message_text(
        f"✏️ Введите сумму, которую вы реально получили:",
        call.message.chat.id,
        call.message.message_id
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_edit_payment, payment_id, call.message.chat.id)
    bot.answer_callback_query(call.id)

def process_edit_payment(message, payment_id, original_chat_id):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите положительное целое число.")
        return
    payment = get_payment_request(payment_id)
    if not payment:
        bot.reply_to(message, "❌ Заявка не найдена")
        return
    # Обновляем сумму и подтверждаем
    update_payment_status(payment_id, 'confirmed', confirmed_amount=amount)
    seller = get_seller_by_id(payment['seller_id'])
    if seller:
        debt, _, _ = get_seller_debt(payment['seller_id'])
        try:
            bot.send_message(
                seller['telegram_id'],
                f"✅ Админ подтвердил получение *{amount} руб.*\n"
                f"Ваш долг составляет *{debt} руб.*",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления продавца: {e}")
    bot.reply_to(message, f"✅ Вы подтвердили получение {amount} руб. от продавца.")

# ==================== ОБРАБОТЧИКИ ПОДТВЕРЖДЕНИЯ И РЕДАКТИРОВАНИЯ ЗАКАЗОВ ====================

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_'))
def handle_confirm(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"✅ Нажата кнопка подтверждения заказа {order_num}")

    order = get_order_by_number(order_num)
    if not order:
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
        return

    if order.get('stock_processed'):
        bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
        return

    # Списываем товары
    for item in order['items']:
        decrease_seller_stock(
            seller_id=seller['id'],
            product_id=item['productId'],
            quantity=item['quantity'],
            reason='sale',
            order_id=order['id']
        )

    mark_order_as_processed(order['id'])

    bot.answer_callback_query(call.id, "✅ Продажа зафиксирована")
    bot.edit_message_text(
        f"✅ Заказ {order_num} проведён.",
        call.message.chat.id,
        call.message.message_id
    )

    # Проверяем отрицательные остатки и отправляем предупреждение
    send_negative_stock_warning(call.message.chat.id, seller['id'])

@bot.callback_query_handler(func=lambda call: call.data.startswith('edit_'))
def handle_edit(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"✏️ Нажата кнопка редактирования заказа {order_num}")

    order = get_order_by_number(order_num)
    if not order:
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
        return

    if order.get('stock_processed'):
        bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
        return

    products = get_all_products()
    if not products:
        bot.answer_callback_query(call.id, "❌ Нет товаров в каталоге")
        return

    edit_sessions[user_id] = {
        'order_number': order_num,
        'original_items': {item['productId']: item['quantity'] for item in order['items']},
        'selected_items': {},
        'message_id': call.message.message_id,
        'chat_id': call.message.chat.id
    }
    logger.info(f"✅ Сессия редактирования создана для заказа {order_num}")

    show_product_selection(user_id)

def show_product_selection(user_id):
    session = edit_sessions.get(user_id)
    if not session:
        return

    products = get_all_products()
    product_names = {p['id']: p['name'] for p in products}
    summary = format_selected_summary(session['selected_items'], product_names)

    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for p in products:
        buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"selprod_{session['order_number']}_{p['id']}"))
    markup.add(*buttons)
    markup.row(types.InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{session['order_number']}"))

    text = f"✏️ *Редактирование заказа {session['order_number']}*\n\n"
    if summary:
        text += summary + "\n\n"
    text += "Выберите товар, чтобы указать проданное количество:"

    bot.edit_message_text(
        text,
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )
    logger.info(f"Показано меню выбора товара для заказа {session['order_number']}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('selprod_'))
def select_product(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    order_num = parts[1]
    product_id = int(parts[2])
    logger.info(f"🔘 Выбран товар {product_id} для заказа {order_num}")

    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

    bot.edit_message_text(
        f"Введите количество для товара *{product_name}*:",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, order_num, product_id)
    bot.answer_callback_query(call.id)

def process_quantity_input(message, user_id, order_num, product_id):
    logger.info(f"📝 Ввод количества для товара {product_id}, заказ {order_num}")
    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.reply_to(message, "❌ Сессия редактирования истекла. Начните заново.")
        return

    try:
        qty = int(message.text.strip())
        if qty < 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите целое неотрицательное число.")
        show_product_selection(user_id)
        return

    session['selected_items'][product_id] = qty
    logger.info(f"✅ Количество для товара {product_id} установлено: {qty}")

    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"conf_{order_num}_{product_id}"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"change_{order_num}_{product_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{order_num}")
    )
    bot.send_message(
        session['chat_id'],
        f"*Заказ {order_num}*\nВы продали *{product_name}* – *{qty}* упаковок, верно?",
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('conf_'))
def confirm_item(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    order_num = parts[1]
    product_id = int(parts[2])
    logger.info(f"✅ Подтверждён товар {product_id} для заказа {order_num}")

    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    bot.delete_message(session['chat_id'], call.message.message_id)
    show_product_selection(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('change_'))
def change_item(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    order_num = parts[1]
    product_id = int(parts[2])
    logger.info(f"✏️ Изменение товара {product_id} для заказа {order_num}")

    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    bot.delete_message(session['chat_id'], call.message.message_id)
    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
    bot.send_message(
        session['chat_id'],
        f"Введите новое количество для товара *{product_name}*:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, order_num, product_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def cancel_item(call):
    user_id = call.from_user.id
    parts = call.data.split('_')
    order_num = parts[1]
    logger.info(f"❌ Отмена выбора товара для заказа {order_num}")

    session = edit_sessions.get(user_id)
    if session and session['order_number'] == order_num:
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('finish_'))
def finish_edit(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"🏁 Завершение редактирования заказа {order_num}")

    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    if not session['selected_items']:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Без изменений", callback_data=f"nochanges_{order_num}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"editcancel_{order_num}")
        )
        bot.edit_message_text(
            f"*Заказ {order_num}*\n\nВы не добавили ни одного товара. Подтвердить заказ без изменений?",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return

    products = get_all_products()
    product_names = {p['id']: p['name'] for p in products}
    lines = []
    for pid, qty in session['selected_items'].items():
        name = product_names.get(pid, f"Товар {pid}")
        lines.append(f"• {name}: {qty} упаковок")
    summary = "\n".join(lines)

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"apply_{order_num}"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"editagain_{order_num}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"editcancel_{order_num}")
    )
    bot.edit_message_text(
        f"*Заказ {order_num}*\n\n"
        f"*Вы продали:*\n{summary}\n\n"
        "Всё верно?",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('apply_'))
def apply_edit(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"✅ Применение изменений для заказа {order_num}")

    session = edit_sessions.pop(user_id, None)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    order = get_order_by_number(order_num)
    if not order:
        logger.error(f"apply_edit: заказ {order_num} не найден в базе")
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
        return

    if order.get('stock_processed'):
        bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
        return

    selected = session['selected_items']
    if not selected:
        bot.answer_callback_query(call.id, "❌ Нет товаров для списания")
        return

    # Списываем
    for product_id, qty in selected.items():
        if qty > 0:
            decrease_seller_stock(
                seller_id=seller['id'],
                product_id=product_id,
                quantity=qty,
                reason='sale',
                order_id=order['id']
            )
            logger.info(f"✅ Списано {qty} ед. товара {product_id}")

    mark_order_as_processed(order['id'])
    logger.info(f"✅ Заказ {order_num} обработан, списано товаров: {len(selected)}")

    bot.edit_message_text(
        f"✅ Заказ {order_num} обработан.",
        session['chat_id'],
        session['message_id']
    )

    # Проверяем отрицательные остатки и отправляем предупреждение
    send_negative_stock_warning(session['chat_id'], seller['id'])

@bot.callback_query_handler(func=lambda call: call.data.startswith('nochanges_'))
def no_changes(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"✅ Подтверждение заказа {order_num} без изменений")

    session = edit_sessions.pop(user_id, None)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    order = get_order_by_number(order_num)
    if not order:
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
        return

    if order.get('stock_processed'):
        bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
        return

    # Списываем исходные количества
    for item in order['items']:
        decrease_seller_stock(
            seller_id=seller['id'],
            product_id=item['productId'],
            quantity=item['quantity'],
            reason='sale',
            order_id=order['id']
        )

    mark_order_as_processed(order['id'])

    bot.edit_message_text(
        f"✅ Заказ {order_num} проведён без изменений.",
        session['chat_id'],
        session['message_id']
    )

    # Проверяем отрицательные остатки и отправляем предупреждение
    send_negative_stock_warning(session['chat_id'], seller['id'])

@bot.callback_query_handler(func=lambda call: call.data.startswith('editagain_'))
def edit_again(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"✏️ Повторное редактирование заказа {order_num}")

    session = edit_sessions.get(user_id)
    if not session or session['order_number'] != order_num:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    session['selected_items'] = {}
    show_product_selection(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('editcancel_'))
def edit_cancel(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]
    logger.info(f"❌ Отмена редактирования заказа {order_num}")

    session = edit_sessions.pop(user_id, None)
    if session and session['order_number'] == order_num:
        bot.edit_message_text(
            "❌ Редактирование отменено.",
            session['chat_id'],
            session['message_id']
        )
    bot.answer_callback_query(call.id)

# ==================== ОБРАБОТЧИК КНОПКИ СОЗДАНИЯ ЗАЯВКИ (из предупреждения) ====================

@bot.callback_query_handler(func=lambda call: call.data == "create_transfer_request")
def handle_create_transfer_request(call):
    user_id = call.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.answer_callback_query(call.id, "❌ Ошибка доступа")
        return
    products = get_all_products()
    if not products:
        bot.answer_callback_query(call.id, "❌ Нет товаров в каталоге.")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for p in products:
        buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
    markup.add(*buttons)
    bot.edit_message_text(
        "🔄 *Создание заявки на перемещение*\n\nВыберите товар, который хотите получить:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='Markdown',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

# ==================== ЭНДПОИНТ ДЛЯ УВЕДОМЛЕНИЙ ИЗ ОСНОВНОГО БОТА ====================

@app.route('/api/order-completed', methods=['POST'])
def order_completed():
    try:
        data = request.get_json()
        if not data or 'order_number' not in data:
            return jsonify({'error': 'Missing order_number'}), 400
        order_number = data['order_number']
        order = get_order_by_number(order_number)
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        if order.get('stock_processed'):
            return jsonify({'status': 'already_processed'}), 200
        seller_id = order['seller_id']
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id FROM sellers WHERE id = %s", (seller_id,))
                seller = cur.fetchone()
                if not seller:
                    return jsonify({'error': 'Seller not found'}), 404
                seller_tg = seller['telegram_id']
        items_text = "\n".join([f"• {item['name']}: {item['quantity']} шт" for item in order['items']])
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{order_number}"),
            types.InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{order_number}")
        )
        try:
            bot.send_message(
                seller_tg,
                f"📦 *Заказ {order_number} завершён!*\n\n"
                f"{items_text}\n\n"
                "Зафиксируйте продажу:",
                parse_mode='Markdown',
                reply_markup=markup
            )
            logger.info(f"Уведомление о заказе {order_number} отправлено продавцу {seller_tg}")
        except Exception as e:
            logger.error(f"Ошибка отправки продавцу: {e}")
            return jsonify({'error': 'Failed to notify seller'}), 500
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.exception("Ошибка в /api/order-completed")
        return jsonify({'error': str(e)}), 500

# ==================== ВЕБХУК И ЗАПУСК ====================

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Bad Request', 400

@app.route('/')
def index():
    return '🤖 Складской бот работает'

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
