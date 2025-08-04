import os

# --- Tinkoff API ---
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "t.ТВОЙ_БОЕВОЙ_ТОКЕН")
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "2183827266")
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI", "BBG004730N88")  # FIGI Сбербанк

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "твой_токен_бота")
CHAT_ID = os.getenv("CHAT_ID", "твой_chat_id")

# --- Торговые параметры ---
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.5))     # 0.5% стоп-лосс
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 1.0)) # 1% тейк-профит
