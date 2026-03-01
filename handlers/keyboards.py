#!/usr/bin/env python
# -*- coding: utf-8 -*-

from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton

def get_main_menu():
    """Базовое меню для неактивированных или общих случаев"""
    keyboard = [['Ввести код активации']]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_seller_menu(seller_code: str):
    """
    Меню для активированного продавца. Если код 'Р' – скрываем кнопку «Заявка на поставку».
    """
    if seller_code == 'Р':
        keyboard = [
            ['📤 Отгруженные поставки'],
            ['💰 Реализовано', '📊 Остатки'],
            ['📦 Заявка на пополнение склада'],
            ['📋 Мои заявки']
        ]
    else:
        keyboard = [
            ['📦 Заявка на поставку', '📤 Отгруженные поставки'],
            ['💰 Реализовано', '📊 Остатки'],
            ['📦 Заявка на пополнение склада'],
            ['📋 Мои заявки']
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_menu():
    """Главное меню администратора"""
    keyboard = [
        ['📦 Управление поставками', '💰 Управление платежами'],
        ['📊 Отчеты', '⚙️ Настройки'],
        ['🆘 Пополнение склада']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_back_keyboard():
    """Клавиатура только с кнопкой 'Назад'"""
    keyboard = [['🔙 Назад']]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_back_and_cancel_keyboard():
    """Клавиатура с кнопками 'Назад' и 'Отмена'"""
    keyboard = [['🔙 Назад', '❌ Отмена']]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_confirm_keyboard():
    """Инлайн-клавиатура подтверждения (для мультитоварных заявок)"""
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm")],
        [InlineKeyboardButton("➕ Добавить ещё товар", callback_data="add_more")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_restock_confirm_keyboard():
    """Инлайн-клавиатура для подтверждения заявки на пополнение (без добавления товара)"""
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_to_main_keyboard():
    """Инлайн-кнопка для возврата в главное меню из разделов"""
    keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]
    return InlineKeyboardMarkup(keyboard)
