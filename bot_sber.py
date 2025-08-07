import asyncio
from datetime import datetime, timedelta
from tinkoff.invest import AsyncClient, CandleInterval, OrderDirection, OrderType
from tinkoff.invest.utils import now
from tinkoff.invest.services import InstrumentsService
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
import pandas as pd
import os

# ENV variables
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TINKOFF_ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")
TINKOFF_FIGI = os.getenv("TINKOFF_SBER_FIGI")
MAX_LOTS = int(os.getenv("MAX_LOTS", 3))
MAX_RUB = float(os.getenv("MAX_RUB", 30000))
COMMISSION_RATE = 0.0005  # 0.05%

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

state = {
    "position": None,
    "entry_price": None,
    "tp": None,
    "sl": None
}

async def fetch_candles():
    async with AsyncClient(TINKOFF_TOKEN) as client:
        candles = await client.market_data.get_candles(
            figi=TINKOFF_FIGI,
            from_=now() - timedelta(hours=1),
            to=now(),
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN
        )
        data = [
            {
                "time": c.time,
                "open": float(c.open.units) + c.open.nano / 1e9,
                "close": float(c.close.units) + c.close.nano / 1e9,
                "high": float(c.high.units) + c.high.nano / 1e9,
                "low": float(c.low.units) + c.low.nano / 1e9,
                "volume": c.volume
            }
            for c in candles.candles
        ]
        return pd.DataFrame(data)

async def generate_signal(df):
    df['ema5'] = EMAIndicator(close=df['close'], window=5).ema_indicator()
    df['ema20'] = EMAIndicator(close=df['close'], window=20).ema_indicator()
    df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()

    ema5 = df['ema5'].iloc[-1]
    ema20 = df['ema20'].iloc[-1]
    rsi = df['rsi'].iloc[-1]
    price = df['close'].iloc[-1]

    if ema5 > ema20 and rsi > 55:
        return "BUY", price, ema5, ema20, rsi
    elif ema5 < ema20 and rsi < 45:
        return "SELL", price, ema5, ema20, rsi
    else:
        return "HOLD", price, ema5, ema20, rsi

async def place_order(direction, price):
    async with AsyncClient(TINKOFF_TOKEN) as client:
        instruments = await client.instruments.get_instrument_by_figi(figi=TINKOFF_FIGI)
        lot = instruments.instrument.lot

        money_per_lot = price * lot
        max_lots_by_money = int(MAX_RUB // money_per_lot)
        lots = min(MAX_LOTS, max_lots_by_money)

        if lots < 1:
            await bot.send_message(CHAT_ID, f"⚠️ Недостаточно средств для покупки хотя бы одного лота по {price:.2f}")
            return

        direction_enum = OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL

        resp = await client.orders.post_order(
            figi=TINKOFF_FIGI,
            quantity=lots,
            direction=direction_enum,
            account_id=TINKOFF_ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=f"sberbot-{datetime.now().timestamp()}"
        )

        # Рассчёт TP и SL с учетом комиссии
        entry_price = price
        tp = entry_price * (1 + 0.01 + COMMISSION_RATE * 2)
        sl = entry_price * (1 - 0.005 - COMMISSION_RATE * 2)

        state["position"] = direction
        state["entry_price"] = entry_price
        state["tp"] = tp
        state["sl"] = sl

        await bot.send_message(CHAT_ID, f"[Сбербанк] 🟢 Открыта {direction} @ {entry_price:.2f}\nTP: {tp:.2f} | SL: {sl:.2f}\nEMA5: {state['ema5']:.2f} | EMA20: {state['ema20']:.2f} | RSI: {state['rsi']:.2f}")

async def monitor():
    df = await fetch_candles()
    signal, price, ema5, ema20, rsi = await generate_signal(df)

    state['ema5'] = ema5
    state['ema20'] = ema20
    state['rsi'] = rsi

    if state["position"] is None:
        await bot.send_message(CHAT_ID, f"[Сбербанк] 🚀 Стартовый сигнал {signal} @ {price:.2f}")
        if signal in ["BUY", "SELL"]:
            await place_order(signal, price)
    else:
        # сопровождение позиции
        direction = state["position"]
        entry = state["entry_price"]
        tp = state["tp"]
        sl = state["sl"]

        if (direction == "BUY" and price >= tp) or (direction == "SELL" and price <= tp):
            state["position"] = None
            await bot.send_message(CHAT_ID, f"[Сбербанк] 🎯 Take Profit достигнут @ {price:.2f}")
        elif (direction == "BUY" and price <= sl) or (direction == "SELL" and price >= sl):
            state["position"] = None
            await bot.send_message(CHAT_ID, f"[Сбербанк] 🛑 Stop Loss достигнут @ {price:.2f}")
        else:
            await bot.send_message(CHAT_ID, f"[Сбербанк] 📈 {direction} @ {entry:.2f} | Цена: {price:.2f}\nTP: {tp:.2f} | SL: {sl:.2f}")

async def main():
    while True:
        try:
            await monitor()
        except Exception as e:
            await bot.send_message(CHAT_ID, f"❌ Ошибка: {e}")
        await asyncio.sleep(300)  # 5 минут

if __name__ == "__main__":
    asyncio.run(main())
