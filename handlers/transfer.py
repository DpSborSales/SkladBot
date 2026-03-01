import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_all_products,
    create_transfer_request, get_transfer_request, update_transfer_request_status,
    decrease_seller_stock, increase_seller_stock
)
from config import HUB_SELLER_ID

logger = logging.getLogger(__name__)

# Сессии для создания заявок (можно использовать общий словарь, но лучше локальный)
transfer_sessions = {}

def register_transfer_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "🔄 Заявка на перемещение")
    def handle_transfer_request_start(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return
        products = get_all_products()
        if not products:
            bot.reply_to(message, "❌ Нет товаров в каталоге.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for p in products:
            buttons.append(types.InlineKeyboardButton(p['name'], callback_data=f"transfer_prod_{p['id']}"))
        markup.add(*buttons)
        bot.send_message(
            message.chat.id,
            "🔄 *Создание заявки на перемещение*\n\nВыберите товар, который хотите получить:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('transfer_prod_'))
    def transfer_product_selected(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return
        transfer_sessions[user_id] = {
            'product_id': product_id,
            'chat_id': call.message.chat.id,
            'message_id': call.message.message_id
        }
        bot.edit_message_text(
            f"Введите количество для товара:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_transfer_quantity, user_id, product_id)
        bot.answer_callback_query(call.id)

    def process_transfer_quantity(message, user_id, product_id):
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
        if not seller:
            bot.reply_to(message, "❌ Ошибка доступа.")
            return
        request_id = create_transfer_request(seller['id'], product_id, qty)
        hub_seller = get_seller_by_id(HUB_SELLER_ID)
        if hub_seller:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_approve_{request_id}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{request_id}")
            )
            products = get_all_products()
            product_name = next((p['name'] for p in products if p['id'] == product_id), f"Товар {product_id}")
            try:
                bot.send_message(
                    hub_seller['telegram_id'],
                    f"📦 *Новая заявка на перемещение*\n\n"
                    f"От: {seller['name']}\n"
                    f"Товар: {product_name}\n"
                    f"Количество: {qty}",
                    parse_mode='Markdown',
                    reply_markup=markup
                )
                logger.info(f"Заявка {request_id} отправлена кладовщику")
            except Exception as e:
                logger.error(f"Ошибка отправки кладовщику: {e}")
        bot.reply_to(message, f"✅ Заявка на перемещение создана (№{request_id}). Ожидайте подтверждения.")

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
                product_id=req['product_id'],
                quantity=req['quantity'],
                reason='transfer_out',
                order_id=None
            )
            increase_seller_stock(
                seller_id=req['to_seller_id'],
                product_id=req['product_id'],
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
                products = get_all_products()
                product_name = next((p['name'] for p in products if p['id'] == req['product_id']), f"Товар {req['product_id']}")
                bot.send_message(
                    seller_to['telegram_id'],
                    f"✅ Ваша заявка на перемещение (№{request_id}) подтверждена!\n"
                    f"Товар: {product_name}\n"
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
                products = get_all_products()
                product_name = next((p['name'] for p in products if p['id'] == req['product_id']), f"Товар {req['product_id']}")
                bot.send_message(
                    seller_to['telegram_id'],
                    f"❌ Ваша заявка на перемещение (№{request_id}) отклонена кладовщиком.\n"
                    f"Товар: {product_name}\n"
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
