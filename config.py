import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
PORT = int(os.getenv('PORT', 10000))
HUB_SELLER_ID = 5  # ID продавца-кладовщика (хаб)

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не заданы обязательные переменные окружения")

BASE_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://skladbot-rhoo.onrender.com')
WEBHOOK_URL = f"{BASE_URL}/webhook"
