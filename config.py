import os

# Токен бери у @BotFather и клади в переменную окружения BOT_TOKEN
# Значение хранится только в переменной окружения хостинга.
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# Telegram user_id администраторов бота (могут запускать/завершать ивенты с боссами)
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7694677506").split(",") if x.strip().isdigit()]

# Redis для FSM-состояний aiogram. Если REDIS_URL не задан или Redis недоступен,
# бот автоматически откатится на MemoryStorage, чтобы не падать на старте.
# Примеры:
#   redis://localhost:6379/0
#   redis://:password@host:6379/0
REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_STATE_TTL = int(os.getenv("REDIS_STATE_TTL", "2592000"))  # 30 дней
REDIS_DATA_TTL = int(os.getenv("REDIS_DATA_TTL", "2592000"))    # 30 дней

# PostgreSQL connection string задаётся в DATABASE_URL и читается database.py.
