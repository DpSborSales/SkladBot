# handlers/admin.py
import logging
from datetime import datetime
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_hub_stock,
    get_all_sellers_stock, get_pending_payments, get_payment_request,
    update_payment_status, get_seller_debt, get_seller_profit,
    create_purchase, get_purchases_history, get_purchase,
    get_total_payments_stats, HUB_SELLER_ID, get_seller_by_id
)
from config import ADMIN_ID
from keyboards import admin_keyboard
from notifications import send_negative_stock_warning
from database import get_db_connection

logger = logging.getLogger(__name__)

purchase_sessions = {}

def register_admin_handlers(bot):
    def is_admin(user_id):
        return user_id == ADMIN_ID

    @bot.message_handler(func=lambda m: m.text == "⏳ Ожидают обработки" and is_admin(m.from_user.id))
    def handle_pending_payments(message):
        # ... (без изменений, как в предыдущей версии)
        pass

    # ... (другие обработчики выплат остаются без изменений)

    @bot.message_handler(func=lambda m: m.text == "📦 Остатки" and is_admin(m.from_user.id))
    def handle_admin_stock(message):
        # Получаем всех продавцов, кроме администратора
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name FROM sellers 
                    WHERE id != %s 
                    ORDER BY name
                """, (ADMIN_ID,))
                sellers = cur.fetchall()
        if not sellers:
            bot.send_message(message.chat.id, "❌ Нет продавцов.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for s in sellers:
            markup.add(types.InlineKeyboardButton(s['name'], callback_data=f"stock_seller_{s['id']}"))
        markup.add(types.InlineKeyboardButton("📊 Все остатки", callback_data="stock_all"))
        markup.add(types.InlineKeyboardButton("📦 Хаб (кг)", callback_data="stock_hub"))
        bot.send_message(
            message.chat.id,
            "📦 Выберите продавца, посмотрите общие остатки или остатки хаба:",
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('stock_seller_') and is_admin(call.from_user.id))
    def stock_seller(call):
        seller_id = int(call.data.split('_')[2])
        from models import get_seller_stock
        stocks = get_seller_stock(seller_id)  # возвращает все варианты с quantity
        if not stocks:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM sellers WHERE id = %s", (seller_id,))
                    seller_name = cur.fetchone()['name']
            bot.edit_message_text(
                f"📦 У продавца {seller_name} нет товаров в каталоге.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        seller_name = stocks[0]['seller_name'] if 'seller_name' in stocks[0] else "Продавец"
        lines = []
        for row in stocks:
            if row['quantity'] > 0:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): {row['quantity']} шт")
            elif row['quantity'] < 0:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): {row['quantity']} шт (❗ минус)")
            else:
                lines.append(f"• {row['product_name']} ({row['variant_name']}): 0 шт")
        bot.edit_message_text(
            f"📦 *Остатки продавца {seller_name}:*\n\n" + "\n".join(lines),
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "stock_hub" and is_admin(call.from_user.id))
    def stock_hub(call):
        # Получаем остатки хаба (в кг)
        hub_stocks = get_hub_stock()
        if not hub_stocks:
            bot.edit_message_text(
                "📦 На хабе нет нерасфасованного товара.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        lines = []
        for item in hub_stocks:
            lines.append(f"• {item['name']}: {item['quantity_kg']} кг")
        bot.edit_message_text(
            "📦 *Остатки на хабе (нерасфасовано):*\n\n" + "\n".join(lines),
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "stock_all" and is_admin(call.from_user.id))
    def stock_all(call):
        # Получаем все варианты товаров
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT v.id, v.name as variant_name, p.id as product_id, p.name as product_name
                    FROM product_variants v
                    JOIN products p ON v.product_id = p.id
                    WHERE v.name != 'Россыпь'
                    ORDER BY p.name, v.sort_order
                """)
                all_variants = cur.fetchall()

        # Суммарные остатки по каждому варианту от всех продавцов (включая кладовщика, но исключая администратора)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT variant_id, SUM(quantity) as total
                    FROM seller_stock
                    WHERE seller_id != %s
                    GROUP BY variant_id
                """, (ADMIN_ID,))
                totals = {row['variant_id']: row['total'] for row in cur.fetchall()}

        lines = []
        for v in all_variants:
            qty = totals.get(v['id'], 0)
            if qty > 0:
                lines.append(f"• {v['product_name']} ({v['variant_name']}): {qty} шт")
            elif qty < 0:
                lines.append(f"• {v['product_name']} ({v['variant_name']}): {qty} шт (❗ минус)")
            else:
                lines.append(f"• {v['product_name']} ({v['variant_name']}): 0 шт")

        bot.edit_message_text(
            "📊 *Общие остатки по всем продавцам:*\n\n" + "\n".join(lines),
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id)

    @bot.message_handler(func=lambda m: m.text == "💰 Выплаты" and is_admin(m.from_user.id))
    def handle_payments_stats(message):
        logger.info("Вызван handle_payments_stats")
        try:
            total_paid, total_debt = get_total_payments_stats()
        except Exception as e:
            logger.error(f"Ошибка в handle_payments_stats: {e}")
            bot.send_message(message.chat.id, "❌ Ошибка при загрузке статистики.")
            return
        # Получаем всех продавцов, кроме администратора
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name FROM sellers 
                    WHERE id != %s 
                    ORDER BY name
                """, (ADMIN_ID,))
                sellers = cur.fetchall()
        msg = (
            f"💰 *Финансовая сводка*\n\n"
            f"Всего выплачено продавцами: *{total_paid} руб.*\n"
            f"Общий долг продавцов: *{total_debt} руб.*\n\n"
            f"*Детали по продавцам:*"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        for s in sellers:
            markup.add(types.InlineKeyboardButton(s['name'], callback_data=f"payments_seller_{s['id']}"))
        bot.send_message(message.chat.id, msg, parse_mode='Markdown', reply_markup=markup)

    # ... (остальные обработчики выплат, включая payments_seller, без изменений)

    @bot.message_handler(func=lambda m: m.text == "📦 Закуп товаров" and is_admin(m.from_user.id))
    def handle_purchase(message):
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("📜 История", callback_data="purchase_history"),
            types.InlineKeyboardButton("🛒 Произвести закуп", callback_data="purchase_new")
        )
        bot.send_message(
            message.chat.id,
            "📦 *Управление закупками*\n\nВыберите действие:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    # ... (обработчики истории закупок без изменений)

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_new" and is_admin(call.from_user.id))
    def purchase_new(call):
        user_id = call.from_user.id
        if user_id in purchase_sessions:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Начать новую", callback_data="purchase_force_new"),
                types.InlineKeyboardButton("❌ Отмена", callback_data="purchase_abort")
            )
            bot.edit_message_text(
                "⚠️ У вас уже есть незавершённая закупка. Начать новую?",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        purchase_sessions[user_id] = {
            'items': [],
            'message_id': call.message.message_id,
            'chat_id': call.message.chat.id
        }
        show_product_list_for_purchase(user_id)

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_force_new" and is_admin(call.from_user.id))
    def purchase_force_new(call):
        user_id = call.from_user.id
        purchase_sessions[user_id] = {
            'items': [],
            'message_id': call.message.message_id,
            'chat_id': call.message.chat.id
        }
        show_product_list_for_purchase(user_id)

    def show_product_list_for_purchase(user_id):
        session = purchase_sessions.get(user_id)
        if not session:
            return
        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"purchase_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="purchase_abort"))
        bot.edit_message_text(
            "🛒 *Закупка товаров (в кг)*\n\nВыберите товар:",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('purchase_prod_') and is_admin(call.from_user.id))
    def purchase_select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        session = purchase_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        session['current_product'] = product_id

        # Получаем закупочную цену товара из базы
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT purchase_price_kg, name FROM products WHERE id = %s", (product_id,))
                product = cur.fetchone()
        if not product:
            bot.answer_callback_query(call.id, "❌ Товар не найден")
            return

        purchase_price = product['purchase_price_kg']
        if purchase_price <= 0:
            bot.answer_callback_query(call.id, "❌ У товара не установлена закупочная цена. Сначала задайте её в базе.")
            return

        # Сохраняем цену в сессии
        session['purchase_price'] = purchase_price

        bot.edit_message_text(
            f"Введите количество килограммов для *{product['name']}* (цена за кг: {purchase_price} руб.):",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_purchase_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    def process_purchase_quantity(message, user_id, product_id):
        session = purchase_sessions.get(user_id)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
            return
        try:
            qty_kg = float(message.text.strip().replace(',', '.'))
            if qty_kg <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное число (килограммы).")
            show_product_list_for_purchase(user_id)
            return

        # Берём цену из сессии
        price_per_kg = session.get('purchase_price')
        if not price_per_kg:
            bot.reply_to(message, "❌ Ошибка: цена не найдена. Начните заново.")
            show_product_list_for_purchase(user_id)
            return

        session['items'].append({
            'product_id': product_id,
            'quantity_kg': qty_kg,
            'price_per_kg': price_per_kg
        })
        # Можно сразу показать сводку, либо продолжить добавление
        show_purchase_summary(user_id)

    def show_purchase_summary(user_id):
        session = purchase_sessions.get(user_id)
        if not session:
            return
        products = get_all_products()
        product_dict = {p['id']: p['name'] for p in products}
        total = sum(item['quantity_kg'] * item['price_per_kg'] for item in session['items'])
        lines = []
        for item in session['items']:
            name = product_dict.get(item['product_id'], f"Товар {item['product_id']}")
            lines.append(f"{name} – {item['quantity_kg']} кг (по {item['price_per_kg']} руб./кг)")
        summary = "\n".join(lines)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Завершить закупку", callback_data="purchase_finish"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="purchase_add_item"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="purchase_abort")
        )
        bot.send_message(
            session['chat_id'],
            f"📦 *Текущая закупка*\n\n{summary}\n\nИтого: *{total} руб.*\n\nЧто дальше?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    # ... (остальные обработчики закупок без изменений)
