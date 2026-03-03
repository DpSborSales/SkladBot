# handlers/direct_sale.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_product_variants,
    get_seller_stock, decrease_seller_stock, create_direct_sale,
    get_negative_stock_summary, get_variant
)
from notifications import send_negative_stock_warning

logger = logging.getLogger(__name__)

direct_sale_sessions = {}

def register_direct_sale_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "➕ Зафиксировать продажу")
    def handle_direct_sale(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return
        direct_sale_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': [],  # список словарей с variant_id, quantity, price, price_seller
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
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        variants = get_product_variants(product_id)
        variants = [v for v in variants if v['name'] != 'Россыпь']
        if not variants:
            bot.answer_callback_query(call.id, "❌ У товара нет доступных вариантов")
            return

        # Показываем кнопки выбора варианта
        markup = types.InlineKeyboardMarkup(row_width=2)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        for v in variants:
            btn_text = f"{product_name} {v['name']}"
            markup.add(types.InlineKeyboardButton(
                btn_text,
                callback_data=f"ds_var_{product_id}_{v['id']}"
            ))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="ds_back_to_products"))
        bot.edit_message_text(
            f"Выберите фасовку:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ds_var_'))
    def select_variant(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        product_id = int(parts[2])
        variant_id = int(parts[3])
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        session['current_product'] = product_id
        session['current_variant'] = variant_id
        bot.edit_message_text(
            f"Введите количество:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_quantity, user_id)
        bot.answer_callback_query(call.id)

    def process_quantity(message, user_id):
        session = direct_sale_sessions.get(user_id)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла")
            return
        try:
            qty = int(message.text.strip())
            if qty <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            show_product_list(user_id)
            return

        product_id = session.get('current_product')
        variant_id = session.get('current_variant')
        if not product_id or not variant_id:
            bot.reply_to(message, "❌ Ошибка выбора товара")
            return

        variant = get_variant(variant_id)
        if not variant:
            bot.reply_to(message, "❌ Вариант не найден")
            return

        session['items'].append({
            'variant_id': variant_id,
            'product_id': product_id,
            'variant_name': variant['name'],
            'product_name': variant['product_name'],
            'quantity': qty,
            'price': variant['price'],
            'price_seller': variant['price_seller']
        })
        logger.info(f"✅ Добавлена позиция: {variant['product_name']} ({variant['name']}) x{qty}")

        # Возвращаемся к списку товаров
        show_product_list(user_id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_back_to_products")
    def back_to_products(call):
        user_id = call.from_user.id
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_finish")
    def finish(call):
        user_id = call.from_user.id
        session = direct_sale_sessions.get(user_id)
        if not session or not session['items']:
            bot.answer_callback_query(call.id, "❌ Нет товаров для завершения")
            return

        items = session['items']
        total_buyer = sum(i['quantity'] * i['price'] for i in items)
        total_seller = sum(i['quantity'] * i['price_seller'] for i in items)

        # Формируем сводку
        lines = [f"• {i['product_name']} ({i['variant_name']}): {i['quantity']} шт (по {i['price']} руб.)" for i in items]
        summary = "\n".join(lines)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data="ds_confirm_sale"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="ds_add"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="ds_cancel")
        )
        bot.edit_message_text(
            f"📦 *Продажа*\n\n{summary}\n\n"
            f"Итого (покупатель): *{total_buyer} руб.*\n"
            f"Итого (продавец): *{total_seller} руб.*\n\n"
            "Подтвердить?",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_add")
    def add_item(call):
        user_id = call.from_user.id
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ds_cancel")
    def cancel(call):
        user_id = call.from_user.id
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
        session = direct_sale_sessions.pop(user_id, None)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        items = session.get('items', [])
        if not items:
            bot.answer_callback_query(call.id, "❌ Нет товаров для продажи")
            return
        seller_id = session['seller_id']
        total_buyer = sum(i['quantity'] * i['price'] for i in items)

        # Списание со склада
        for item in items:
            decrease_seller_stock(
                seller_id=seller_id,
                variant_id=item['variant_id'],
                quantity=item['quantity'],
                reason='sale',
                order_id=None
            )

        # Сохранение прямой продажи
        sale_id = create_direct_sale(seller_id, items, total_buyer)

        bot.edit_message_text(
            f"✅ Продажа №{sale_id} зафиксирована!\nТовары списаны со склада.",
            call.message.chat.id,
            call.message.message_id
        )

        negatives = get_negative_stock_summary(seller_id)
        if negatives:
            send_negative_stock_warning(bot, session['chat_id'], seller_id)

        bot.answer_callback_query(call.id, "✅ Продажа подтверждена")
