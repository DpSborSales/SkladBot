# handlers/transfer.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_all_products,
    get_product_variants, get_seller_stock, get_variant,
    create_transfer_request, add_transfer_request_item,
    get_transfer_request_with_items, update_transfer_request_status,
    decrease_seller_stock, increase_seller_stock
)
from config import HUB_SELLER_ID

logger = logging.getLogger(__name__)

transfer_sessions = {}

def register_transfer_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "🔄 Заявка на перемещение")
    def handle_transfer_request_start(message):
        user_id = message.from_user.id
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
        session = transfer_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        variants = get_product_variants(product_id)
        transfer_variants = [v for v in variants if v['name'] != 'Россыпь']
        if not transfer_variants:
            bot.answer_callback_query(call.id, "❌ Нет вариантов для перемещения")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
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
        session['current_product'] = product_id
        session['current_variant'] = variant_id
        bot.edit_message_text(
            f"Введите количество упаковок:",
            call.message.chat.id,
            call.message.message_id
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
            bot.reply_to(message, "❌ Введите положительное целое число.")
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

        # Отправляем уведомление кладовщику
        hub_seller = get_seller_by_id(HUB_SELLER_ID)
        if hub_seller:
            try:
                # Формируем текст с позициями
                lines = []
                for item in items:
                    variant = get_variant(item['variant_id'])
                    if variant:
                        lines.append(f"• {variant['product_name']} ({variant['name']}): {item['quantity']} шт")
                items_text = "\n".join(lines)

                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                    types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
                )
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

        bot.edit_message_text(
            f"✅ Заявка на перемещение №{request_id} создана. Ожидайте подтверждения кладовщиком.",
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
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для подтверждения.")
            return
        request_id = int(call.data.split('_')[2])
        request = get_transfer_request_with_items(request_id)
        if not request:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if request['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {request['status']}")
            return

        try:
            for item in request['items']:
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
        except Exception as e:
            logger.error(f"Ошибка при перемещении: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка при перемещении (возможно, недостаточно товара на хабе).", show_alert=True)
            return

        update_transfer_request_status(request_id, 'approved')
        seller_to = get_seller_by_id(request['to_seller_id'])
        if seller_to:
            try:
                bot.send_message(
                    seller_to['telegram_id'],
                    f"✅ Ваша заявка на перемещение (№{request_id}) подтверждена!\n"
                    f"Товары перемещены на ваш склад."
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
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав.")
            return
        request_id = int(call.data.split('_')[2])
        request = get_transfer_request_with_items(request_id)
        if not request:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if request['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {request['status']}")
            return
        update_transfer_request_status(request_id, 'rejected')
        seller_to = get_seller_by_id(request['to_seller_id'])
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
