# handlers/packing.py
import logging
from telebot import types
from models import (
    get_seller_by_telegram_id, get_all_products, get_product_variants,
    create_packing_operation, get_hub_stock, get_variant
)
from config import HUB_SELLER_ID

logger = logging.getLogger(__name__)

packing_sessions = {}

def register_packing_handlers(bot):
    @bot.message_handler(func=lambda m: m.text == "📦 Фасовка")
    def handle_packing(message):
        user_id = message.from_user.id
        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.reply_to(message, "❌ У вас нет доступа к этому разделу.")
            return

        packing_sessions[user_id] = {
            'seller_id': seller['id'],
            'items': {},
            'chat_id': message.chat.id
        }
        show_product_list(user_id)

    def show_product_list(user_id):
        session = packing_sessions.get(user_id)
        if not session:
            return
        products = get_all_products()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for p in products:
            markup.add(types.InlineKeyboardButton(p['name'], callback_data=f"pack_prod_{p['id']}"))
        markup.add(types.InlineKeyboardButton("✅ Завершить", callback_data="pack_finish"))
        bot.send_message(
            session['chat_id'],
            "📦 *Фасовка товаров*\n\nВыберите товар:",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pack_prod_'))
    def select_product(call):
        user_id = call.from_user.id
        product_id = int(call.data.split('_')[2])
        session = packing_sessions.get(user_id)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        # Получаем остаток на хабе для этого товара
        hub_kg = get_hub_stock(product_id)
        if hub_kg is None:
            hub_kg = 0

        variants = get_product_variants(product_id)
        pack_variants = [v for v in variants if v['name'] != 'Россыпь']
        if not pack_variants:
            bot.answer_callback_query(call.id, "❌ Нет вариантов для фасовки")
            return

        # Показываем варианты фасовки и сообщаем о доступных кг
        markup = types.InlineKeyboardMarkup(row_width=2)
        products = get_all_products()
        product_name = next((p['name'] for p in products if p['id'] == product_id), "Товар")
        for v in pack_variants:
            btn_text = f"{product_name} {v['name']} ({v['weight_kg']} кг)"
            markup.add(types.InlineKeyboardButton(
                btn_text,
                callback_data=f"pack_var_{product_id}_{v['id']}"
            ))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="pack_back_to_products"))

        bot.edit_message_text(
            f"Вы выбрали *{product_name}*. На хабе доступно *{hub_kg} кг*.\n\nВыберите вариант фасовки:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('pack_var_'))
    def select_variant(call):
        user_id = call.from_user.id
        parts = call.data.split('_')
        product_id = int(parts[2])
        variant_id = int(parts[3])
        session = packing_sessions.get(user_id)
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
        session = packing_sessions.get(user_id)
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
        session = packing_sessions.get(user_id)
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
                lines.append(f"• {variant['product_name']} ({variant['name']}): {item['quantity']} упаковок")
            else:
                lines.append(f"• Товар (вариант {item['variant_id']}): {item['quantity']} упаковок")
        summary = "\n".join(lines)

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить фасовку", callback_data="pack_confirm"),
            types.InlineKeyboardButton("➕ Добавить товар", callback_data="pack_add"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="pack_cancel")
        )
        bot.send_message(
            session['chat_id'],
            f"📦 *Фасовка*\n\n{summary}\n\nПодтвердить?",
            parse_mode='Markdown',
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data == "pack_add")
    def pack_add(call):
        user_id = call.from_user.id
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "pack_back_to_products")
    def pack_back_to_products(call):
        user_id = call.from_user.id
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_product_list(user_id)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "pack_cancel")
    def pack_cancel(call):
        user_id = call.from_user.id
        packing_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Фасовка отменена.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "pack_confirm")
    def pack_confirm(call):
        user_id = call.from_user.id
        session = packing_sessions.pop(user_id, None)
        if not session:
            bot.answer_callback_query(call.id, "❌ Сессия истекла")
            return

        items = list(session['items'].values())
        if not items:
            bot.answer_callback_query(call.id, "❌ Нет позиций для фасовки")
            return

        seller = get_seller_by_telegram_id(user_id)
        if not seller or seller['id'] != HUB_SELLER_ID:
            bot.answer_callback_query(call.id, "❌ Ошибка доступа")
            return

        success_items = []
        failed_items = []
        for item in items:
            try:
                op_id = create_packing_operation(
                    product_id=item['product_id'],
                    variant_id=item['variant_id'],
                    quantity_packs=item['quantity'],
                    created_by=seller['id']
                )
                variant = get_variant(item['variant_id'])
                success_items.append(f"• {variant['product_name']} ({variant['name']}): {item['quantity']} упаковок")
                logger.info(f"✅ Операция фасовки {op_id} создана")
            except ValueError as e:
                variant = get_variant(item['variant_id'])
                failed_items.append(f"• {variant['product_name']} ({variant['name']}): {str(e)}")
                logger.error(f"Ошибка фасовки для variant {item['variant_id']}: {e}")
            except Exception as e:
                variant = get_variant(item['variant_id'])
                failed_items.append(f"• {variant['product_name']} ({variant['name']}): внутренняя ошибка")
                logger.exception(f"Неизвестная ошибка при фасовке variant {item['variant_id']}")

        result_msg = ""
        if success_items:
            result_msg += "✅ *Успешно расфасовано:*\n" + "\n".join(success_items) + "\n\n"
        if failed_items:
            result_msg += "❌ *Не удалось расфасовать:*\n" + "\n".join(failed_items) + "\n\n"
            result_msg += "Оповестите Админа о необходимости инвентаризации."

        if not result_msg:
            result_msg = "❌ Не удалось выполнить фасовку."

        bot.edit_message_text(
            result_msg,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='Markdown'
        )
        bot.answer_callback_query(call.id, "✅ Фасовка завершена")

    @bot.callback_query_handler(func=lambda call: call.data == "pack_finish")
    def pack_finish(call):
        user_id = call.from_user.id
        packing_sessions.pop(user_id, None)
        bot.edit_message_text(
            "❌ Действие завершено.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)
