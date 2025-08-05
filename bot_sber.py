import sys, os
# Добавляем в путь папку, где лежит этот файл, чтобы найти config.py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import *
import pandas as pd
import ta
import time
import asyncio
import uuid
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from aiogram.types import FSInputFile
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval

# === Настройки торговли из Environment Variables ===
TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
MIN_POSITION_THRESHOLD = 0.5  # Минимум акций, чтобы считать позицию открытой

moscow_tz = pytz.timezone("Europe/Moscow")
current_position = None
entry_price = None

def get_account_balance():
    """Баланс в рублях."""
    with Client(TINKOFF_TOKEN) as client:
        portfolio = client.operations.get_portfolio(account_id=ACCOUNT_ID)
        for pos in portfolio.positions:
            if pos.instrument_type == "currency" and pos.figi == "FG0000000000":
                return float(pos.quantity.units)
    return 0

def get_current_position():
    """Количество акций Сбербанка в портфеле."""
    with Client(TINKOFF_TOKEN) as client:
        portfolio = client.operations.get_portfolio(account_id=ACCOUNT_ID)
        for pos in portfolio.positions:
            if pos.figi == TINKOFF_FIGI:
                return float(pos.quantity.units)
    return 0

def load_initial_prices():
    try:
        with Client(TINKOFF_TOKEN) as client:
            now = datetime.now(pytz.UTC)
            candles = client.market_data.get_candles(
                figi=TINKOFF_FIGI,
                from_=now - timedelta(hours=1),
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN
            )
            return [c.close.units + c.close.nano / 1e9 for c in candles.candles]
    except:
        return []

def get_price():
    try:
        with Client(TINKOFF_TOKEN) as client:
            now = datetime.now(pytz.UTC)
            candles = client.market_data.get_candles(
                figi=TINKOFF_FIGI,
                from_=now - timedelta(minutes=5),
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN
            )
            if not candles.candles:
                return None
            last = candles.candles[-1]
            return last.close.units + last.close.nano / 1e9
    except:
        return None

def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    ema5, ema20, rsi = last["ema_fast"], last["ema_slow"], last["rsi"]

    if pd.notna(ema5) and pd.notna(ema20):
        if ema5 > ema20 and rsi < 70:
            return "BUY", df, "восходящий тренд", ema5, ema20, rsi
        elif ema5 < ema20 and rsi > 30:
            return "SELL", df, "нисходящий тренд", ema5, ema20, rsi
    return "HOLD", df, "нет тренда", ema5, ema20, rsi

def plot_chart(df, signal, price):
    if len(df) < 20:
        return
    os.makedirs("charts_sber", exist_ok=True)
    plt.figure(figsize=(8, 4))
    plt.plot(df["close"], label="Цена", color="black")
    plt.plot(df["ema_fast"], label="EMA(5)", color="blue")
    plt.plot(df["ema_slow"], label="EMA(20)", color="red")
    if signal == "BUY":
        plt.scatter(len(df) - 1, price, color="green")
    elif signal == "SELL":
        plt.scatter(len(df) - 1, price, color="red")
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("charts_sber/chart.png")
    plt.close()

async def send_chart(signal, price, reason, ema5, ema20, rsi):
    bot = Bot(token=TELEGRAM_TOKEN)
    if os.path.exists("charts_sber/chart.png"):
        photo = FSInputFile("charts_sber/chart.png")
        await bot.send_photo(
            CHAT_ID, photo,
            caption=f"[Сбербанк] {signal} @ {price:.2f}\nПричина: {reason}\nEMA5: {ema5:.2f} | EMA20: {ema20:.2f} | RSI: {rsi:.2f}"
        )
    else:
        await bot.send_message(CHAT_ID, f"[Сбербанк] {signal} @ {price:.2f}")
    await bot.session.close()

async def notify_order_rejected(reason):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, f"[Сбербанк] ⚠️ Ордер отклонён!\nПричина: {reason}")
    await bot.session.close()

def place_market_order(direction, current_price):
    current_balance = get_current_position()
    rub_balance = get_account_balance()
    trade_amount_rub = current_price * TRADE_LOTS

    if direction == "BUY":
        if current_balance > MIN_POSITION_THRESHOLD:
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT or trade_amount_rub > rub_balance:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = TRADE_LOTS

    elif direction == "SELL":
        if current_balance <= MIN_POSITION_THRESHOLD:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_SELL
        qty = int(current_balance)
    else:
        return None

    with Client(TINKOFF_TOKEN) as client:
        try:
            resp = client.orders.post_order(
                figi=TINKOFF_FIGI,
                quantity=qty,
                direction=order_dir,
                account_id=ACCOUNT_ID,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4())
            )
            if resp.execution_report_status.name != "EXECUTION_REPORT_STATUS_FILL":
                asyncio.run(notify_order_rejected(str(resp)))
                return None
            return resp
        except Exception as e:
            asyncio.run(notify_order_rejected(str(e)))
            return None

def main():
    global current_position, entry_price
    prices = load_initial_prices()
    first_run = True

    while True:
        price = get_price()
        if price is None:
            time.sleep(60)
            continue

        prices.append(price)
        if len(prices) > 60:
            prices = prices[-60:]

        signal, df, reason, ema5, ema20, rsi = generate_signal(prices)
        plot_chart(df, signal, price)

        if first_run:
            asyncio.run(send_chart(f"🚀 Стартовый сигнал {signal}", price, reason, ema5, ema20, rsi))
            first_run = False

        if signal in ["BUY", "SELL"] and signal != current_position:
            resp = place_market_order(signal, price)
            if resp:
                current_position = signal if signal == "BUY" else None
                entry_price = price
                asyncio.run(send_chart(f"🟢 Открыта {signal}", price, reason, ema5, ema20, rsi))

        time.sleep(60)

if __name__ == "__main__":
    main()
