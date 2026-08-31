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

COOLDOWN_SECONDS = 15 * 60  # 15 minutes cooldown for standard alerts
CYCLE_INTERVAL_SECONDS = 60  # Scan exchange every 1 minute
MAX_WORKERS = 15            # Concurrent worker threads

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966"]  # Add your cousin's ID here in quotes when available
# ------------------------------------------------- #

# State tracker: { pair: {"last_alert_time": float, "last_tier": str} }
tracker = {}


def get_all_usdt_pairs():
    """
    Fetches all tradeable USDT pairs (Binance-backed, native CoinDCX listings, and unprefixed pairs)
    while excluding unsearchable KuCoin (KC-) listings.
    """
    url = "https://api.coindcx.com/exchange/v1/markets_details"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        pairs = []
        for item in data:
            if item.get("status") == "active" and item.get("base_currency_short_name") == "USDT":
                pair_name = item.get("pair") or item.get("coindcx_name")
                
                # Exclude unsearchable KuCoin order books
                if pair_name and pair_name.startswith("KC-"):
                    continue
                
                if pair_name and pair_name not in pairs:
                    pairs.append(pair_name)
                    
        print(f"Loaded {len(pairs)} active USDT pairs from CoinDCX.")
        return sorted(pairs)
    except Exception as e:
        print(f"Error fetching market list: {e}")
        return ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates exact Wilder's Smoothed RSI."""
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


def clean_display_symbol(pair: str) -> str:
    """Formats any token name into a clean SYMBOL/USDT layout."""
    clean = pair.replace("B-", "").replace("I-", "")
    if clean.endswith("_USDT"):
        clean = clean.replace("_USDT", "/USDT")
    elif clean.endswith("USDT") and not clean.endswith("/USDT"):
        clean = clean[:-4] + "/USDT"
    return clean


def evaluate_pair(pair: str):
    global tracker
    now = time.time()
    url = f"https://public.coindcx.com/market_data/candles/?pair={pair}&interval={INTERVAL}&limit=250"

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if not isinstance(data, list) or len(data) < RSI_PERIOD + 2:
            return

        df = pd.DataFrame(data)
        df = df.sort_values(by="time", ascending=True).reset_index(drop=True)
        df["close"] = df["close"].astype(float)
        df["rsi"] = calculate_rsi(df["close"], period=RSI_PERIOD)

        # Live running 1-hour candle
        live_candle = df.iloc[-1]
        current_rsi = live_candle["rsi"]
        live_price = live_candle["close"]

        clean_name = clean_display_symbol(pair)

        if pair not in tracker:
            tracker[pair] = {"last_alert_time": 0, "last_tier": None}

        state = tracker[pair]
        time_since_alert = now - state["last_alert_time"]

        # Reset state when RSI returns to neutral zone
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
        futures = [executor.submit(evaluate_pair, pair) for pair in pairs]
        for future in as_completed(futures):
            pass


if __name__ == "__main__":
    print("Starting CoinDCX Live 1h RSI Scanner (All Active USDT Pairs)...")
    all_pairs = get_all_usdt_pairs()

    send_telegram_alert(
        f"🤖 *CoinDCX Scanner Active*\n"
        f"Monitoring `{len(all_pairs)}` USDT pairs on 1-hour live candles."
    )

    while True:
        cycle_start = time.time()
        scan_all_markets(all_pairs)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
