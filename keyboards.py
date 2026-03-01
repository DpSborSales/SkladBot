from telebot import types

def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Ожидают обработки"))
    keyboard.add(types.KeyboardButton("📦 Мои остатки"), types.KeyboardButton("🔄 Заявка на перемещение"))
    keyboard.add(types.KeyboardButton("💰 Выплата админу"))
    return keyboard
