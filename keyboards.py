# keyboards.py
from telebot import types

def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("📋 Ожидают обработки"))
    keyboard.add(types.KeyboardButton("📦 Мои остатки"), types.KeyboardButton("🔄 Заявка на перемещение"))
    keyboard.add(types.KeyboardButton("💰 Выплата админу"))
    keyboard.add(types.KeyboardButton("➕ Зафиксировать продажу"))
    keyboard.add(types.KeyboardButton("📦 Фасовка"))  # только для кладовщика
    keyboard.add(types.KeyboardButton("👑 Админ панель"))
    return keyboard

def admin_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("⏳ Ожидают обработки"))
    keyboard.add(types.KeyboardButton("📦 Остатки"), types.KeyboardButton("💰 Выплаты"))
    keyboard.add(types.KeyboardButton("📦 Закуп товаров"))
    keyboard.add(types.KeyboardButton("🔙 Назад в общее меню"))
    return keyboard
