import logging
from telebot import types
from models import (
    get_order_by_number, get_seller_by_telegram_id, get_all_products,
    get_seller_stock, decrease_seller_stock, mark_order_as_processed,
    get_negative_stock_summary
)
from notifications import send_negative_stock_warning
from keyboards import main_keyboard
from utils import format_selected_summary

logger = logging.getLogger(__name__)

edit_sessions = {}

def register_edit_handlers(bot):
    @bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_'))
    def handle_confirm(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"✅ Нажата кнопка подтверждения заказа {order_num}")

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
            # Используем variantId вместо productId
            variant_id = item.get('variantId')
            if not variant_id:
                logger.error(f"В заказе {order_num} отсутствует variantId для товара {item.get('name')}")
                bot.answer_callback_query(call.id, "❌ Ошибка данных заказа (нет variantId)")
                return
            decrease_seller_stock(
                seller_id=seller['id'],
                variant_id=variant_id,
                quantity=item['quantity'],
                reason='sale',
                order_id=order['id']
            )

        mark_order_as_processed(order['id'])

        bot.answer_callback_query(call.id, "✅ Продажа зафиксирована")
        bot.edit_message_text(
            f"✅ Заказ {order_num} проведён.",
            call.message.chat.id,
            call.message.message_id
        )

        negatives = get_negative_stock_summary(seller['id'])
        if negatives:
            send_negative_stock_warning(bot, call.message.chat.id, seller['id'])

    @bot.callback_query_handler(func=lambda call: call.data.startswith('edit_'))
    def handle_edit(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"✏️ Нажата кнопка редактирования заказа {order_num}")

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

        products = get_all_products()
        if not products:
            bot.answer_callback_query(call.id, "❌ Нет товаров в каталоге")
            return

        edit_sessions[user_id] = {
            'order_number': order_num,
            'original_items': {item['productId']: item for item in order['items']},
            'selected_items': {},
            'message_id': call.message.message_id,
            'chat_id': call.message.chat.id
        }
        logger.info(f"✅ Сессия редактирования создана для заказа {order_num}")

        show_product_selection(user_id)

    def show_product_selection(user_id):
        session = edit_sessions.get(user_id)
        if not session:
            return

        products = get_all_products()
        product_names = {p['id']: p['name'] for p in products}
        selected_lines = []
        for pid, qty in session['selected_items'].items():
            original_item = session['original_items'].get(pid)
            if original_item and original_item.get('variantName'):
                name = f"{product_names.get(pid, 'Товар')} ({original_item['variantName']})"
            else:
                name = product_names.get(pid, 'Товар')
            selected_lines.append(f"{name} – {qty} упаковок")
        summary = "\n".join(selected_lines)

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for p in products:
            buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"selprod_{session['order_number']}_{p['id']}"))
        markup.add(*buttons)
        markup.row(types.InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{session['order_number']}"))

        text = f"✏️ *Редактирование заказа {session['order_number']}*\n\n"
        if summary:
            text += summary + "\n\n"
        text += "Выберите товар, чтобы указать проданное количество:"

        bot.edit_message_text(
            text,
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )
        logger.info(f"Показано меню выбора товара для заказа {session['order_number']}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('selprod_'))
    def select_product(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        order_num = parts[1]
        product_id = int(parts[2])
        logger.info(f"🔘 Выбран товар {product_id} для заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

        bot.edit_message_text(
            f"Введите количество для товара *{product_name}*:",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, order_num, product_id)
        bot.answer_callback_query(call.id)

    def process_quantity_input(message, user_id, order_num, product_id):
        logger.info(f"📝 Ввод количества для товара {product_id}, заказ {order_num}")
        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.reply_to(message, "❌ Сессия редактирования истекла. Начните заново.")
            return

        try:
            qty = int(message.text.strip())
            if qty < 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите целое неотрицательное число.")
            show_product_selection(user_id)
            return

        session['selected_items'][product_id] = qty
        logger.info(f"✅ Количество для товара {product_id} установлено: {qty}")

        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        original_item = session['original_items'].get(product_id)
        variant_display = f" ({original_item['variantName']})" if original_item and original_item.get('variantName') else ""

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"conf_{order_num}_{product_id}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"change_{order_num}_{product_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{order_num}")
        )
        bot.send_message(
            session['chat_id'],
            f"*Заказ {order_num}*\nВы продали *{product_name}{variant_display}* – *{qty}* упаковок, верно?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('conf_'))
    def confirm_item(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        order_num = parts[1]
        product_id = int(parts[2])
        logger.info(f"✅ Подтверждён товар {product_id} для заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('change_'))
    def change_item(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        order_num = parts[1]
        product_id = int(parts[2])
        logger.info(f"✏️ Изменение товара {product_id} для заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        bot.delete_message(session['chat_id'], call.message.message_id)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        bot.send_message(
            session['chat_id'],
            f"Введите новое количество для товара *{product_name}*:",
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(session['chat_id'], process_quantity_input, user_id, order_num, product_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
    def cancel_item(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        order_num = parts[1]
        logger.info(f"❌ Отмена выбора товара для заказа {order_num}")

        session = edit_sessions.get(user_id)
        if session and session['order_number'] == order_num:
            bot.delete_message(session['chat_id'], call.message.message_id)
            show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('finish_'))
    def finish_edit(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"🏁 Завершение редактирования заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        if not session['selected_items']:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Без изменений", callback_data=f"nochanges_{order_num}"),
                types.InlineKeyboardButton("❌ Отмена", callback_data=f"editcancel_{order_num}")
            )
            bot.edit_message_text(
                f"*Заказ {order_num}*\n\nВы не добавили ни одного товара. Подтвердить заказ без изменений?",
                session['chat_id'],
                session['message_id'],
                parse_mode='Markdown',
                reply_markup=markup
            )
            bot.answer_callback_query(call.id)
            return

        products = get_all_products()
        product_names = {p['id']: p['name'] for p in products}
        lines = []
        for pid, qty in session['selected_items'].items():
            original_item = session['original_items'].get(pid)
            if original_item and original_item.get('variantName'):
                name = f"{product_names.get(pid, 'Товар')} ({original_item['variantName']})"
            else:
                name = product_names.get(pid, 'Товар')
            lines.append(f"• {name}: {qty} упаковок")
        summary = "\n".join(lines)

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"apply_{order_num}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"editagain_{order_num}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"editcancel_{order_num}")
        )
        bot.edit_message_text(
            f"*Заказ {order_num}*\n\n"
            f"*Вы продали:*\n{summary}\n\n"
            "Всё верно?",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('apply_'))
    def apply_edit(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"✅ Применение изменений для заказа {order_num}")

        session = edit_sessions.pop(user_id, None)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        order = get_order_by_number(order_num)
        if not order:
            logger.error(f"apply_edit: заказ {order_num} не найден в базе")
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
            return

        if order.get('stock_processed'):
            bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
            return

        selected = session['selected_items']
        if not selected:
            bot.answer_callback_query(call.id, "❌ Нет товаров для списания")
            return

        # Для каждого выбранного товара списываем по variantId
        for product_id, qty in selected.items():
            if qty > 0:
                # Находим оригинальный элемент заказа, чтобы получить variantId
                original_item = session['original_items'].get(product_id)
                if not original_item or not original_item.get('variantId'):
                    logger.error(f"Для товара {product_id} не найден variantId в оригинальном заказе")
                    bot.answer_callback_query(call.id, f"❌ Ошибка: нет variantId для товара")
                    return
                decrease_seller_stock(
                    seller_id=seller['id'],
                    variant_id=original_item['variantId'],
                    quantity=qty,
                    reason='sale',
                    order_id=order['id']
                )
                logger.info(f"✅ Списано {qty} ед. товара variant {original_item['variantId']}")

        mark_order_as_processed(order['id'])
        logger.info(f"✅ Заказ {order_num} обработан, списано товаров: {len(selected)}")

        bot.edit_message_text(
            f"✅ Заказ {order_num} обработан.",
            session['chat_id'],
            session['message_id']
        )

        negatives = get_negative_stock_summary(seller['id'])
        if negatives:
            send_negative_stock_warning(bot, session['chat_id'], seller['id'])

    @bot.callback_query_handler(func=lambda call: call.data.startswith('nochanges_'))
    def no_changes(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"✅ Подтверждение заказа {order_num} без изменений")

        session = edit_sessions.pop(user_id, None)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

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
            variant_id = item.get('variantId')
            if not variant_id:
                logger.error(f"В заказе {order_num} отсутствует variantId для товара {item.get('name')}")
                bot.answer_callback_query(call.id, "❌ Ошибка данных заказа (нет variantId)")
                return
            decrease_seller_stock(
                seller_id=seller['id'],
                variant_id=variant_id,
                quantity=item['quantity'],
                reason='sale',
                order_id=order['id']
            )

        mark_order_as_processed(order['id'])

        bot.edit_message_text(
            f"✅ Заказ {order_num} проведён без изменений.",
            session['chat_id'],
            session['message_id']
        )

        negatives = get_negative_stock_summary(seller['id'])
        if negatives:
            send_negative_stock_warning(bot, session['chat_id'], seller['id'])

    @bot.callback_query_handler(func=lambda call: call.data.startswith('editagain_'))
    def edit_again(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"✏️ Повторное редактирование заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        session['selected_items'] = {}
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('editcancel_'))
    def edit_cancel(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"❌ Отмена редактирования заказа {order_num}")

        session = edit_sessions.pop(user_id, None)
        if session and session['order_number'] == order_num:
            bot.edit_message_text(
                "❌ Редактирование отменено.",
                session['chat_id'],
                session['message_id']
            )
        bot.answer_callback_query(call.id)   
