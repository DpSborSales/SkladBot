# handlers/common.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_order_by_number, get_all_products,
    get_seller_stock, decrease_seller_stock, mark_order_as_processed,
    get_negative_stock_summary, get_pending_transfer_requests_for_hub
)
from keyboards import main_keyboard, admin_keyboard
from notifications import send_negative_stock_warning
from config import ADMIN_ID, HUB_SELLER_ID
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

    @bot.message_handler(commands=['stock'])
    def handle_stock(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа к этому боту.")
            return
        stocks = get_seller_stock(seller['id'])
        if not stocks:
            bot.reply_to(message, "📦 У вас нет товаров на складе.")
            return
        lines = []
        for row in stocks:
            if row['quantity'] > 0:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): {row['quantity']} шт")
            elif row['quantity'] < 0:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): {row['quantity']} шт (❗ минус)")
            else:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): 0 шт")
        bot.reply_to(message, "📦 *Ваши остатки:*\n" + "\n".join(lines), parse_mode='Markdown')

    @bot.message_handler(func=lambda m: m.text == "📋 Ожидают обработки")
    def handle_pending_orders(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return

        # Получаем завершённые заказы, ожидающие обработки
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT order_number, items FROM orders
                    WHERE seller_id = %s AND status = 'completed' AND stock_processed = FALSE
                    ORDER BY id DESC
                """, (seller['id'],))
                pending_orders = cur.fetchall()

        # Если это кладовщик, добавляем заявки на перемещение
        pending_transfers = []
        if seller['id'] == HUB_SELLER_ID:
            pending_transfers = get_pending_transfer_requests_for_hub()

        if not pending_orders and not pending_transfers:
            bot.reply_to(message, "✅ Нет заказов или заявок, ожидающих обработки.")
            return

        # Отправляем заказы
        for order in pending_orders:
            order_number = order['order_number']
            items = order['items']
            items_text_lines = []
            for item in items:
                if item.get('variantName'):
                    items_text_lines.append(f"• {item['name']} ({item['variantName']}): {item['quantity']} шт")
                else:
                    items_text_lines.append(f"• {item['name']}: {item['quantity']} шт")
            items_text = "\n".join(items_text_lines)
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

        # Отправляем заявки на перемещение (только для кладовщика)
        for transfer in pending_transfers:
            transfer_id = transfer['id']
            items = transfer['items']
            items_text_lines = []
            for item in items:
                items_text_lines.append(f"• {item['product_name']} ({item['variant_name']}): {item['quantity']} шт")
            items_text = "\n".join(items_text_lines)
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{transfer_id}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{transfer_id}")
            )
            bot.send_message(
                message.chat.id,
                f"📦 *Заявка на перемещение №{transfer_id}*\n\n{items_text}",
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
