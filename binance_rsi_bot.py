from ta.momentum import RSIIndicator  # TradingView uyumlu RSI için
import ccxt
import pandas as pd
from telegram import Bot, error as telegram_error
import logging
from statistics import mean
import asyncio
import time
from datetime import datetime
import random
from typing import List, Optional

# === Telegram Ayarları ===
TELEGRAM_TOKEN = '7761091287:AAGEW8OcnfMFUt5_DmAIzBm2I63YgHAcia4'
CHAT_ID = '-1002565394717'

# === Binance API ===
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 30000
})

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('rsi_bot.log')
    ]
)

bot = Bot(token=TELEGRAM_TOKEN)

# === Parametreler ===
RSI_PERIOD = 12
OHLCV_LIMIT = 20
API_DELAY = 0.3
MAX_CONCURRENT = 5
TELEGRAM_TIMEOUT = 30
MAX_RETRIES = 3

# === Stabil Coin Blacklist ===
STABLECOIN_BLACKLIST = [
    'USDC/USDT', 'BUSD/USDT', 'DAI/USDT', 'TUSD/USDT', 'PAX/USDT',
    'UST/USDT', 'EUR/USDT', 'GBP/USDT', 'JPY/USDT', 'AUD/USDT',
    'BTC/USDT', 'ETH/USDT'
]

async def send_telegram_alert(message: str, retry_count: int = 0) -> bool:
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True,
            read_timeout=TELEGRAM_TIMEOUT,
            write_timeout=TELEGRAM_TIMEOUT,
            connect_timeout=TELEGRAM_TIMEOUT,
            pool_timeout=TELEGRAM_TIMEOUT
        )
        logging.info("Telegram mesajı gönderildi.")
        await asyncio.sleep(2)
        return True
    except telegram_error.TimedOut:
        if retry_count < MAX_RETRIES:
            await asyncio.sleep(5)
            return await send_telegram_alert(message, retry_count + 1)
        return False
    except telegram_error.RetryAfter as e:
        await asyncio.sleep(e.retry_after + 2)
        return await send_telegram_alert(message, retry_count)
    except Exception as e:
        logging.error(f"Telegram hatası: {str(e)}")
        return False

async def fetch_ohlcv(symbol: str, timeframe: str, retry_count: int = 0) -> Optional[List[float]]:
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe, limit=OHLCV_LIMIT)
        await asyncio.sleep(API_DELAY)
        return data if data else None
    except ccxt.NetworkError:
        if retry_count < 2:
            await asyncio.sleep(5 * (retry_count + 1))
            return await fetch_ohlcv(symbol, timeframe, retry_count + 1)
        return None
    except Exception:
        return None

def calculate_rsi(prices: List[float]) -> float:
    if len(prices) < RSI_PERIOD:
        return 50.0
    deltas = pd.Series(prices).diff()
    gain = deltas.clip(lower=0)
    loss = -deltas.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean().iloc[-1]
    return 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100

async def get_last_price(symbol: str) -> float:
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker['last'])
    except Exception:
        return 0.0

async def check_symbol(symbol: str) -> bool:
    try:
        data_5m = await fetch_ohlcv(symbol, "5m")
        if not data_5m or len(data_5m) < RSI_PERIOD:
            return False
        rsi_5m = calculate_rsi([x[4] for x in data_5m])
        if rsi_5m < 89:
            return False

        data_15m = await fetch_ohlcv(symbol, "15m")
        if not data_15m or len(data_15m) < RSI_PERIOD:
            return False
        rsi_15m = calculate_rsi([x[4] for x in data_15m])
        if rsi_15m < 89:
            return False

        data_1h = await fetch_ohlcv(symbol, "1h")
        data_4h = await fetch_ohlcv(symbol, "4h")
        if not data_1h or not data_4h:
            return False
        rsi_1h = calculate_rsi([x[4] for x in data_1h])
        rsi_4h = calculate_rsi([x[4] for x in data_4h])

        rsi_avg = mean([rsi_5m, rsi_15m, rsi_1h, rsi_4h])
        if rsi_avg < 85:
            return False

        last_price = await get_last_price(symbol)
        clean_symbol = symbol.replace('/USDT:USDT', '').replace('/USDT', '')

        message = (
    f"💰: {clean_symbol}USDT.P\n"
    f"🔔: High🔴🔴 RSI Alert +85\n"
    f"RSI 5m: {rsi_5m:.2f} | Close: {data_5m[-1][4]:.5f}\n"
    f"RSI 15m: {rsi_15m:.2f} | Close: {data_15m[-1][4]:.5f}\n"
    f"RSI 1h: {rsi_1h:.2f} | Close: {data_1h[-1][4]:.5f}\n"
    f"RSI 4h: {rsi_4h:.2f} | Close: {data_4h[-1][4]:.5f}\n"
    f"Last Price: {last_price:.5f}\n"
    f"ScalpingPA"
)
        await send_telegram_alert(message)
        return True
    except Exception as e:
        logging.error(f"{symbol} kontrolünde hata: {str(e)}")
        return False

async def process_batch(symbols: List[str]) -> int:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def limited_check(symbol: str) -> bool:
        async with semaphore:
            return await check_symbol(symbol)

    results = await asyncio.gather(*[limited_check(s) for s in symbols])
    return sum(results)

async def main_loop():
    logging.info("⚡ Bot başlatıldı")
    while True:
        start_time = time.time()
        try:
            markets = exchange.load_markets()
            symbols = [
                s for s in markets
                if '/USDT' in s
                and markets[s].get('contract')
                and markets[s].get('linear')
                and markets[s].get('active')
                and s not in STABLECOIN_BLACKLIST
            ]

            random.shuffle(symbols)

            logging.info(f"🔍 {len(symbols)} coin taranıyor...")
            alerts = 0
            batch_size = 40
            for i in range(0, len(symbols), batch_size):
                alerts += await process_batch(symbols[i:i + batch_size])
                if i + batch_size < len(symbols):
                    await asyncio.sleep(5)

            elapsed = time.time() - start_time
            logging.info(f"✅ Tarama tamamlandı | {alerts} sinyal | {elapsed:.1f}s")
            await asyncio.sleep(max(120 - elapsed, 60))

        except ccxt.BaseError as e:
            logging.error(f"Binance hatası: {str(e)}")
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Kritik hata: {str(e)}")
            await asyncio.sleep(60)

if __name__ == '__main__':
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logging.info("⛔ Bot durduruldu")
    except Exception as e:
        logging.error(f"Kritik hata: {str(e)}")
