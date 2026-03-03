# handlers/packing.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_product_variants,
    create_packing_operation, get_hub_stock
)
from config import HUB_SELLER_ID

logger = logging.getLogger(__name__)

packing_sessions = {}

def register_packing_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "📦 Фасовка" and m.from_user.id)
    def handle_packing(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.reply_to(message, "❌ У вас нет доступа к этому разделу.")
            return

        products = get_all_products()
        if not products:
            bot.reply_to(message, "❌ Нет товаров в каталоге.")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"pack_prod_{p['id']}"))
        bot.send_message(
            message.chat.id,
            "📦 *Фасовка товаров*\n\nВыберите товар:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pack_prod_'))
    def select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        variants = get_product_variants(product_id)
        if not variants:
            bot.answer_callback_query(call.id, "❌ У товара нет вариантов фасовки")
            return

        # Отфильтровываем вариант "Россыпь" (если есть) — его нельзя фасовать
        pack_variants = [v for v in variants if v['name'] != 'Россыпь']
        if not pack_variants:
            bot.answer_callback_query(call.id, "❌ Нет вариантов для фасовки")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        for v in pack_variants:
            markup.add(types.InlineKeyboardButton(
                f"{v['name']} ({v['weight_kg']} кг)",
                callback_data=f"pack_var_{v['id']}"
            ))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="pack_back_to_products"))

        bot.edit_message_text(
            f"Выберите фасовку:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pack_var_'))
    def select_variant(call):
        user_id = call.from_user.id
        variant_id = int(call.data.split('_')[2])
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        packing_sessions[user_id] = {'variant_id': variant_id}
        bot.edit_message_text(
            f"Введите количество упаковок:",
            call.message.chat.id,
            call.message.message_id
        )
        bot.register_next_step_handler_by_chat_id(call.message.chat.id, process_pack_quantity, user_id)
        bot.answer_callback_query(call.id)

    def process_pack_quantity(message, user_id):
        session = packing_sessions.pop(user_id, None)
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

        variant_id = session['variant_id']
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.reply_to(message, "❌ Ошибка доступа")
            return

        try:
            op_id = create_packing_operation(
                product_id=None,  # можно не передавать, определим по variant
                variant_id=variant_id,
                quantity_packs=qty,
                created_by=seller['id']
            )
            bot.reply_to(
                message,
                f"✅ Операция фасовки №{op_id} выполнена!\n"
                f"Создано {qty} упаковок."
            )
        except ValueError as e:
            bot.reply_to(message, f"❌ {e}")
        except Exception as e:
            logger.exception("Ошибка при фасовке")
            bot.reply_to(message, "❌ Произошла внутренняя ошибка.")

    @bot.callback_query_handler(func=lambda call: call.data == "pack_back_to_products")
    def back_to_products(call):
        user_id = call.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"pack_prod_{p['id']}"))
        bot.edit_message_text(
            "📦 *Фасовка товаров*\n\nВыберите товар:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
