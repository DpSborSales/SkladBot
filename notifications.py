from telebot import types

def send_negative_stock_warning(bot, chat_id, seller_id):
    from models import get_negative_stock_summary
    negatives = get_negative_stock_summary(seller_id)
    if not negatives:
        return
    lines = [f"• {row['product_name']} ({row['variant_name']}): {abs(row['quantity'])} упаковок" for row in negatives]
    summary = "\n".join(lines)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📦 Создать заявку на перемещение", callback_data="create_transfer_request"))
    bot.send_message(
        chat_id,
        f"⚠️ *Внимание!* Вы продали больше товара, чем было у вас на складе.\n"
        f"Необходимо произвести инвентаризацию и сделать заявку на перемещение.\n"
        f"Сейчас ваши остатки ушли в минус:\n{summary}",
        parse_mode='Markdown',
        reply_markup=markup
    )
