import os
import time
import threading
import requests
import pandas as pd
from http.server import HTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------- DUMMY SERVER FOR RENDER WEB SERVICE ----------------- #
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    print(f"Dummy health check server running on port {port}")
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()
# ---------------------------------------------------------------------- #

# ----------------- CONFIGURATION ----------------- #
INTERVAL = "1h"
RSI_PERIOD = 14

# Alert Thresholds
RSI_STANDARD_OB = 80.0
RSI_EXTREME_OB = 85.0

RSI_STANDARD_OS = 20.0
RSI_EXTREME_OS = 15.0

COOLDOWN_SECONDS = 15 * 60  # 15 minutes cooldown
CYCLE_INTERVAL_SECONDS = 60  # Scan every 1 minute
MAX_WORKERS = 20            # Parallel scanner threads

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966"]  # Add your cousin's ID here when available
# ------------------------------------------------- #

tracker = {}


def get_all_binance_usdt_pairs():
    """Fetches all actively trading USDT spot pairs directly from Binance API."""
    url = "https://api.binance.com/api/v3/exchangeInfo"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        pairs = []
        for symbol_info in data.get("symbols", []):
            if (
                symbol_info.get("status") == "TRADING"
                and symbol_info.get("quoteAsset") == "USDT"
                and symbol_info.get("isSpotTradingAllowed", True)
            ):
                pairs.append(symbol_info.get("symbol"))
                
        print(f"Loaded {len(pairs)} active USDT pairs directly from Binance.")
        return sorted(pairs)
    except Exception as e:
        print(f"Error fetching Binance market list: {e}")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates exact Wilder's Smoothed RSI (identical to TradingView/Binance)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def send_telegram_alert(message: str):
    """Sends a markdown-formatted message to all configured chat IDs."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to reach Telegram API for {chat_id}: {e}")


def evaluate_pair(symbol: str):
    global tracker
    now = time.time()

    # Binance standard 1h klines endpoint (limit=100 is plenty for RSI 14)
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit=100"

    try:
        response = requests.get(url, timeout=8)
        data = response.json()

        if not isinstance(data, list) or len(data) < RSI_PERIOD + 2:
            return

        # Binance kline index 4 is the Close price
        closes = [float(kline[4]) for kline in data]
        df = pd.DataFrame({"close": closes})
        df["rsi"] = calculate_rsi(df["close"], period=RSI_PERIOD)

        # Index -1 represents the actively RUNNING 1-hour candle
        current_rsi = df["rsi"].iloc[-1]
        live_price = df["close"].iloc[-1]

        # Clean display format (e.g. BTCUSDT -> BTC/USDT)
        clean_name = f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol

        if symbol not in tracker:
            tracker[symbol] = {"last_alert_time": 0, "last_tier": None}

        state = tracker[symbol]
        time_since_alert = now - state["last_alert_time"]

        # Reset alert tier when RSI normalizes between 20 and 80
        if RSI_STANDARD_OS < current_rsi < RSI_STANDARD_OB:
            state["last_tier"] = None
            return

        # ----------------- OVERBOUGHT LOGIC ----------------- #
        if current_rsi >= RSI_EXTREME_OB:
            if state["last_tier"] != "EXTREME_OB" or time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🔥 *CRITICAL OVERBOUGHT ALERT*\n\n"
                    f"*Pair:* `{clean_name}`\n"
                    f"*Timeframe:* 1 Hour (Live Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_EXTREME_OB})\n"
                    f"*Live Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "EXTREME_OB"

        elif current_rsi >= RSI_STANDARD_OB:
            if state["last_tier"] is None and time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🚨 *RSI OVERBOUGHT ALERT*\n\n"
                    f"*Pair:* `{clean_name}`\n"
                    f"*Timeframe:* 1 Hour (Live Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_STANDARD_OB})\n"
                    f"*Live Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "STANDARD_OB"

        # ----------------- OVERSOLD LOGIC ----------------- #
        elif current_rsi <= RSI_EXTREME_OS:
            if state["last_tier"] != "EXTREME_OS" or time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"❄️ *CRITICAL OVERSOLD ALERT*\n\n"
                    f"*Pair:* `{clean_name}`\n"
                    f"*Timeframe:* 1 Hour (Live Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_EXTREME_OS})\n"
                    f"*Live Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "EXTREME_OS"

        elif current_rsi <= RSI_STANDARD_OS:
            if state["last_tier"] is None and time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🟢 *RSI OVERSOLD ALERT*\n\n"
                    f"*Pair:* `{clean_name}`\n"
                    f"*Timeframe:* 1 Hour (Live Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_STANDARD_OS})\n"
                    f"*Live Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "STANDARD_OS"

    except Exception:
        pass


def scan_all_markets(pairs):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(evaluate_pair, symbol) for symbol in pairs]
        for future in as_completed(futures):
            pass


if __name__ == "__main__":
    print("Starting Live 1h RSI Scanner via Binance Engine...")
    all_pairs = get_all_binance_usdt_pairs()

    send_telegram_alert(
        f"🤖 *Crypto RSI Scanner Online*\n"
        f"Monitoring `{len(all_pairs)}` USDT pairs on 1-hour live candles."
    )

    while True:
        cycle_start = time.time()
        scan_all_markets(all_pairs)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
