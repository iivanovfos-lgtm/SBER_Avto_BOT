from config_sber import *
import os
import pandas as pd
import ta
import time
import asyncio
import csv
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from aiogram.types import FSInputFile
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval, StopOrderDirection, StopOrderExpirationType, StopOrderType

moscow_tz = pytz.timezone("Europe/Moscow")
LOT_SIZE = 1  # 1 лот = 10 акций Сбербанка
current_position = None
entry_price = None

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
            last_candle = candles.candles[-1]
            return last_candle.close.units + last_candle.close.nano / 1e9
    except Exception as e:
        print(f"[Ошибка цены] {e}")
        return None

def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    last = df.iloc[-1]
    if pd.notna(last["ema_fast"]) and pd.notna(last["ema_slow"]):
        if last["ema_fast"] > last["ema_slow"] and last["rsi"] < 70:
            return "BUY", df
        elif last["ema_fast"] < last["ema_slow"] and last["rsi"] > 30:
            return "SELL", df
    return "HOLD", df

def place_market_order(direction):
    with Client(TINKOFF_TOKEN) as client:
        dir_enum = OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL
        client.orders.post_order(
            figi=TINKOFF_FIGI,
            quantity=LOT_SIZE,
            direction=dir_enum,
            account_id=ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=f"bot-order-{datetime.now().timestamp()}"
        )

def place_stop_orders(entry_price, direction):
    with Client(TINKOFF_TOKEN) as client:
        if direction == "BUY":
            sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
        else:
            sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        # SL
        client.stop_orders.post_stop_order(
            figi=TINKOFF_FIGI,
            quantity=LOT_SIZE,
            price=sl_price,
            stop_price=sl_price,
            direction=stop_dir,
            account_id=ACCOUNT_ID,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LIMIT
        )

        # TP
        client.stop_orders.post_stop_order(
            figi=TINKOFF_FIGI,
            quantity=LOT_SIZE,
            price=tp_price,
            stop_price=tp_price,
            direction=stop_dir,
            account_id=ACCOUNT_ID,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT
        )

def log_trade(action, price, profit=None):
    with open("trades_sber.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(moscow_tz).strftime("%Y-%m-%d %H:%M:%S"), action, price, profit])

def plot_chart(df, signal, price):
    os.makedirs("charts_sber", exist_ok=True)
    plt.figure(figsize=(8, 4))
    plt.plot(df["close"], label="Цена", color="black")
    plt.plot(df["ema_fast"], label="EMA(5)", color="blue")
    plt.plot(df["ema_slow"], label="EMA(20)", color="red")
    if signal == "BUY":
        plt.scatter(len(df) - 1, price, color="green", label="BUY")
    elif signal == "SELL":
        plt.scatter(len(df) - 1, price, color="red", label="SELL")
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("charts_sber/chart.png")
    plt.close()

async def send_chart(signal, price):
    bot = Bot(token=TELEGRAM_TOKEN)
    photo = FSInputFile("charts_sber/chart.png")
    await bot.send_photo(CHAT_ID, photo, caption=f"{signal} @ {price:.2f}")
    await bot.session.close()

async def send_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, text)
    await bot.session.close()

def main():
    global current_position, entry_price
    prices = []
    first_run = True

    while True:
        price = get_price()
        if price is None:
            time.sleep(60)
            continue

        prices.append(price)
        if len(prices) > 60:
            prices = prices[-60:]

        signal, df = generate_signal(prices)
        plot_chart(df, signal, price)

        if first_run:
            asyncio.run(send_chart(f"🚀 Стартовый сигнал {signal}", price))
            first_run = False

        if signal in ["BUY", "SELL"] and signal != current_position:
            current_position = signal
            entry_price = price
            place_market_order(signal)
            log_trade(f"OPEN {signal}", price)
            asyncio.run(send_message(f"🟢 Открыта {signal} @ {price:.2f}"))
            place_stop_orders(entry_price, signal)
            asyncio.run(send_message(f"📌 SL: {entry_price * (1 - STOP_LOSS_PCT / 100 if signal == 'BUY' else 1 + STOP_LOSS_PCT / 100):.2f}\n📌 TP: {entry_price * (1 + TAKE_PROFIT_PCT / 100 if signal == 'BUY' else 1 - TAKE_PROFIT_PCT / 100):.2f}"))

        time.sleep(60)

if __name__ == "__main__":
    main()
