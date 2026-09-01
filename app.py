import os
import time
import threading
import requests
import pandas as pd
from http.server import HTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------- DUMMY SERVER FOR RENDER ----------------- #
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()
# ----------------------------------------------------------- #

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
MAX_WORKERS = 15

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966"]
# ------------------------------------------------- #

tracker = {}


def get_all_active_usdt_pairs():
    """Fetches all tradeable USDT pairs from CoinDCX, excluding KuCoin (KC-) and INR."""
    url = "https://api.coindcx.com/exchange/v1/markets_details"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        pairs = []
        for item in data:
            if item.get("status") != "active":
                continue
            pair_name = item.get("pair") or item.get("coindcx_name")
            base_curr = item.get("base_currency_short_name", "")

            # Exclude INR and KuCoin
            if base_curr == "INR" or (pair_name and (pair_name.startswith("KC-") or pair_name.endswith("INR"))):
                continue

            if base_curr == "USDT" or (pair_name and "USDT" in pair_name):
                if pair_name and pair_name not in pairs:
                    pairs.append(pair_name)

        print(f"Loaded {len(pairs)} active USDT pairs from CoinDCX.")
        return sorted(pairs)
    except Exception as e:
        print(f"Error fetching market list: {e}")
        return ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates exact Wilder's Smoothed RSI matching TradingView/CoinDCX charts."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's Smoothing requires alpha = 1 / period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def send_telegram_alert(message: str):
    """Dispatches alerts to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to reach Telegram API: {e}")


def format_display_name(pair: str) -> str:
    """Cleans pair name into standard SYMBOL/USDT layout."""
    clean = pair.replace("B-", "").replace("KC-", "").replace("I-", "")
    if clean.endswith("_USDT"):
        return clean.replace("_USDT", "/USDT")
    elif clean.endswith("USDT") and not clean.endswith("/USDT"):
        return clean[:-4] + "/USDT"
    elif "_" in clean:
        return clean.replace("_", "/")
    return clean


def evaluate_pair(pair: str):
    global tracker
    now = time.time()
    
    # Request 300 candles to ensure full Wilder's RSI convergence
    url = f"https://public.coindcx.com/market_data/candles/?pair={pair}&interval={INTERVAL}&limit=300"

    try:
        response = requests.get(url, timeout=8)
        data = response.json()

        if not isinstance(data, list) or len(data) < 30:
            return

        df = pd.DataFrame(data)

        # CRITICAL: Convert 'time' to explicit numeric integer before sorting
        df["time"] = pd.to_numeric(df["time"])
        df["close"] = pd.to_numeric(df["close"])
        df = df.sort_values(by="time", ascending=True).reset_index(drop=True)

        df["rsi"] = calculate_rsi(df["close"], period=RSI_PERIOD)

        # Current live candle
        current_rsi = df["rsi"].iloc[-1]
        live_price = df["close"].iloc[-1]

        if pd.isna(current_rsi):
            return

        display_name = format_display_name(pair)

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
                    f"*Pair:* `{display_name}` (`{pair}`)\n"
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
                    f"*Pair:* `{display_name}` (`{pair}`)\n"
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
                    f"*Pair:* `{display_name}` (`{pair}`)\n"
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
                    f"*Pair:* `{display_name}` (`{pair}`)\n"
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
    print("Starting CoinDCX Clean Live 1h RSI Scanner...")
    all_pairs = get_all_active_usdt_pairs()

    send_telegram_alert(
        f"🤖 *CoinDCX Scanner Online*\n"
        f"Monitoring `{len(all_pairs)}` USDT pairs on 1-hour live candles."
    )

    while True:
        cycle_start = time.time()
        scan_all_markets(all_pairs)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
