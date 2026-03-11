import logging
from telebot import types
from models import (
    get_order_by_number, get_seller_by_telegram_id, get_all_products,
    get_product_variants, decrease_seller_stock, mark_order_as_processed,
    get_negative_stock_summary, get_variant, update_order_total, get_db_connection
)
from notifications import send_negative_stock_warning

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
            logger.error(f"Заказ {order_num} не найден")
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
            return

        if order.get('stock_processed'):
            bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
            return

        # Сумма не меняется, используем исходную
        for item in order['items']:
            variant_id = item.get('variantId')
            if not variant_id:
                logger.error(f"В заказе {order_num} отсутствует variantId")
                bot.answer_callback_query(call.id, "❌ Ошибка данных заказа")
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
            'seller_id': seller['id'],
            'order_id': order['id'],
            'selected_items': {},  # ключ (product_id, variant_id) -> количество
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
        product_dict = {p['id']: p['name'] for p in products}

        selected_lines = []
        for (pid, vid), qty in session['selected_items'].items():
            if qty <= 0:
                continue
            variant = get_variant(vid)
            variant_name = variant['name'] if variant else "Неизвестный вариант"
            product_name = product_dict.get(pid, "Неизвестный товар")
            selected_lines.append(f"{product_name} ({variant_name}): {qty} шт")
        summary = "\n".join(selected_lines)

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for p in products:
            buttons.append(types.InlineKeyboardButton(
                p['name'],
                callback_data=f"selprod_{session['order_number']}_{p['id']}"
            ))
        markup.add(*buttons)
        markup.row(types.InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{session['order_number']}"))

        text = f"✏️ *Редактирование заказа {session['order_number']}*\n\n"
        if summary:
            text += f"*Уже выбрано:*\n{summary}\n\n"
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

        variants = get_product_variants(product_id)
        variants = [v for v in variants if v['name'] != 'Россыпь']
        if not variants:
            bot.answer_callback_query(call.id, "❌ У товара нет доступных вариантов")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        for v in variants:
            btn_text = f"{product_name} {v['name']}"
            markup.add(types.InlineKeyboardButton(
                btn_text,
                callback_data=f"selvar_{order_num}_{product_id}_{v['id']}"
            ))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"backtoproducts_{order_num}"))
        bot.edit_message_text(
            f"Выберите фасовку:",
            session['chat_id'],
            session['message_id'],
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('selvar_'))
    def select_variant(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        order_num = parts[1]
        product_id = int(parts[2])
        variant_id = int(parts[3])
        logger.info(f"🔘 Выбран вариант {variant_id} для товара {product_id} в заказе {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        session['current_product'] = product_id
        session['current_variant'] = variant_id

        variant = get_variant(variant_id)
        variant_name = variant['name'] if variant else "Неизвестный вариант"
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")

        bot.edit_message_text(
            f"Введите количество для *{product_name} ({variant_name})*:",
            session['chat_id'],
            session['message_id'],
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(
            session['chat_id'],
            process_quantity_input,
            user_id, order_num, product_id, variant_id
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('backtoproducts_'))
    def back_to_products(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        show_product_selection(user_id)
        bot.answer_callback_query(call.id)

    def process_quantity_input(message, user_id, order_num, product_id, variant_id):
        logger.info(f"📝 Ввод количества для товара {product_id}, вариант {variant_id}, заказ {order_num}")
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

        key = (product_id, variant_id)
        if qty == 0:
            if key in session['selected_items']:
                del session['selected_items'][key]
                logger.info(f"✅ Позиция {key} удалена (количество 0)")
        else:
            session['selected_items'][key] = qty
            logger.info(f"✅ Количество для варианта {variant_id} установлено: {qty}")

        show_product_selection(user_id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('finish_'))
    def finish_edit(call):
        user_id = call.from_user.id
        order_num = call.data.split('_')[1]
        logger.info(f"🏁 Завершение редактирования заказа {order_num}")

        session = edit_sessions.get(user_id)
        if not session or session['order_number'] != order_num:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        positive_items = {k: v for k, v in session['selected_items'].items() if v > 0}
        if not positive_items:
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
        product_dict = {p['id']: p['name'] for p in products}
        
        # Пересчитываем новую сумму заказа (по цене покупателя)
        new_total = 0
        lines = []
        for (pid, vid), qty in positive_items.items():
            variant = get_variant(vid)
            if variant:
                variant_name = variant['name'] if variant else "Неизвестный вариант"
                product_name = product_dict.get(pid, "Неизвестный товар")
                # Используем цену покупателя (price)
                lines.append(f"• {product_name} ({variant_name}): {qty} шт × {variant['price']} руб. = {variant['price'] * qty} руб.")
                new_total += variant['price'] * qty
                logger.info(f"💰 Товар {product_name} ({variant_name}) - {qty} шт по {variant['price']} руб. (цена покупателя), сумма: {variant['price'] * qty}")
            else:
                lines.append(f"• Товар (вариант {vid}): {qty} упаковок")
        
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
            f"*Итого: {new_total} руб.*\n\n"
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
            logger.error(f"apply_edit: заказ {order_num} не найден")
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or order['seller_id'] != seller['id']:
            bot.answer_callback_query(call.id, "❌ Этот заказ не ваш")
            return

        if order.get('stock_processed'):
            bot.answer_callback_query(call.id, "✅ Заказ уже обработан")
            return

        # Берём только позиции с положительным количеством
        selected = {k: v for k, v in session['selected_items'].items() if v > 0}
        if not selected:
            bot.answer_callback_query(call.id, "❌ Нет товаров для списания")
            return

        # Пересчитываем общую сумму заказа на основе выбранных товаров (по цене покупателя)
        new_total = 0
        updated_items = []
        for (pid, vid), qty in selected.items():
            # Получаем информацию о варианте
            variant = get_variant(vid)
            if variant:
                # Используем цену покупателя (price)
                new_total += variant['price'] * qty
                logger.info(f"💰 Товар {variant['product_name']} ({variant['name']}) - {qty} шт по {variant['price']} руб. (цена покупателя), сумма: {variant['price'] * qty}")
                
                # Формируем обновлённый элемент заказа
                updated_items.append({
                    'productId': pid,
                    'variantId': vid,
                    'name': variant['product_name'],
                    'variantName': variant['name'],
                    'quantity': qty,
                    'price': variant['price'],
                    'price_seller': variant['price_seller']  # сохраняем для расчётов
                })
            else:
                logger.error(f"Вариант {vid} не найден")
                bot.answer_callback_query(call.id, f"❌ Товар с variant_id {vid} не найден", show_alert=True)
                return

        # Обновляем поле total и items в заказе
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Обновляем и сумму, и состав заказа
                    cur.execute(
                        "UPDATE orders SET total = %s, items = %s WHERE id = %s",
                        (new_total, json.dumps(updated_items), order['id'])
                    )
                    conn.commit()
            logger.info(f"✅ Обновлена сумма заказа {order_num}: {order['total']} -> {new_total}")
            logger.info(f"✅ Обновлён состав заказа {order_num}")
            
            # Проверяем, что обновление действительно произошло
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT total FROM orders WHERE id = %s", (order['id'],))
                    updated = cur.fetchone()
                    logger.info(f"✅ Проверка: в БД теперь total = {updated['total']}")
                    
        except Exception as e:
            logger.error(f"Ошибка при обновлении заказа: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка обновления заказа", show_alert=True)
            return

        # Списание по каждому выбранному варианту (используем variant_id, количество уже учтено)
        for (pid, vid), qty in selected.items():
            if qty > 0:
                decrease_seller_stock(
                    seller_id=seller['id'],
                    variant_id=vid,
                    quantity=qty,
                    reason='sale',
                    order_id=order['id']
                )
                logger.info(f"✅ Списано {qty} ед. товара variant {vid}")

        mark_order_as_processed(order['id'])
        logger.info(f"✅ Заказ {order_num} обработан, списано товаров: {len(selected)}")

        bot.edit_message_text(
            f"✅ Заказ {order_num} обработан.\nСумма заказа: {new_total} руб.",
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

        # Сумма не меняется, используем исходную
        for item in order['items']:
            variant_id = item.get('variantId')
            if not variant_id:
                logger.error(f"В заказе {order_num} отсутствует variantId")
                bot.answer_callback_query(call.id, "❌ Ошибка данных заказа")
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
            f"✅ Заказ {order_num} проведён без изменений.\nСумма заказа: {order['total']} руб.",
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
