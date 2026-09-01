import os
import time
import threading
import requests
import pandas as pd
from http.server import HTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------- 24/7 KEEP-ALIVE SERVER (RENDER COMPATIBLE) ----------------- #
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()
# ----------------------------------------------------------------------------- #

# ----------------- SCANNER CONFIGURATION ----------------- #
INTERVAL = "1h"
RSI_PERIOD = 14
MIN_CANDLES_REQUIRED = 50      # Minimum historical bars required for calculation

# Alert Thresholds
RSI_STANDARD_OB = 90.0
RSI_EXTREME_OB = 95.0

RSI_STANDARD_OS = 15.0
RSI_EXTREME_OS = 10.0

COOLDOWN_SECONDS = 15 * 60     # 15-minute cooldown for repeated alerts
CYCLE_INTERVAL_SECONDS = 60    # 1-minute full sweep interval
MAX_WORKERS = 15               # Concurrency pool size
# --------------------------------------------------------- #

# Telegram Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966"]

# State tracker: { pair: {"last_alert_time": float, "last_tier": str} }
tracker = {}


def get_all_usdt_markets():
    """
    Fetches all active USDT pairs across CoinDCX directly.
    Excludes domestic INR pairs to avoid duplicate alerts and low-volume local books.
    """
    url = "https://api.coindcx.com/exchange/v1/markets_details"
    try:
        response = requests.get(url, timeout=12)
        response.raise_for_status()
        data = response.json()

        valid_pairs = []
        for item in data:
            if item.get("status") != "active":
                continue

            pair_name = item.get("pair") or item.get("coindcx_name")
            base_currency = item.get("base_currency_short_name", "")

            # Exclude INR pairs
            if base_currency == "INR" or (pair_name and (pair_name.endswith("INR") or pair_name.endswith("_INR"))):
                continue

            # Capture all active USDT markets
            if base_currency == "USDT" or (pair_name and "USDT" in pair_name):
                if pair_name and pair_name not in valid_pairs:
                    valid_pairs.append(pair_name)

        print(f"Loaded {len(valid_pairs)} active USDT pairs across CoinDCX.")
        return sorted(valid_pairs)
    except Exception as e:
        print(f"Error loading CoinDCX market catalog: {e}")
        return ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]


def calculate_wilders_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Vectorized Wilder's Exponentially Smoothed RSI (matching TradingView and CoinDCX).
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's Smoothing formula uses alpha = 1 / period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def send_telegram_alert(message: str):
    """Sends markdown-formatted alert notifications to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
        try:
            requests.post(url, json=payload, timeout=8)
        except Exception as e:
            print(f"Telegram dispatch failed for {chat_id}: {e}")


def clean_symbol_display(pair: str) -> str:
    """Formats internal exchange symbols into clean TOKEN/USDT layouts."""
    clean = pair.replace("B-", "").replace("KC-", "").replace("I-", "")
    if clean.endswith("_USDT"):
        return clean.replace("_USDT", "/USDT")
    elif clean.endswith("USDT") and not clean.endswith("/USDT"):
        return clean[:-4] + "/USDT"
    elif "_" in clean:
        return clean.replace("_", "/")
    return clean


def process_market_candle(pair: str):
    global tracker
    now = time.time()

    url = f"https://public.coindcx.com/market_data/candles/?pair={pair}&interval={INTERVAL}&limit=250"

    try:
        response = requests.get(url, timeout=8)
        data = response.json()

        if not isinstance(data, list) or len(data) < MIN_CANDLES_REQUIRED:
            return

        df = pd.DataFrame(data)

        # 1. Enforce strict numeric data typing (handles Unix millisecond timestamps)
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["time", "close"])

        # 2. Monotonic Array Normalization (Prune duplicates & sort chronologically)
        df = df.drop_duplicates(subset=["time"]).sort_values(by="time", ascending=True).reset_index(drop=True)

        if len(df) < MIN_CANDLES_REQUIRED:
            return

        # 3. Calculate exact Wilder's RSI
        df["rsi"] = calculate_wilders_rsi(df["close"], period=RSI_PERIOD)

        # Extract current running 1-hour live candle
        live_candle = df.iloc[-1]
        current_rsi = live_candle["rsi"]
        live_price = live_candle["close"]

        if pd.isna(current_rsi):
            return

        display_name = clean_symbol_display(pair)

        # Initialize tracker state for immediate evaluation upon boot
        if pair not in tracker:
            tracker[pair] = {"last_alert_time": 0, "last_tier": None}

        state = tracker[pair]
        time_since_alert = now - state["last_alert_time"]

        # Reset state when RSI returns to neutral levels
        if RSI_STANDARD_OS < current_rsi < RSI_STANDARD_OB:
            state["last_tier"] = None
            return

        # ----------------- OVERBOUGHT SIGNALS (>= 90.0) ----------------- #
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

        # ----------------- OVERSOLD SIGNALS (<= 15.0) ----------------- #
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


def execute_market_sweep(pairs):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_market_candle, pair) for pair in pairs]
        for future in as_completed(futures):
            pass


if __name__ == "__main__":
    print("Starting CoinDCX Pro Scanner (RSI <= 15 / >= 90)...")
    all_pairs = get_all_usdt_markets()

    send_telegram_alert(
        f"🤖 *CoinDCX Scanner Active*\n"
        f"Monitoring `{len(all_pairs)}` USDT pairs.\n"
        f"*Thresholds:* RSI <= {RSI_STANDARD_OS} (Oversold) | RSI >= {RSI_STANDARD_OB} (Overbought)."
    )

    while True:
        cycle_start = time.time()
        execute_market_sweep(all_pairs)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
