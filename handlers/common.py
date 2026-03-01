# handlers/common.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_order_by_number, get_all_products,
    get_seller_stock, decrease_seller_stock, mark_order_as_processed,
    get_negative_stock_summary
)
from keyboards import main_keyboard, admin_keyboard
from notifications import send_negative_stock_warning
from config import ADMIN_ID
from database import get_db_connection

logger = logging.getLogger(__name__)

def register_common_handlers(bot):
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller and user_id != ADMIN_ID:
            bot.reply_to(message, "❌ У вас нет доступа к этому боту.")
            return
        bot.send_message(
            message.chat.id,
            "👋 Добро пожаловать в складской учёт!\n\n"
            "Когда заказ завершён, вы получите уведомление для фиксации продажи.\n"
            "Используйте кнопки ниже для навигации.",
            reply_markup=main_keyboard()
        )
        if user_id == ADMIN_ID:
            bot.send_message(
                message.chat.id,
                "👑 Вы администратор. Используйте кнопку '👑 Админ панель' для перехода.",
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

    @bot.message_handler(func=lambda m: m.text == "👑 Админ панель")
    def handle_admin_panel(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "❌ У вас нет прав администратора.")
            return
        bot.send_message(
            message.chat.id,
            "👑 *Панель администратора*",
            parse_mode='Markdown',
            reply_markup=admin_keyboard()
        )

    @bot.message_handler(func=lambda m: m.text == "🔙 Назад в общее меню")
    def handle_back_to_main(message):
        bot.send_message(
            message.chat.id,
            "Главное меню:",
            reply_markup=main_keyboard()
        )
