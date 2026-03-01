# handlers/payments.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_seller_by_id, get_seller_debt,
    get_seller_profit, create_payment_request, get_payment_request,
    update_payment_status
)
from config import ADMIN_ID
from keyboards import main_keyboard

logger = logging.getLogger(__name__)

payment_sessions = {}

def register_payment_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "💰 Выплата админу")
    def handle_payment(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.reply_to(message, "❌ У вас нет доступа.")
            return
        debt, _, _ = get_seller_debt(seller['id'])
        profit, _, _ = get_seller_profit(seller['id'])
        msg = (
            f"💰 *Ваш расчётный счёт*\n\n"
            f"Вы должны перевести Админу: *{debt} руб.*\n"
            f"___________________________________________\n"
            f"Ваша чистая прибыль за всё время: *{profit} руб.*"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Произвести выплату", callback_data="make_payment"))
        bot.send_message(message.chat.id, msg, parse_mode='Markdown', reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data == "make_payment")
    def make_payment(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return
        debt, _, _ = get_seller_debt(seller['id'])
        bot.edit_message_text(
            f"💳 Ваш долг: *{debt} руб.*\n\nВведите сумму, которую передаёте Админу:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_payment_amount, seller['id'], call.message.chat.id)
        bot.answer_callback_query(call.id)

    def process_payment_amount(message, seller_id, original_chat_id):
        try:
            amount = int(message.text.strip())
            if amount <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            return
        payment_id = create_payment_request(seller_id, amount)
        seller = get_seller_by_id(seller_id)
        debt, _, _ = get_seller_debt(seller_id)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"payment_confirm_{payment_id}_{amount}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"payment_edit_{payment_id}")
        )
        try:
            bot.send_message(
                ADMIN_ID,
                f"💸 *Запрос на выплату*\n\n"
                f"Продавец: {seller['name']}\n"
                f"Долг: {debt} руб.\n"
                f"Передаёт: {amount} руб.\n\n"
                f"Всё верно?",
                parse_mode='Markdown',
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Ошибка отправки админу: {e}")
            bot.reply_to(message, "❌ Не удалось уведомить администратора.")
            return
        bot.reply_to(message, f"✅ Запрос на выплату {amount} руб. отправлен администратору. Ожидайте подтверждения.")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('payment_confirm_'))
    def payment_confirm(call):
        user_id = call.from_user.id
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав.")
            return
        parts = call.data.split('_')
        payment_id = int(parts[2])
        amount = int(parts[3])
        payment = get_payment_request(payment_id)
        if not payment:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if payment['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {payment['status']}")
            return
        update_payment_status(payment_id, 'confirmed', confirmed_amount=amount)
        seller = get_seller_by_id(payment['seller_id'])
        if seller:
            debt, _, _ = get_seller_debt(payment['seller_id'])
            try:
                bot.send_message(
                    seller['telegram_id'],
                    f"✅ Админ подтвердил получение *{amount} руб.*\n"
                    f"Ваш долг составляет *{debt} руб.*",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления продавца: {e}")
        bot.edit_message_text(
            f"✅ Вы подтвердили получение {amount} руб. от продавца.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "✅ Подтверждено")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('payment_edit_'))
    def payment_edit(call):
        user_id = call.from_user.id
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав.")
            return
        payment_id = int(call.data.split('_')[2])
        payment = get_payment_request(payment_id)
        if not payment:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена")
            return
        if payment['status'] != 'pending':
            bot.answer_callback_query(call.id, f"✅ Заявка уже {payment['status']}")
            return
        bot.edit_message_text(
            f"✏️ Введите сумму, которую вы реально получили:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_edit_payment, payment_id, call.message.chat.id)
        bot.answer_callback_query(call.id)

    def process_edit_payment(message, payment_id, original_chat_id):
        try:
            amount = int(message.text.strip())
            if amount <= 0:
                raise ValueError
        except:
            bot.reply_to(message, "❌ Введите положительное целое число.")
            return
        payment = get_payment_request(payment_id)
        if not payment:
            bot.reply_to(message, "❌ Заявка не найдена")
            return
        update_payment_status(payment_id, 'confirmed', confirmed_amount=amount)
        seller = get_seller_by_id(payment['seller_id'])
        if seller:
            debt, _, _ = get_seller_debt(payment['seller_id'])
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
