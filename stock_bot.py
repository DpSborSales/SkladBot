import logging
import telebot
from flask import Flask, request, jsonify

from config import BOT_TOKEN, PORT, WEBHOOK_URL, ADMIN_ID
from handlers import register_all_handlers
from database import get_db_connection
from models import get_order_by_number, get_seller_by_id
from telebot import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Регистрируем все обработчики
register_all_handlers(bot)

# Эндпоинт для уведомлений из основного бота
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
