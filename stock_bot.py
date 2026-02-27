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

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы обязательные переменные окружения")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://skladbot-rhoo.onrender.com')
WEBHOOK_URL = f"{BASE_URL}/webhook"

def parse_contact(contact_json):
    """Преобразует JSON-строку contact в словарь, если это необходимо."""
    if isinstance(contact_json, dict):
        return contact_json
    try:
        return json.loads(contact_json)
    except:
        return {}

def parse_items(items_json):
    """Преобразует JSON-строку items в список, если это необходимо."""
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

def get_order_by_number(order_number: str):
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

def update_product_stock(product_id: int, change: int, reason: str, order_id: int = None, seller_id: int = None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE products SET stock = stock + %s WHERE id = %s", (change, product_id))
            cur.execute("""
                INSERT INTO stock_movements (product_id, quantity_change, reason, order_id, seller_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (product_id, change, reason, order_id, seller_id))
            conn.commit()

def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Ожидают обработки"))
    return keyboard

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
        "Используйте кнопку ниже, чтобы посмотреть заказы, ожидающие обработки.",
        reply_markup=main_keyboard()
    )

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
        items = order['items']  # уже список, так как RealDictCursor парсит JSON
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

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_'))
def handle_confirm(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]

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

    for item in order['items']:
        update_product_stock(
            product_id=item['productId'],
            change=-item['quantity'],
            reason='sale',
            order_id=order['id'],
            seller_id=seller['id']
        )

    mark_order_as_processed(order['id'])

    bot.answer_callback_query(call.id, "✅ Продажа зафиксирована")
    bot.edit_message_text(
        f"✅ Заказ {order_num} проведён.",
        call.message.chat.id,
        call.message.message_id
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('edit_'))
def handle_edit(call):
    user_id = call.from_user.id
    order_num = call.data.split('_')[1]

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

    markup = types.ForceReply(selective=False)
    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ Редактирование заказа {order_num}\n"
        "Введите новое количество для каждого товара в формате:\n"
        f"{chr(10).join([f'{item["name"]}: {item["quantity"]}' for item in order['items']])}\n\n"
        "Например: 2,1,3 (через запятую в том же порядке)",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_edit, order_num)

def process_edit(message, order_num):
    user_id = message.from_user.id
    seller = get_seller_by_telegram_id(user_id)
    if not seller:
        bot.reply_to(message, "❌ Ошибка доступа")
        return

    order = get_order_by_number(order_num)
    if not order or order['seller_id'] != seller['id']:
        bot.reply_to(message, "❌ Заказ не найден или не ваш")
        return

    try:
        new_quantities = [int(x.strip()) for x in message.text.split(',')]
        if len(new_quantities) != len(order['items']):
            raise ValueError
    except:
        bot.reply_to(message, "❌ Неверный формат. Введите числа через запятую в том же порядке.")
        return

    if any(q < 0 for q in new_quantities):
        bot.reply_to(message, "❌ Количество не может быть отрицательным.")
        return

    for i, item in enumerate(order['items']):
        old_qty = item['quantity']
        new_qty = new_quantities[i]
        diff = new_qty - old_qty
        if diff != 0:
            update_product_stock(
                product_id=item['productId'],
                change=-diff,
                reason='correction',
                order_id=order['id'],
                seller_id=seller['id']
            )

    mark_order_as_processed(order['id'])

    bot.reply_to(message, f"✅ Заказ {order_num} обработан с изменениями.")
    bot.send_message(message.chat.id, "Вернуться к списку: /pending")

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
