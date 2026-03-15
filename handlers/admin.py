# handlers/admin.py
import logging
from datetime import datetime
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_hub_stock,
    get_all_sellers_stock, get_pending_payments, get_payment_request,
    update_payment_status, get_seller_debt, get_seller_profit,
    create_purchase, get_purchases_history, get_purchase,
    get_total_payments_stats, HUB_SELLER_ID, get_seller_by_id,
    get_all_pending_transfer_requests
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
    def handle_pending_items(message):
        logger.info("Вызван handle_pending_items")
        
        # Получаем неподтверждённые выплаты
        try:
            pending_payments = get_pending_payments()
            logger.info(f"Найдено неподтверждённых выплат: {len(pending_payments)}")
        except Exception as e:
            logger.error(f"Ошибка при загрузке выплат: {e}")
            pending_payments = []
        
        # Получаем неподтверждённые заявки на перемещение
        try:
            pending_transfers = get_all_pending_transfer_requests()
            logger.info(f"Найдено неподтверждённых заявок: {len(pending_transfers)}")
        except Exception as e:
            logger.error(f"Ошибка при загрузке заявок: {e}")
            pending_transfers = []
        
        # Если ничего нет
        if not pending_payments and not pending_transfers:
            bot.send_message(message.chat.id, "✅ Нет неподтверждённых выплат или заявок на перемещение.")
            return
        
        # Отправляем выплаты
        for p in pending_payments:
            date_str = p['created_at'][:10] if p['created_at'] else 'неизвестно'
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_pay_confirm_{p['id']}"),
                types.InlineKeyboardButton("✏️ Изменить", callback_data=f"admin_pay_edit_{p['id']}")
            )
            bot.send_message(
                message.chat.id,
                f"💸 *Запрос на выплату*\n\n"
                f"Продавец: {p['seller_name']}\n"
                f"Сумма: {p['amount']} руб.\n"
                f"Дата: {date_str}\n\n"
                f"Действие:",
                parse_mode='Markdown',
                reply_markup=markup
            )
        
        # Отправляем заявки на перемещение
        for req in pending_transfers:
            items_text = "\n".join([
                f"• {item['product_name']} ({item['variant_name']}): {item['quantity']} шт"
                for item in req['items']
            ])
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{req['id']}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{req['id']}")
            )
            bot.send_message(
                message.chat.id,
                f"📦 *Заявка на перемещение №{req['id']}*\n"
                f"От: {req['from_seller_name']}\n"
                f"Кому: {req['to_seller_name']}\n\n"
                f"{items_text}",
                parse_mode='Markdown',
                reply_markup=markup
            )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('admin_pay_confirm_') and is_admin(call.from_user.id))
    def admin_pay_confirm(call):
        payment_id = int(call.data.split('_')[3])
        payment = get_payment_request(payment_id)
        if not payment or payment['status'] != 'pending':
            bot.answer_callback_query(call.id, "❌ Заявка уже обработана или не найдена")
            return
        update_payment_status(payment_id, 'confirmed', confirmed_amount=payment['amount'])
        bot.edit_message_text(
            f"✅ Выплата {payment['amount']} руб. подтверждена.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Подтверждено")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('admin_pay_edit_') and is_admin(call.from_user.id))
    def admin_pay_edit(call):
        payment_id = int(call.data.split('_')[3])
        payment = get_payment_request(payment_id)
        if not payment or payment['status'] != 'pending':
            bot.answer_callback_query(call.id, "❌ Заявка уже обработана или не найдена")
            return
        bot.edit_message_text(
            f"✏️ Введите новую сумму для выплаты:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_admin_pay_edit, payment_id, call.message.chat.id)
        bot.answer_callback_query(call.id)

    def process_admin_pay_edit(message, payment_id, original_chat_id):
        try:
            amount = int(message.text.strip())
            if amount <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            return
        payment = get_payment_request(payment_id)
        if not payment or payment['status'] != 'pending':
            bot.reply_to(message, "❌ Заявка уже обработана или не найдена")
            return
        update_payment_status(payment_id, 'confirmed', confirmed_amount=amount)
        seller = get_seller_by_id(payment['seller_id'])
        debt, _, _, _ = get_seller_debt(payment['seller_id'])
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

    @bot.message_handler(func=lambda m: m.text == "📦 Остатки" and is_admin(m.from_user.id))
    def handle_admin_stock(message):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name FROM sellers 
                    ORDER BY name
                """)
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
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM sellers WHERE id = %s", (seller_id,))
                seller_row = cur.fetchone()
                seller_name = seller_row['name'] if seller_row else "Продавец"
        from models import get_seller_stock
        stocks = get_seller_stock(seller_id)
        if not stocks:
            bot.edit_message_text(
                f"📦 У продавца {seller_name} нет товаров в каталоге.",
                call.message.chat.id,
                call.message.message_id
            )
            return
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
                cur.execute("""
                    SELECT variant_id, SUM(quantity) as total
                    FROM seller_stock
                    GROUP BY variant_id
                """)
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
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name FROM sellers 
                    ORDER BY name
                """)
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

    @bot.callback_query_handler(func=lambda call: call.data.startswith('payments_seller_') and is_admin(call.from_user.id))
    def payments_seller(call):
        seller_id = int(call.data.split('_')[2])
        debt, total_sales, total_paid, total_direct = get_seller_debt(seller_id)
        profit, total_buyer, total_seller = get_seller_profit(seller_id)
        seller = get_seller_by_id(seller_id)
        msg = (
            f"💰 *Продавец {seller['name']}*\n\n"
            f"Долг перед админом: *{debt} руб.*\n"
            f"Выплачено админу: *{total_paid} руб.*\n"
            f"________________________________\n"
            f"Общая сумма продаж: *{total_buyer} руб.*\n"
            f"Чистая прибыль продавца: *{profit} руб.*"
        )
        bot.edit_message_text(
            msg,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id)

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

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_history" and is_admin(call.from_user.id))
    def purchase_history(call):
        user_id = call.from_user.id
        logger.info(f"📜 Вызвана история закупок пользователем {user_id}")
        try:
            history = get_purchases_history(10)
            logger.info(f"Получено {len(history)} записей истории")
        except Exception as e:
            logger.error(f"Ошибка при получении истории закупок: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка базы данных")
            return
        if not history:
            bot.edit_message_text(
                "📭 История закупок пуста.",
                call.message.chat.id,
                call.message.message_id
            )
            bot.answer_callback_query(call.id)
            return
        markup = types.InlineKeyboardMarkup()
        for h in history:
            date_str = str(h['purchase_date'])[:10]
            btn_text = f"{date_str} – {h['total']} руб."
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"purchase_view_{h['id']}"))
        try:
            bot.edit_message_text(
                "📜 *История закупок*\n\nВыберите запись:",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown',
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
            bot.send_message(
                call.message.chat.id,
                "📜 *История закупок*\n\nВыберите запись:",
                parse_mode='Markdown',
                reply_markup=markup
            )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('purchase_view_') and is_admin(call.from_user.id))
    def purchase_view(call):
        logger.info(f"✅ Вызван purchase_view с data={call.data}")
        try:
            parts = call.data.split('_')
            if len(parts) < 3:
                logger.error(f"Неверный формат callback: {call.data}")
                bot.answer_callback_query(call.id, "❌ Ошибка данных")
                return
            purchase_id = int(parts[2])
            purchase = get_purchase(purchase_id)
            if not purchase:
                logger.error(f"Закупка {purchase_id} не найдена")
                bot.answer_callback_query(call.id, "❌ Закупка не найдена")
                return
            date_str = str(purchase['purchase_date'])[:10] if purchase['purchase_date'] else 'неизвестно'
            items_text = "\n".join([f"• {item['name']}: {item['quantity_kg']} кг (по {item['price_per_kg']} руб./кг)" for item in purchase['items']])
            msg = (
                f"📦 *Закупка от {date_str}*\n\n"
                f"{items_text}\n\n"
                f"Итого: *{purchase['total']} руб.*\n"
                f"Комментарий: {purchase['comment']}"
            )
            bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode='Markdown'
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в purchase_view: {e}")
            bot.answer_callback_query(call.id, "❌ Внутренняя ошибка")

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
        logger.info(f"✅ Добавлен товар {product_id} в закупку: {qty_kg} кг по {price_per_kg} руб.")
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

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_add_item" and is_admin(call.from_user.id))
    def purchase_add_item(call):
        user_id = call.from_user.id
        session = purchase_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"purchase_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад к сводке", callback_data="purchase_show_summary"))
        bot.edit_message_text(
            "🛒 *Добавление товара*\n\nВыберите товар:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_show_summary" and is_admin(call.from_user.id))
    def purchase_show_summary(call):
        user_id = call.from_user.id
        show_purchase_summary(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_finish" and is_admin(call.from_user.id))
    def purchase_finish(call):
        user_id = call.from_user.id
        session = purchase_sessions.pop(user_id, None)
        if not session or not session['items']:
            bot.answer_callback_query(call.id, "❌ Нет товаров в закупке")
            return
        total = sum(item['quantity_kg'] * item['price_per_kg'] for item in session['items'])
        admin_seller = get_seller_by_telegram_id(ADMIN_ID)
        seller_id = admin_seller['id'] if admin_seller else None
        try:
            purchase_id = create_purchase(seller_id, session['items'], total, comment="")
            logger.info(f"Закупка {purchase_id} успешно создана")
        except Exception as e:
            logger.error(f"Ошибка при создании закупки: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка базы данных", show_alert=True)
            return
        bot.edit_message_text(
            f"✅ Закупка №{purchase_id} успешно проведена!\nТовары добавлены на склад хаба.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Закупка завершена")

    @bot.callback_query_handler(func=lambda call: call.data == "purchase_abort" and is_admin(call.from_user.id))
    def purchase_abort(call):
        user_id = call.from_user.id
        purchase_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Закупка отменена.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)

    # ===== ОБРАБОТЧИК ДЛЯ ЗАЯВОК НА ПЕРЕМЕЩЕНИЕ =====
    @bot.message_handler(func=lambda m: m.text == "📦 Заявки на перемещение" and is_admin(m.from_user.id))
    def handle_admin_transfer_requests(message):
        requests = get_all_pending_transfer_requests()
        if not requests:
            bot.send_message(message.chat.id, "✅ Нет активных заявок на перемещение.")
            return
        for req in requests:
            items_text = "\n".join([
                f"• {item['product_name']} ({item['variant_name']}): {item['quantity']} шт"
                for item in req['items']
            ])
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{req['id']}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{req['id']}")
            )
            bot.send_message(
                message.chat.id,
                f"📦 *Заявка №{req['id']}*\n"
                f"От: {req['from_seller_name']}\n"
                f"Кому: {req['to_seller_name']}\n\n"
                f"{items_text}",
                parse_mode='Markdown',
                reply_markup=markup
            )
