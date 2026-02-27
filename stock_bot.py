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

# Хранилище сессий редактирования
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
            cur.execute("SELECT id, name FROM products ORDER BY name")
            return cur.fetchall()

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

# ==================== НОВОЕ РЕДАКТИРОВАНИЕ ====================

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

    # Получаем все товары из каталога
    products = get_all_products()
    if not products:
        bot.answer_callback_query(call.id, "❌ Нет товаров в каталоге")
        return

    # Создаём сессию редактирования, сохраняем исходные количества
    edit_sessions[user_id] = {
        'order_number': order_num,
        'original_items': {item['productId']: item['quantity'] for item in order['items']},
        'selected_items': {},  # {product_id: quantity}
        'message_id': call.message.message_id,
        'chat_id': call.message.chat.id
    }

    # Показываем меню выбора товара
    show_product_selection(user_id)

def show_product_selection(user_id):
    session = edit_sessions.get(user_id)
    if not session:
        return

    products = get_all_products()
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for p in products:
        buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"select_product_{p['id']}"))
    markup.add(*buttons)
    # Добавляем кнопку завершения
    markup.row(types.InlineKeyboardButton("✅ Завершить редактирование", callback_data="finish_edit"))

    bot.edit_message_text(
        f"✏️ *Редактирование заказа {session['order_number']}*\n\n"
        "Выберите товар, чтобы указать проданное количество:",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('select_product_'))
def select_product(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[2])

    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Получаем название товара
    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

    session['current_product'] = product_id
    bot.edit_message_text(
        f"Введите количество для товара *{product_name}*:",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, product_id)
    bot.answer_callback_query(call.id)

def process_quantity_input(message, user_id, product_id):
    session = edit_sessions.get(user_id)
    if not session:
        bot.reply_to(message, "❌ Сессия редактирования истекла. Начните заново.")
        return

    try:
        qty = int(message.text.strip())
        if qty < 0:
            raise ValueError
    except:
        bot.reply_to(message, "❌ Введите целое неотрицательное число.")
        # Возвращаемся к выбору товара
        show_product_selection(user_id)
        return

    # Сохраняем количество во временной сессии
    session['selected_items'][product_id] = qty
    # Получаем название товара
    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_item_{product_id}"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"change_item_{product_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_item")
    )
    bot.send_message(
        session['chat_id'],
        f"*Заказ {session['order_number']}*\nВы продали *{product_name}* – *{qty}* упаковок, верно?",
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

    # Товар уже сохранён в selected_items
    # Возвращаемся к выбору следующего товара
    bot.delete_message(session['chat_id'], call.message.message_id)
    show_product_selection(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('change_item_'))
def change_item(call):
    user_id = call.from_user.id
    product_id = int(call.data.split('_')[2])

    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    bot.delete_message(session['chat_id'], call.message.message_id)
    session['current_product'] = product_id
    # Получаем название товара
    products = get_all_products()
    product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
    bot.send_message(
        session['chat_id'],
        f"Введите новое количество для товара *{product_name}*:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, product_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_item")
def cancel_item(call):
    user_id = call.from_user.id
    session = edit_sessions.get(user_id)
    if session:
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "finish_edit")
def finish_edit(call):
    user_id = call.from_user.id
    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    if not session['selected_items']:
        # Ничего не выбрано – можно завершить без изменений или отменить
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить без изменений", callback_data="finish_no_changes"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")
        )
        bot.edit_message_text(
            f"*Заказ {session['order_number']}*\n\nВы не добавили ни одного товара. Подтвердить заказ без изменений?",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return

    # Формируем сводку
    products = get_all_products()
    product_names = {p['id']: p['name'] for p in products}
    lines = []
    for pid, qty in session['selected_items'].items():
        name = product_names.get(pid, f"Товар {pid}")
        lines.append(f"• {name}: {qty} упаковок")
    summary = "\n".join(lines)

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Подтвердить заказ", callback_data="apply_edit"),
        types.InlineKeyboardButton("✏️ Изменить", callback_data="edit_again"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")
    )
    bot.edit_message_text(
        f"*Заказ {session['order_number']}*\n\n"
        f"*Вы продали:*\n{summary}\n\n"
        "Всё верно?",
        session['chat_id'],
        session['message_id'],
        parse_mode='Markdown',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "apply_edit")
def apply_edit(call):
    user_id = call.from_user.id
    session = edit_sessions.pop(user_id, None)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    order_num = session['order_number']
    logger.info(f"🔄 apply_edit: пытаемся найти заказ '{order_num}'")
    order = get_order_by_number(order_num)
    if not order:
        logger.error(f"❌ apply_edit: заказ '{order_num}' не найден в базе")
        bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    seller = get_seller_by_telegram_id(user_id)
    if not seller or order['seller_id'] != seller['id']:
        bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
        return

    if order.get('stock_processed'):
        bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
        return

    # Сравниваем с исходными количествами и списываем разницу
    original = session['original_items']
    selected = session['selected_items']

    all_product_ids = set(original.keys()) | set(selected.keys())
    for pid in all_product_ids:
        old_qty = original.get(pid, 0)
        new_qty = selected.get(pid, 0)
        diff = new_qty - old_qty
        if diff != 0:
            update_product_stock(
                product_id=pid,
                change=-diff,  # если diff>0, списываем дополнительно; если diff<0 – возвращаем
                reason='correction',
                order_id=order['id'],
                seller_id=seller['id']
            )

    mark_order_as_processed(order['id'])

    bot.edit_message_text(
        f"✅ Заказ {session['order_number']} обработан.",
        session['chat_id'],
        session['message_id']
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "finish_no_changes")
def finish_no_changes(call):
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

    # Списать исходные количества
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
        f"✅ Заказ {session['order_number']} проведён без изменений.",
        session['chat_id'],
        session['message_id']
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "edit_again")
def edit_again(call):
    user_id = call.from_user.id
    session = edit_sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "❌ Сессия истекла")
        return

    # Очищаем выбранные товары и начинаем заново
    session['selected_items'] = {}
    show_product_selection(user_id)
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

# ==================== ЭНДПОИНТ ====================

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
