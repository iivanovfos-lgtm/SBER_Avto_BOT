import os
import pandas as pd
import ta
import time
import asyncio
import uuid
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval

# === Читаем переменные из окружения Render ===
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI", "BBG004730N88")  # FIGI Сбербанк по умолчанию

TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))  # Лоты на сделку
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
LOT_SIZE_SBER = 10  # 1 лот = 10 акций
TP_PERCENT = float(os.getenv("TP_PERCENT", 0.5))  # Take Profit %
SL_PERCENT = float(os.getenv("SL_PERCENT", 0.3))  # Stop Loss %
BROKER_FEE = float(os.getenv("BROKER_FEE", 0.003))  # 0.3% комиссия

moscow_tz = pytz.timezone("Europe/Moscow")

current_position = None
entry_price = None
take_profit_price = None
stop_loss_price = None

# ===== Получаем баланс =====
def get_balances():
    rub_balance = 0
    sber_balance = 0
    with Client(TINKOFF_TOKEN) as client:
        positions = client.operations.get_positions(account_id=ACCOUNT_ID)
        for cur in positions.money:
            if cur.currency == "rub":
                rub_balance = float(cur.units)
        for pos in positions.securities:
            if pos.figi == TINKOFF_FIGI:
                sber_balance = float(pos.balance)
    return rub_balance, sber_balance

# ===== Telegram =====
async def send_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, text)
    await bot.session.close()

async def notify_order_rejected(reason):
    await send_message(f"[Сбербанк] ⚠️ Ордер отклонён!\nПричина: {reason}")

# ===== Цены =====
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

# ===== Сигналы =====
def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    ema5, ema20, rsi = last["ema_fast"], last["ema_slow"], last["rsi"]

    if pd.notna(ema5) and pd.notna(ema20):
        if ema5 > ema20 and rsi < 70:
            return "BUY", "восходящий тренд", ema5, ema20, rsi
        elif ema5 < ema20 and rsi > 30:
            return "SELL", "нисходящий тренд", ema5, ema20, rsi
    return "HOLD", "нет тренда", ema5, ema20, rsi

# ===== Ордера =====
def place_market_order(direction, current_price):
    rub_balance, sber_balance = get_balances()

    sber_lots = int(sber_balance // LOT_SIZE_SBER)
    buy_qty = TRADE_LOTS * LOT_SIZE_SBER
    trade_amount_rub = current_price * buy_qty

    if direction == "BUY":
        if sber_balance >= LOT_SIZE_SBER:
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT or trade_amount_rub > rub_balance:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = buy_qty

    elif direction == "SELL":
        if sber_balance < LOT_SIZE_SBER:
            return None
        qty = min(sber_lots, TRADE_LOTS) * LOT_SIZE_SBER
        if qty < LOT_SIZE_SBER:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_SELL
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

# ===== Основной цикл =====
def main():
    global current_position, entry_price, take_profit_price, stop_loss_price
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

        signal, reason, ema5, ema20, rsi = generate_signal(prices)

        # === Проверка TP/SL ===
        if current_position == "BUY":
            if price >= take_profit_price:
                asyncio.run(send_message(f"[Сбербанк] 🎯 Take Profit достигнут @ {price:.2f}"))
                place_market_order("SELL", price)
                current_position = None
                continue
            elif price <= stop_loss_price:
                asyncio.run(send_message(f"[Сбербанк] 🛑 Stop Loss достигнут @ {price:.2f}"))
                place_market_order("SELL", price)
                current_position = None
                continue

        # === Новый вход ===
        if first_run:
            asyncio.run(send_message(f"🚀 Стартовый сигнал {signal} @ {price:.2f}"))
            first_run = False

        if signal == "BUY" and current_position != "BUY":
            resp = place_market_order("BUY", price)
            if resp:
                current_position = "BUY"
                entry_price = price

                # Цена входа с учётом комиссии (покупка + продажа)
                total_fee = BROKER_FEE * 2
                entry_price_with_fee = entry_price * (1 + total_fee)

                take_profit_price = entry_price_with_fee * (1 + TP_PERCENT / 100)
                stop_loss_price = entry_price_with_fee * (1 - SL_PERCENT / 100)

                asyncio.run(send_message(
                    f"[Сбербанк] 🟢 Открыта BUY @ {price:.2f}\n"
                    f"TP: {take_profit_price:.2f} | SL: {stop_loss_price:.2f} "
                    f"(учтена комиссия {BROKER_FEE*100:.2f}% с каждой сделки)"
                ))

        elif signal == "SELL" and current_position == "BUY":
            asyncio.run(send_message(f"[Сбербанк] 📉 Тренд развернулся — SELL @ {price:.2f}"))
            place_market_order("SELL", price)
            current_position = None

        time.sleep(60)

if __name__ == "__main__":
    main()
