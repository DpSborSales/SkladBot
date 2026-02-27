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

# Хранилище сессий редактирования для каждого пользователя
edit_sessions = {}

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
        items = order['items']  # уже список
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

    # Прямое подтверждение без изменений
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

# ==================== ПОШАГОВОЕ РЕДАКТИРОВАНИЕ ====================

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

    # Создаём сессию редактирования
    edit_sessions[user_id] = {
        'order_number': order_num,
        'original_items': order['items'],
        'new_quantities': {item['productId']: None for item in order['items']},  # пока не заполнено
        'current_index': 0,
        'total_items': len(order['items']),
        'message_id': call.message.message_id,
        'chat_id': call.message.chat.id
    }

    # Показываем первый товар для редактирования
    show_next_item(user_id)

def show_next_item(user_id):
    session = edit_sessions.get(user_id)
    if not session:
        return

    idx = session['current_index']
    items = session['original_items']
    if idx >= len(items):
        # Все товары обработаны, показываем сводку
        show_summary(user_id)
        return

    item = items[idx]
    product_name = item['name']
    product_id = item['productId']
    old_qty = item['quantity']

    markup = types.InlineKeyboardMarkup()
    # Кнопка для пропуска (оставить как есть)
    markup.row(
        types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip_{product_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")
    )

    bot.edit_message_text(
        f"✏️ *Редактирование заказа {session['order_number']}*\n\n"
        f"Товар *{product_name}* (было {old_qty} шт)\n\n"
        f"Введите новое количество:",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )
    # Регистрируем следующий шаг для ввода количества
    bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, product_id)

def process_quantity_input(message, user_id, product_id):
    session = edit_sessions.get(user_id)
    if not session:
        bot.reply_to(message, "❌ Сессия редактирования истекла. Начните заново.")
        return

    try:
        new_qty = int(message.text.strip())
        if new_qty < 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите целое неотрицательное число.")
        # Повторяем запрос
        show_next_item(user_id)
        return

    # Сохраняем новое количество
    session['new_quantities'][product_id] = new_qty

    # Запрашиваем подтверждение для этого товара
    ask_confirm_item(user_id, product_id, new_qty)

def ask_confirm_item(user_id, product_id, new_qty):
    session = edit_sessions.get(user_id)
    if not session:
        return

    idx = session['current_index']
    item = session['original_items'][idx]
    product_name = item['name']
    old_qty = item['quantity']

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_item_{product_id}"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"change_item_{product_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")
    )

    bot.edit_message_text(
        f"Вы продали *{product_name}* – *{new_qty}* шт?",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_item_'))
def confirm_item(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[2])

    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Переходим к следующему товару
    session['current_index'] += 1
    show_next_item(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('change_item_'))
def change_item(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[2])

    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Повторяем ввод для того же товара
    show_next_item(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('skip_'))
def skip_item(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[1])

    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Пропускаем товар – оставляем исходное количество
    item = session['original_items'][session['current_index']]
    session['new_quantities'][product_id] = item['quantity']  # оставляем старое

    # Переходим к следующему
    session['current_index'] += 1
    show_next_item(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "edit_cancel")
def edit_cancel(call):
    user_id = call.from_user.id
    session = edit_sessions.pop(user_id, None)
    if session:
        bot.edit_message_text(
            "❌ Редактирование отменено.",
            session['chat_id'],
            session['message_id']
        )
    bot.answer_callback_query(call.id)

def show_summary(user_id):
    session = edit_sessions.get(user_id)
    if not session:
        return

    original_items = session['original_items']
    new_quantities = session['new_quantities']

    # Формируем сводку
    summary_lines = []
    changes = []  # для последующего применения
    for item in original_items:
        product_id = item['productId']
        old_qty = item['quantity']
        new_qty = new_quantities.get(product_id, old_qty)
        diff = new_qty - old_qty
        if diff != 0:
            changes.append((product_id, diff, old_qty, new_qty))
            summary_lines.append(f"• {item['name']}: {old_qty} → {new_qty} ({diff:+d})")
        else:
            summary_lines.append(f"• {item['name']}: {old_qty} (без изменений)")

    if not changes:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data="summary_confirm_no_changes"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data="summary_edit"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="summary_cancel")
        )
        bot.edit_message_text(
            f"📦 *Заказ {session['order_number']}*\n\n"
            "Изменений не внесено.\n\n"
            "Подтвердить заказ?",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )
    else:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить заказ", callback_data="summary_confirm"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data="summary_edit"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="summary_cancel")
        )
        bot.edit_message_text(
            f"📦 *Заказ {session['order_number']}*\n\n"
            f"*Изменения:*\n" + "\n".join(summary_lines) + "\n\n"
            "Подтвердить заказ?",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "summary_confirm")
def summary_confirm(call):
    user_id = call.from_user.id
    session = edit_sessions.pop(user_id, None)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    order = get_order_by_number(session['order_number'])
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

    # Применяем изменения
    original_items = session['original_items']
    new_quantities = session['new_quantities']

    for item in original_items:
        product_id = item['productId']
        old_qty = item['quantity']
        new_qty = new_quantities.get(product_id, old_qty)
        diff = new_qty - old_qty
        if diff != 0:
            update_product_stock(
                product_id=product_id,
                change=-diff,
                reason='correction',
                order_id=order['id'],
                seller_id=seller['id']
            )

    mark_order_as_processed(order['id'])

    bot.edit_message_text(
        f"✅ Заказ {session['order_number']} обработан с изменениями.",
        session['chat_id'],
        session['message_id']
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "summary_confirm_no_changes")
def summary_confirm_no_changes(call):
    user_id = call.from_user.id
    session = edit_sessions.pop(user_id, None)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    order = get_order_by_number(session['order_number'])
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

    # Списать исходные количества (без изменений)
    for item in order['items']:
        update_product_stock(
            product_id=item['productId'],
            change=-item['quantity'],
            reason='sale',
            order_id=order['id'],
            seller_id=seller['id']
        )

    mark_order_as_processed(order['id'])

    bot.edit_message_text(
        f"✅ Заказ {session['order_number']} проведён.",
        session['chat_id'],
        session['message_id']
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "summary_edit")
def summary_edit(call):
    user_id = call.from_user.id
    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Сбросить индекс и начать заново
    session['current_index'] = 0
    session['new_quantities'] = {item['productId']: None for item in session['original_items']}
    show_next_item(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "summary_cancel")
def summary_cancel(call):
    user_id = call.from_user.id
    session = edit_sessions.pop(user_id, None)
    if session:
        bot.edit_message_text(
            "❌ Редактирование отменено.",
            session['chat_id'],
            session['message_id']
        )
    bot.answer_callback_query(call.id)

# ==================== ЭНДПОИНТ ДЛЯ УВЕДОМЛЕНИЙ ====================

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
