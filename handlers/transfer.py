# handlers/transfer.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_all_products,
    get_product_variants, get_seller_stock,
    create_transfer_request, get_transfer_request, update_transfer_request_status,
    decrease_seller_stock, increase_seller_stock, get_variant
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

        products = get_all_products()
        if not products:
            bot.reply_to(message, "❌ Нет товаров в каталоге.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
        bot.send_message(
            message.chat.id,
            "🔄 *Создание заявки на перемещение*\n\nВыберите товар:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_prod_'))
    def select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] == HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
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
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] == HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        transfer_sessions[user_id] = {
            'product_id': product_id,
            'variant_id': variant_id,
            'chat_id': call.message.chat.id,
            'message_id': call.message.message_id
        }
        bot.edit_message_text(
            f"Введите количество упаковок:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_transfer_quantity, user_id, product_id, variant_id)
        bot.answer_callback_query(call.id)

    def process_transfer_quantity(message, user_id, product_id, variant_id):
        session = transfer_sessions.pop(user_id, None)
        if not session:
            bot.reply_to(message, "❌ Сессия истекла. Начните заново.")
            return
        try:
            qty = int(message.text.strip())
            if qty <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] == HUB_SELLER_ID:
            bot.reply_to(message, "❌ Ошибка доступа.")
            return

        request_id = create_transfer_request(seller['id'], variant_id, qty)
        hub_seller = get_seller_by_id(HUB_SELLER_ID)
        if hub_seller:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
            )
            variant = get_variant(variant_id)
            product_name = variant['product_name'] if variant else "Товар"
            variant_name = variant['name'] if variant else "Неизвестный вариант"
            try:
                bot.send_message(
                    hub_seller['telegram_id'],
                    f"📦 *Новая заявка на перемещение*\n\n"
                    f"От: {seller['name']}\n"
                    f"Товар: {product_name} ({variant_name})\n"
                    f"Количество: {qty} упаковок",
                    parse_mode='Markdown',
                    reply_markup=markup
                )
                logger.info(f"Заявка {request_id} отправлена кладовщику")
                bot.reply_to(message, f"✅ Заявка на перемещение создана (№{request_id}). Ожидайте подтверждения.")
            except Exception as e:
                logger.error(f"Ошибка отправки кладовщику: {e}")
                bot.reply_to(message, "❌ Не удалось уведомить кладовщика, но заявка сохранена.")
        else:
            bot.reply_to(message, f"✅ Заявка на перемещение создана (№{request_id}).")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_approve_'))
    def approve_transfer(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для подтверждения.")
            return
        request_id = int(call.data.split('_')[2])
        req = get_transfer_request(request_id)
        if not req:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if req['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
            return
        try:
            decrease_seller_stock(
                seller_id=HUB_SELLER_ID,
                variant_id=req['variant_id'],
                quantity=req['quantity'],
                reason='transfer_out',
                order_id=None
            )
            increase_seller_stock(
                seller_id=req['to_seller_id'],
                variant_id=req['variant_id'],
                quantity=req['quantity'],
                reason='transfer_in',
                order_id=None
            )
        except Exception as e:
            logger.error(f"Ошибка при перемещении: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка при перемещении (возможно, недостаточно товара на хабе).", show_alert=True)
            return
        update_transfer_request_status(request_id, 'approved')
        seller_to = get_seller_by_id(req['to_seller_id'])
        if seller_to:
            try:
                variant = get_variant(req['variant_id'])
                bot.send_message(
                    seller_to['telegram_id'],
                    f"✅ Ваша заявка на перемещение (№{request_id}) подтверждена!\n"
                    f"Товар: {variant['product_name']} ({variant['name']})\n"
                    f"Количество: {req['quantity']}"
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
        req = get_transfer_request(request_id)
        if not req:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if req['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {req['status']}")
            return
        update_transfer_request_status(request_id, 'rejected')
        seller_to = get_seller_by_id(req['to_seller_id'])
        if seller_to:
            try:
                variant = get_variant(req['variant_id'])
                bot.send_message(
                    seller_to['telegram_id'],
                    f"❌ Ваша заявка на перемещение (№{request_id}) отклонена кладовщиком.\n"
                    f"Товар: {variant['product_name']} ({variant['name']})\n"
                    f"Количество: {req['quantity']}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления продавца: {e}")
        bot.edit_message_text(
            f"❌ Заявка {request_id} отклонена.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Заявка отклонена")

    @bot.callback_query_handler(func=lambda call: call.data == "transfer_back_to_products")
    def back_to_products(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] == HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
        bot.edit_message_text(
            "🔄 *Создание заявки на перемещение*\n\nВыберите товар:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
