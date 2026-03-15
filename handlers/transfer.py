# handlers/transfer.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_all_products,
    get_product_variants, get_seller_stock, get_variant,
    create_transfer_request, add_transfer_request_item,
    get_transfer_request_with_items, update_transfer_request_status,
    decrease_seller_stock, increase_seller_stock,
    get_seller_stock_with_check, update_transfer_request_status_atomic
)
from config import HUB_SELLER_ID, ADMIN_ID

logger = logging.getLogger(__name__)

transfer_sessions = {}

def register_transfer_handlers(bot):
    def is_admin(user_id):
        return user_id == ADMIN_ID

    # ========== ОТЛАДОЧНЫЙ ОБРАБОТЧИК ==========
    # Этот обработчик ловит ВСЕ callback'и и логирует их
    # Он должен быть первым, чтобы видеть все входящие запросы
    @bot.callback_query_handler(func=lambda call: True)
    def debug_all_callbacks(call):
        """Отлавливает ВСЕ callback'и и логирует их"""
        logger.info("=" * 60)
        logger.info(f"🔥 ОТЛАДКА: Получен callback")
        logger.info(f"   User ID: {call.from_user.id}")
        logger.info(f"   Data: '{call.data}'")
        logger.info(f"   Длина: {len(call.data)} байт")
        logger.info(f"   Message ID: {call.message.message_id if call.message else 'Нет'}")
        logger.info("=" * 60)
        
        # Пробуем ответить, чтобы проверить связь
        try:
            bot.answer_callback_query(call.id, f"✅ Получено")
        except Exception as e:
            logger.error(f"❌ Ошибка при ответе на callback: {e}")
        
        # Возвращаем False, чтобы другие обработчики тоже могли сработать
        # Это важно! Если вернуть True, цепочка обработчиков прервётся
        return False

    @bot.message_handler(func=lambda m: m.text == "🔄 Заявка на перемещение")
    def handle_transfer_request_start(message):
        user_id = message.from_user.id
        logger.info(f"🔄 Начало создания заявки пользователем {user_id}")
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return

        if seller['id'] == HUB_SELLER_ID:
            bot.reply_to(
                message,
                "ℹ️ Управление заявками на перемещение осуществляется через раздел «Ожидают обработки»."
            )
            return

        transfer_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': {},
            'chat_id': message.chat.id
        }
        show_product_list(user_id)

    def show_product_list(user_id):
        session = transfer_sessions.get(user_id)
        if not session:
            return
        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("✅ Завершить", callback_data="transfer_finish"))
        bot.send_message(
            session['chat_id'],
            "🔄 *Создание заявки на перемещение*\n\nВыберите товар:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_prod_'))
    def select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        logger.info(f"🔘 Выбран товар {product_id} пользователем {user_id}")
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        variants = get_product_variants(product_id)
        transfer_variants = [v for v in variants if v['name'] != 'Россыпь']
        if not transfer_variants:
            bot.answer_callback_query(call.id, "❌ Нет вариантов для перемещения")
            return

        # Сохраняем название товара в сессии
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        session['product_name'] = product_name

        markup = types.InlineKeyboardMarkup(row_width=2)
        for v in transfer_variants:
            btn_text = f"{product_name} {v['name']}"
            markup.add(types.InlineKeyboardButton(
                btn_text,
                callback_data=f"transfer_var_{product_id}_{v['id']}"
            ))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="transfer_back_to_products"))
        bot.edit_message_text(
            f"Выберите фасовку:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_var_'))
    def select_variant(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        product_id = int(parts[2])
        variant_id = int(parts[3])
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return
        
        # Получаем название варианта
        variant = get_variant(variant_id)
        variant_name = variant['name'] if variant else "Неизвестный вариант"
        product_name = session.get('product_name', "Товар")
        
        session['current_product'] = product_id
        session['current_variant'] = variant_id
        session['variant_name'] = variant_name
        session['product_name'] = product_name
        
        bot.edit_message_text(
            f"Введите количество упаковок для *{product_name} ({variant_name})*:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(
            call.message.chat.id,
            process_quantity,
            user_id, product_id, variant_id
        )
        bot.answer_callback_query(call.id)

    def process_quantity(message, user_id, product_id, variant_id):
        session = transfer_sessions.get(user_id)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
            return

        try:
            qty = int(message.text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            product_name = session.get('product_name', "Товар")
            variant_name = session.get('variant_name', "Неизвестный вариант")
            bot.reply_to(message, f"❌ Введите положительное целое число для {product_name} ({variant_name}).")
            show_product_list(user_id)
            return

        session['items'][variant_id] = {
            'variant_id': variant_id,
            'product_id': product_id,
            'quantity': qty
        }
        logger.info(f"✅ Добавлена позиция variant {variant_id}, qty {qty}")
        show_summary(user_id)

    def show_summary(user_id):
        session = transfer_sessions.get(user_id)
        if not session:
            return
        items = session['items'].values()
        if not items:
            show_product_list(user_id)
            return

        lines = []
        for item in items:
            variant = get_variant(item['variant_id'])
            if variant:
                lines.append(f"• {variant['product_name']} ({variant['name']}): {item['quantity']} шт")
            else:
                lines.append(f"• Товар (вариант {item['variant_id']}): {item['quantity']} шт")
        summary = "\n".join(lines)

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить заявку", callback_data="transfer_confirm"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="transfer_add"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="transfer_cancel")
        )
        bot.send_message(
            session['chat_id'],
            f"📦 *Заявка на перемещение*\n\n{summary}\n\nПодтвердить?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_add")
    def transfer_add(call):
        user_id = call.from_user.id
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_back_to_products")
    def back_to_products(call):
        user_id = call.from_user.id
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

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

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_confirm")
    def transfer_confirm(call):
        user_id = call.from_user.id
        session = transfer_sessions.pop(user_id, None)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        items = list(session['items'].values())
        if not items:
            bot.answer_callback_query(call.id, "❌ Нет позиций для заявки")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != session['seller_id']:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        try:
            request_id = create_transfer_request(HUB_SELLER_ID, seller['id'])
            for item in items:
                add_transfer_request_item(request_id, item['variant_id'], item['quantity'])
            logger.info(f"✅ Заявка на перемещение {request_id} создана с {len(items)} позициями")
        except Exception as e:
            logger.exception(f"Ошибка при создании заявки: {e}")
            bot.answer_callback_query(call.id, "❌ Не удалось создать заявку из-за внутренней ошибки.", show_alert=True)
            return

        # Формируем текст для уведомлений
        lines = []
        for item in items:
            variant = get_variant(item['variant_id'])
            if variant:
                lines.append(f"• {variant['product_name']} ({variant['name']}): {item['quantity']} шт")
        items_text = "\n".join(lines)

        # Уведомляем кладовщика
        hub_seller = get_seller_by_id(HUB_SELLER_ID)
        if hub_seller:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
            )
            try:
                bot.send_message(
                    hub_seller['telegram_id'],
                    f"📦 *Новая заявка на перемещение №{request_id}*\n\n"
                    f"От: {seller['name']}\n"
                    f"{items_text}",
                    parse_mode='Markdown',
                    reply_markup=markup
                )
                logger.info(f"Уведомление о заявке {request_id} отправлено кладовщику")
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления кладовщику: {e}")

        # Уведомляем администратора
        if ADMIN_ID and ADMIN_ID != hub_seller['telegram_id']:
            try:
                admin_markup = types.InlineKeyboardMarkup()
                admin_markup.row(
                    types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                    types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
                )
                bot.send_message(
                    ADMIN_ID,
                    f"📦 *Новая заявка на перемещение №{request_id}*\n\n"
                    f"От: {seller['name']}\n"
                    f"{items_text}",
                    parse_mode='Markdown',
                    reply_markup=admin_markup
                )
                logger.info(f"Уведомление о заявке {request_id} отправлено админу")
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления админу: {e}")

        bot.edit_message_text(
            f"✅ Заявка на перемещение №{request_id} создана. Ожидайте подтверждения.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Заявка создана")

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_finish")
    def transfer_finish(call):
        user_id = call.from_user.id
        transfer_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Создание заявки завершено.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_approve_'))
    def approve_transfer(call):
        user_id = call.from_user.id
        logger.info(f"🔄 ПОЛУЧЕН ЗАПРОС НА ПОДТВЕРЖДЕНИЕ: {call.data}")
        
        try:
            # Проверяем формат данных
            parts = call.data.split('_')
            if len(parts) < 3:
                logger.error(f"❌ Неверный формат callback_data: {call.data}")
                bot.answer_callback_query(call.id, "❌ Ошибка формата данных", show_alert=True)
                return
            
            # Извлекаем ID заявки
            request_id_str = parts[2]
            try:
                request_id = int(request_id_str)
                logger.info(f"✅ ID заявки: {request_id}")
            except ValueError:
                logger.error(f"❌ Не удалось преобразовать '{request_id_str}' в число")
                bot.answer_callback_query(call.id, "❌ Неверный ID заявки", show_alert=True)
                return

            # Проверяем права
            seller = get_seller_by_telegram_id(user_id)
            if not seller:
                logger.warning(f"❌ Пользователь {user_id} не найден в таблице sellers")
                bot.answer_callback_query(call.id, "❌ Вы не авторизованы как продавец", show_alert=True)
                return

            if seller['id'] != HUB_SELLER_ID and not is_admin(user_id):
                logger.warning(f"❌ Нет прав у пользователя {user_id}. ID продавца: {seller['id']}, HUB_SELLER_ID: {HUB_SELLER_ID}")
                bot.answer_callback_query(call.id, "❌ У вас нет прав для подтверждения", show_alert=True)
                return

            logger.info(f"✅ Права подтверждены для пользователя {user_id}")

            # Получаем информацию о заявке
            request = get_transfer_request_with_items(request_id)
            if not request:
                logger.error(f"❌ Заявка {request_id} не найдена")
                bot.answer_callback_query(call.id, "❌ Заявка не найдена", show_alert=True)
                return

            logger.info(f"✅ Заявка {request_id} найдена, статус: {request['status']}")

            # Проверяем статус
            if request['status'] != 'pending':
                status_text = "подтверждена" if request['status'] == 'approved' else "отклонена"
                logger.warning(f"❌ Заявка уже {status_text}")
                bot.answer_callback_query(
                    call.id, 
                    f"❌ Заявка уже {status_text}. Повторное подтверждение невозможно.",
                    show_alert=True
                )
                return

            # Проверяем, достаточно ли товара у кладовщика
            insufficient_items = []
            for item in request['items']:
                current_stock = get_seller_stock_with_check(HUB_SELLER_ID, item['variant_id'])
                if current_stock < item['quantity']:
                    variant = get_variant(item['variant_id'])
                    product_name = variant['product_name'] if variant else "Неизвестный товар"
                    variant_name = variant['name'] if variant else "Неизвестный вариант"
                    insufficient_items.append(f"{product_name} ({variant_name}): есть {current_stock}, требуется {item['quantity']}")
            
            if insufficient_items:
                error_msg = "❌ Недостаточно товара у кладовщика:\n" + "\n".join(insufficient_items)
                logger.error(error_msg)
                bot.answer_callback_query(call.id, error_msg, show_alert=True)
                return

            # Атомарное обновление статуса
            logger.info(f"🔄 Попытка атомарного обновления статуса заявки {request_id}")
            if not update_transfer_request_status_atomic(request_id, 'approved'):
                logger.warning(f"❌ Не удалось обновить статус заявки {request_id} (возможно, уже обработана)")
                bot.answer_callback_query(
                    call.id, 
                    "❌ Заявка уже обрабатывается или была обработана ранее.",
                    show_alert=True
                )
                return

            logger.info(f"✅ Статус заявки {request_id} успешно обновлён")

            # Определяем, кто подтверждает
            completer_name = "Администратор" if is_admin(user_id) else seller['name']
            completer_display = completer_name

            # Выполняем перемещение
            try:
                for item in request['items']:
                    logger.info(f"🔄 Перемещение: кладовщик {HUB_SELLER_ID} -> продавец {request['to_seller_id']}, "
                               f"variant {item['variant_id']}, quantity {item['quantity']}")
                    
                    decrease_seller_stock(
                        seller_id=HUB_SELLER_ID,
                        variant_id=item['variant_id'],
                        quantity=item['quantity'],
                        reason='transfer_out',
                        order_id=None
                    )
                    increase_seller_stock(
                        seller_id=request['to_seller_id'],
                        variant_id=item['variant_id'],
                        quantity=item['quantity'],
                        reason='transfer_in',
                        order_id=None
                    )
                
                logger.info(f"✅ Заявка {request_id} подтверждена {completer_display}, перемещение выполнено")
                
            except Exception as e:
                logger.error(f"❌ Ошибка при перемещении: {e}")
                # В случае ошибки не откатываем статус, но логируем
                bot.answer_callback_query(
                    call.id, 
                    "❌ Ошибка при перемещении. Проверьте логи.",
                    show_alert=True
                )
                return

            # Формируем детальное сообщение о полученных товарах
            items_received = []
            for item in request['items']:
                variant = get_variant(item['variant_id'])
                if variant:
                    # Склоняем слово "упаковка" в зависимости от количества
                    if item['quantity'] % 10 == 1 and item['quantity'] % 100 != 11:
                        pack_word = "упаковку"
                    elif 2 <= item['quantity'] % 10 <= 4 and (item['quantity'] % 100 < 10 or item['quantity'] % 100 >= 20):
                        pack_word = "упаковки"
                    else:
                        pack_word = "упаковок"
                        
                    items_received.append(f"• {variant['product_name']} ({variant['name']}) {item['quantity']} {pack_word}")
            
            items_text = "\n".join(items_received)

            # Получаем информацию о продавце, который получил товар
            seller_to = get_seller_by_id(request['to_seller_id'])
            seller_to_name = seller_to['name'] if seller_to else "Неизвестный продавец"

            # Уведомление для продавца, который получил товар
            if seller_to:
                try:
                    bot.send_message(
                        seller_to['telegram_id'],
                        f"✅ *Заявка на перемещение остатков №{request_id}*\n"
                        f"Исполнена *{completer_display}*\n\n"
                        f"Вы получили:\n{items_text}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление о подтверждении отправлено продавцу {seller_to['telegram_id']}")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления продавца: {e}")

            # Уведомление для кладовщика (всегда)
            hub_seller = get_seller_by_id(HUB_SELLER_ID)
            if hub_seller:
                try:
                    bot.send_message(
                        hub_seller['telegram_id'],
                        f"✅ *Заявка на перемещение остатков №{request_id}*\n"
                        f"Исполнена *{completer_display}*\n\n"
                        f"Продавец *{seller_to_name}* получил:\n{items_text}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление о подтверждении отправлено кладовщику")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления кладовщика: {e}")

            # Уведомление для администратора (всегда)
            if ADMIN_ID:
                try:
                    bot.send_message(
                        ADMIN_ID,
                        f"✅ *Заявка на перемещение остатков №{request_id}*\n"
                        f"Исполнена *{completer_display}*\n\n"
                        f"Продавец *{seller_to_name}* получил:\n{items_text}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление о подтверждении отправлено администратору")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления администратора: {e}")

            # Обновляем сообщение, из которого была нажата кнопка
            try:
                bot.edit_message_text(
                    f"✅ *Заявка {request_id} подтверждена {completer_display}*\n\n"
                    f"Продавец *{seller_to_name}* получил:\n{items_text}",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='Markdown'
                )
                logger.info(f"✅ Сообщение успешно обновлено")
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.error(f"❌ Не удалось отредактировать сообщение: {e}")
                # Игнорируем ошибку "message is not modified"

            bot.answer_callback_query(call.id, "✅ Заявка подтверждена")
            
        except Exception as e:
            logger.exception(f"❌ Критическая ошибка в approve_transfer: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Внутренняя ошибка", show_alert=True)
            except:
                pass

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_reject_'))
    def reject_transfer(call):
        user_id = call.from_user.id
        logger.info(f"❌ ПОЛУЧЕН ЗАПРОС НА ОТКЛОНЕНИЕ: {call.data}")
        
        try:
            parts = call.data.split('_')
            if len(parts) < 3:
                logger.error(f"❌ Неверный формат callback_data: {call.data}")
                bot.answer_callback_query(call.id, "❌ Ошибка формата данных", show_alert=True)
                return
            
            request_id = int(parts[2])
            logger.info(f"✅ ID заявки: {request_id}")

            seller = get_seller_by_telegram_id(user_id)
            if not seller or (seller['id'] != HUB_SELLER_ID and not is_admin(user_id)):
                logger.warning(f"❌ Нет прав у пользователя {user_id}")
                bot.answer_callback_query(call.id, "❌ У вас нет прав.", show_alert=True)
                return

            request = get_transfer_request_with_items(request_id)
            if not request:
                logger.error(f"❌ Заявка {request_id} не найдена")
                bot.answer_callback_query(call.id, "❌ Заявка не найдена", show_alert=True)
                return

            if request['status'] != 'pending':
                status_text = "подтверждена" if request['status'] == 'approved' else "отклонена"
                logger.warning(f"❌ Заявка уже {status_text}")
                bot.answer_callback_query(
                    call.id, 
                    f"❌ Заявка уже {status_text}. Повторное отклонение невозможно.",
                    show_alert=True
                )
                return

            # Атомарное обновление статуса
            if not update_transfer_request_status_atomic(request_id, 'rejected'):
                logger.warning(f"❌ Не удалось обновить статус заявки {request_id}")
                bot.answer_callback_query(
                    call.id, 
                    "❌ Заявка уже обрабатывается или была обработана ранее.",
                    show_alert=True
                )
                return

            completer_name = "Администратор" if is_admin(user_id) else seller['name']
            completer_display = completer_name

            logger.info(f"✅ Заявка {request_id} отклонена {completer_display}")

            seller_to = get_seller_by_id(request['to_seller_id'])
            seller_to_name = seller_to['name'] if seller_to else "Неизвестный продавец"

            if seller_to:
                try:
                    bot.send_message(
                        seller_to['telegram_id'],
                        f"❌ *Заявка на перемещение №{request_id}*\n"
                        f"Отклонена *{completer_display}*.",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление об отклонении отправлено продавцу {seller_to['telegram_id']}")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления продавца: {e}")

            # Уведомление для кладовщика
            hub_seller = get_seller_by_id(HUB_SELLER_ID)
            if hub_seller:
                try:
                    bot.send_message(
                        hub_seller['telegram_id'],
                        f"❌ *Заявка на перемещение №{request_id}*\n"
                        f"Отклонена *{completer_display}*.\n"
                        f"Продавец: {seller_to_name}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление об отклонении отправлено кладовщику")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления кладовщика: {e}")

            # Уведомление для администратора
            if ADMIN_ID:
                try:
                    bot.send_message(
                        ADMIN_ID,
                        f"❌ *Заявка на перемещение №{request_id}*\n"
                        f"Отклонена *{completer_display}*.\n"
                        f"Продавец: {seller_to_name}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Уведомление об отклонении отправлено администратору")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления администратора: {e}")

            try:
                bot.edit_message_text(
                    f"❌ *Заявка {request_id} отклонена {completer_display}*.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='Markdown'
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.error(f"❌ Не удалось отредактировать сообщение: {e}")

            bot.answer_callback_query(call.id, "✅ Заявка отклонена")
            
        except Exception as e:
            logger.exception(f"❌ Критическая ошибка в reject_transfer: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Внутренняя ошибка", show_alert=True)
            except:
                pass
