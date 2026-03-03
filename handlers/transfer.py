# handlers/transfer.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_all_products,
    create_transfer_request, get_transfer_request, update_transfer_request_status,
    decrease_seller_stock, increase_seller_stock, get_seller_stock
)
from config import HUB_SELLER_ID
from database import get_db_connection

logger = logging.getLogger(__name__)

transfer_sessions = {}  # user_id -> session data

def register_transfer_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "🔄 Заявка на перемещение")
    def handle_transfer_request_start(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return

        # Если это кладовщик – показываем информационное сообщение
        if seller['id'] == HUB_SELLER_ID:
            bot.reply_to(
                message,
                "ℹ️ Управление заявками на перемещение осуществляется через раздел «Ожидают обработки».\n\n"
                "Здесь вы можете подтверждать или отклонять заявки от продавцов."
            )
            return

        # Обычный продавец – начинаем новую сессию
        if user_id in transfer_sessions:
            # Если сессия уже есть, предлагаем продолжить или начать заново
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Продолжить текущую", callback_data="transfer_continue"),
                types.InlineKeyboardButton("🔄 Новая заявка", callback_data="transfer_new")
            )
            bot.send_message(
                message.chat.id,
                "⚠️ У вас уже есть незавершённая заявка. Выберите действие:",
                reply_markup=markup
            )
            return

        # Новая сессия
        transfer_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': [],          # список словарей {'product_id': id, 'quantity': qty, 'name': name}
            'message_id': None,
            'chat_id': message.chat.id
        }
        show_product_selection(user_id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_continue")
    def transfer_continue(call):
        user_id = call.from_user.id
        if user_id not in transfer_sessions:
            bot.answer_callback_query(call.id, "❌ Сессия не найдена")
            return
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_new")
    def transfer_new(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return
        transfer_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': [],
            'message_id': None,
            'chat_id': call.message.chat.id
        }
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    def show_product_selection(user_id):
        session = transfer_sessions.get(user_id)
        if not session:
            return

        products = get_all_products()
        if not products:
            bot.send_message(session['chat_id'], "❌ Нет товаров в каталоге.")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("📋 Просмотреть заявку", callback_data="transfer_show_summary"))
        markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="transfer_cancel"))

        text = "🔄 *Создание заявки на перемещение*\n\nВыберите товар, который хотите получить:"
        if session['items']:
            # Показываем уже добавленные товары
            lines = []
            for item in session['items']:
                lines.append(f"• {item['name']}: {item['quantity']} шт")
            text += "\n\n*Добавлено:*\n" + "\n".join(lines)

        bot.send_message(
            session['chat_id'],
            text,
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_prod_'))
    def transfer_product_selected(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        session['current_product'] = product_id
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

        bot.edit_message_text(
            f"Введите количество для товара *{product_name}*:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_transfer_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    def process_transfer_quantity(message, user_id, product_id):
        session = transfer_sessions.get(user_id)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
            return

        try:
            qty = int(message.text.strip())
            if qty <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            show_product_selection(user_id)
            return

        products = get_all_products()
        product = next((p for p in products if p['id'] == product_id), None)
        if not product:
            bot.reply_to(message, "❌ Товар не найден")
            return

        # Сохраняем во временные переменные
        session['temp_qty'] = qty
        session['temp_product'] = product

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_confirm_item_{product_id}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"transfer_change_item_{product_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="transfer_cancel_item")
        )
        bot.send_message(
            session['chat_id'],
            f"Добавить *{product['name']}* – *{qty}* упаковок в заявку?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_confirm_item_'))
    def transfer_confirm_item(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[3])
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        qty = session.pop('temp_qty', None)
        product = session.pop('temp_product', None)
        if qty is None or product is None:
            bot.answer_callback_query(call.id, "❌ Ошибка данных")
            return

        # Проверяем, есть ли уже такой товар в заявке
        existing = next((item for item in session['items'] if item['product_id'] == product_id), None)
        if existing:
            existing['quantity'] += qty
        else:
            session['items'].append({
                'product_id': product_id,
                'name': product['name'],
                'quantity': qty
            })

        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_change_item_'))
    def transfer_change_item(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[3])
        session = transfer_sessions.get(user_id)
        if not session:
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
        bot.register_next_step_handler_by_chat_id(session['chat_id'], process_transfer_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_cancel_item")
    def transfer_cancel_item(call):
        user_id = call.from_user.id
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_show_summary")
    def transfer_show_summary(call):
        user_id = call.from_user.id
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        if not session['items']:
            bot.answer_callback_query(call.id, "❌ Нет добавленных товаров")
            return

        lines = [f"• {item['name']}: {item['quantity']} шт" for item in session['items']]
        summary = "\n".join(lines)

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Отправить заявку", callback_data="transfer_submit"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="transfer_add_more"),
            types.InlineKeyboardButton("❌ Отменить", callback_data="transfer_cancel")
        )

        bot.edit_message_text(
            f"📦 *Состав заявки*\n\n{summary}\n\nПодтвердите отправку:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_add_more")
    def transfer_add_more(call):
        user_id = call.from_user.id
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        bot.delete_message(session['chat_id'], call.message.message_id)
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_submit")
    def transfer_submit(call):
        user_id = call.from_user.id
        session = transfer_sessions.pop(user_id, None)
        if not session or not session['items']:
            bot.answer_callback_query(call.id, "❌ Нет товаров в заявке")
            return

        seller = get_seller_by_id(session['seller_id'])
        # Создаём заявку в БД
        items_for_db = [{'product_id': item['product_id'], 'quantity': item['quantity']} for item in session['items']]
        request_id = create_transfer_request(session['seller_id'], items_for_db)

        # Отправляем уведомление кладовщику
        hub_seller = get_seller_by_id(HUB_SELLER_ID)
        if hub_seller:
            lines = [f"• {item['name']}: {item['quantity']} шт" for item in session['items']]
            items_text = "\n".join(lines)

            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
            )

            try:
                bot.send_message(
                    hub_seller['telegram_id'],
                    f"📦 *Новая заявка на перемещение*\n\n"
                    f"Продавец: {seller['name']}\n"
                    f"Запрашивает:\n{items_text}",
                    parse_mode='Markdown',
                    reply_markup=markup
                )
                logger.info(f"Заявка {request_id} отправлена кладовщику")
            except Exception as e:
                logger.error(f"Ошибка отправки кладовщику: {e}")

        bot.edit_message_text(
            f"✅ Заявка №{request_id} отправлена кладовщику. Ожидайте подтверждения.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Заявка отправлена")

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_cancel")
    def transfer_cancel(call):
        user_id = call.from_user.id
        transfer_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Создание заявки отменено.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)

    # ---------- Обработчики для кладовщика (подтверждение/отклонение) ----------
    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_approve_'))
    def approve_transfer(call):
        user_id = call.from_user.id
        logger.info(f"✅ Нажата кнопка подтверждения заявки пользователем {user_id}")

        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            logger.warning(f"Пользователь {user_id} не является кладовщиком")
            bot.answer_callback_query(call.id, "❌ У вас нет прав для подтверждения.")
            return

        request_id = int(call.data.split('_')[2])
        logger.info(f"Подтверждение заявки {request_id}")

        req = get_transfer_request(request_id)
        if not req:
            logger.error(f"Заявка {request_id} не найдена")
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return

        if req['status'] != 'pending':
            logger.info(f"Заявка {request_id} уже имеет статус {req['status']}")
            bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
            return

        # Проверяем наличие всех товаров на складе хаба
        insufficient = []
        for item in req['items']:
            stock = get_seller_stock(HUB_SELLER_ID, item['product_id'])
            if stock < item['quantity']:
                insufficient.append(f"{item['product_name']} (доступно {stock}, требуется {item['quantity']})")

        if insufficient:
            msg = "❌ Недостаточно товара на хабе:\n" + "\n".join(insufficient)
            logger.warning(f"Заявка {request_id} отклонена из-за недостатка: {msg}")
            bot.answer_callback_query(call.id, msg, show_alert=True)
            return

        # Выполняем перемещение
        try:
            for item in req['items']:
                decrease_seller_stock(
                    seller_id=HUB_SELLER_ID,
                    product_id=item['product_id'],
                    quantity=item['quantity'],
                    reason='transfer_out',
                    order_id=None
                )
                increase_seller_stock(
                    seller_id=req['to_seller_id'],
                    product_id=item['product_id'],
                    quantity=item['quantity'],
                    reason='transfer_in',
                    order_id=None
                )
            logger.info(f"Перемещение по заявке {request_id} выполнено успешно")
        except Exception as e:
            logger.error(f"Ошибка при перемещении товаров: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка при перемещении", show_alert=True)
            return

        update_transfer_request_status(request_id, 'approved')

        # Уведомляем продавца
        seller_to = get_seller_by_id(req['to_seller_id'])
        if seller_to:
            try:
                bot.send_message(
                    seller_to['telegram_id'],
                    f"✅ Ваша заявка на перемещение (№{request_id}) подтверждена!\n"
                    f"Товары поступили на ваш склад."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления продавца: {e}")

        bot.edit_message_text(
            f"✅ Заявка {request_id} подтверждена, перемещение выполнено.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Заявка подтверждена")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_reject_'))
    def reject_transfer(call):
        user_id = call.from_user.id
        logger.info(f"❌ Нажата кнопка отклонения заявки пользователем {user_id}")

        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            logger.warning(f"Пользователь {user_id} не является кладовщиком")
            bot.answer_callback_query(call.id, "❌ У вас нет прав.")
            return

        request_id = int(call.data.split('_')[2])
        logger.info(f"Отклонение заявки {request_id}")

        req = get_transfer_request(request_id)
        if not req:
            logger.error(f"Заявка {request_id} не найдена")
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return

        if req['status'] != 'pending':
            logger.info(f"Заявка {request_id} уже имеет статус {req['status']}")
            bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
            return

        update_transfer_request_status(request_id, 'rejected')

        seller_to = get_seller_by_id(req['to_seller_id'])
        if seller_to:
            try:
                bot.send_message(
                    seller_to['telegram_id'],
                    f"❌ Ваша заявка на перемещение (№{request_id}) отклонена кладовщиком."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления продавца: {e}")

        bot.edit_message_text(
            f"❌ Заявка {request_id} отклонена.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Заявка отклонена")

    # Обработчик кнопки из предупреждения о минусах
    @bot.callback_query_handler(func=lambda call: call.data == "create_transfer_request")
    def handle_create_transfer_request(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] == HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        # Начинаем новую сессию
        transfer_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': [],
            'message_id': None,
            'chat_id': call.message.chat.id
        }
        bot.edit_message_text(
            "🔄 Начинаем создание заявки на перемещение...",
            call.message.chat.id,
            call.message.message_id
        )
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)
