import logging
from datetime import datetime
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_seller_stock,
    decrease_seller_stock, create_direct_sale
)
from keyboards import main_keyboard
from notifications import send_negative_stock_warning

logger = logging.getLogger(__name__)

direct_sale_sessions = {}

def register_direct_sale_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "➕ Зафиксировать продажу")
    def handle_direct_sale(message):
        user_id = message.from_user.id
        logger.info(f"➕ Нажата кнопка 'Зафиксировать продажу' пользователем {user_id}")
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return
        direct_sale_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': [],
            'message_id': message.message_id,
            'chat_id': message.chat.id
        }
        show_product_list(user_id)

    def show_product_list(user_id):
        session = direct_sale_sessions.get(user_id)
        if not session:
            return
        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"ds_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("✅ Завершить", callback_data="ds_finish"))
        bot.send_message(
            session['chat_id'],
            "🛒 *Зафиксировать продажу*\n\nВыберите товар:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ds_prod_'))
    def select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        logger.info(f"🔘 Выбран товар {product_id} пользователем {user_id}")
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        session['current_product'] = product_id
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        bot.edit_message_text(
            f"Введите количество для *{product_name}*:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    def process_quantity(message, user_id, product_id):
        logger.info(f"📝 Ввод количества для товара {product_id}, пользователь {user_id}")
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
            return
        try:
            qty = int(message.text.strip())
            if qty <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            show_product_list(user_id)
            return
        products = get_all_products()
        product = next((p for p in products if p['id'] == product_id), None)
        if not product:
            bot.reply_to(message, "❌ Товар не найден")
            return
        # Используем цену для покупателя (price)
        price = product.get('price', 0)
        if price == 0:
            bot.reply_to(message, "❌ У товара не указана цена.")
            return
        session['temp_qty'] = qty
        session['temp_price'] = price
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"ds_confirm_{product_id}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"ds_change_{product_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="ds_cancel")
        )
        bot.send_message(
            session['chat_id'],
            f"Вы продали *{product['name']}* – *{qty}* упаковок?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ds_confirm_'))
    def confirm_item(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        logger.info(f"✅ Подтверждение товара {product_id} пользователем {user_id}")
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        qty = session.pop('temp_qty', None)
        price = session.pop('temp_price', None)
        if qty is None or price is None:
            bot.answer_callback_query(call.id, "❌ Ошибка данных")
            return
        products = get_all_products()
        product = next((p for p in products if p['id'] == product_id), None)
        if not product:
            bot.answer_callback_query(call.id, "❌ Товар не найден")
            return
        session['items'].append({
            'product_id': product_id,
            'name': product['name'],
            'quantity': qty,
            'price': price
        })
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_summary(user_id)

    def show_summary(user_id):
        session = direct_sale_sessions.get(user_id)
        if not session:
            return
        items = session['items']
        if not items:
            bot.send_message(session['chat_id'], "❌ Нет товаров.")
            return
        total = sum(item['quantity'] * item['price'] for item in items)
        lines = [f"{item['name']} – {item['quantity']} шт (по {item['price']} руб.)" for item in items]
        summary = "\n".join(lines)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить продажу", callback_data="ds_confirm_sale"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="ds_add"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="ds_cancel")
        )
        bot.send_message(
            session['chat_id'],
            f"📦 *Продажа*\n\n{summary}\n\nИтого: *{total} руб.*\n\nПодтвердить?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data == "ds_add")
    def add_item(call):
        user_id = call.from_user.id
        logger.info(f"➕ Добавление товара пользователем {user_id}")
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_list(user_id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ds_change_'))
    def change_item(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        logger.info(f"✏️ Изменение товара {product_id} пользователем {user_id}")
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        bot.delete_message(session['chat_id'], call.message.message_id)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        bot.send_message(
            session['chat_id'],
            f"Введите количество для *{product_name}*:",
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_cancel")
    def cancel(call):
        user_id = call.from_user.id
        logger.info(f"❌ Отмена пользователем {user_id}")
        direct_sale_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Действие отменено.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_confirm_sale")
    def confirm_sale(call):
        user_id = call.from_user.id
        logger.info(f"✅ Вызван confirm_sale, user_id={user_id}")
        session = direct_sale_sessions.pop(user_id, None)
        if not session or not session['items']:
            logger.warning(f"❌ Сессия пуста или не найдена для user_id={user_id}")
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        seller_id = session['seller_id']
        items = session['items']
        total = sum(item['quantity'] * item['price'] for item in items)
        logger.info(f"Продажа: seller={seller_id}, items={items}, total={total}")
        # Списываем товары со склада продавца
        for item in items:
            decrease_seller_stock(
                seller_id=seller_id,
                product_id=item['product_id'],
                quantity=item['quantity'],
                reason='sale',
                order_id=None
            )
        # Сохраняем прямую продажу
        sale_id = create_direct_sale(seller_id, items, total)
        logger.info(f"✅ Продажа №{sale_id} сохранена")
        bot.edit_message_text(
            f"✅ Продажа №{sale_id} зафиксирована!\nТовары списаны со склада.",
            call.message.chat.id,
            call.message.message_id
        )
        # Проверка на отрицательные остатки
        from models import get_negative_stock_summary
        negatives = get_negative_stock_summary(seller_id)
        if negatives:
            send_negative_stock_warning(bot, session['chat_id'], seller_id)
        bot.answer_callback_query(call.id, "✅ Продажа подтверждена")

    @bot.callback_query_handler(func=lambda call: call.data == "ds_finish")
    def finish_without_items(call):
        user_id = call.from_user.id
        logger.info(f"🏁 Завершение без товаров пользователем {user_id}")
        session = direct_sale_sessions.get(user_id)
        if session and not session['items']:
            direct_sale_sessions.pop(user_id, None)
            bot.edit_message_text(
                "✅ Продажа отменена (нет товаров).",
                call.message.chat.id,
                call.message.message_id
            )
        else:
            bot.answer_callback_query(call.id, "❌ Ошибка")
        bot.answer_callback_query(call.id)
