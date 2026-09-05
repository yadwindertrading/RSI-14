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
CANDLE_LIMIT = 250             # 250 bars ensures >99.999% Wilder's convergence
MIN_CANDLES_REQUIRED = 50      # Bars required to compute stable RSI

# Alert Thresholds
RSI_STANDARD_OB = 90.0
RSI_EXTREME_OB = 95.0

RSI_STANDARD_OS = 15.0
RSI_EXTREME_OS = 10.0

COOLDOWN_SECONDS = 15 * 60     # 15-minute cooldown for repeated alerts
CYCLE_INTERVAL_SECONDS = 60    # 1-minute full sweep interval
MAX_WORKERS = 15               # Concurrency pool size
# --------------------------------------------------------- #

# Telegram Credentials (Configured for both recipients)
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966", "630462102"]

# State tracker: { symbol: {"last_alert_time": float, "last_tier": str} }
tracker = {}


def get_active_futures_pairs():
    """
    1. Fetches active CoinDCX futures contracts directly from their active_instruments endpoint.
    2. Converts them to standard perpetual symbols (e.g. 'B-BTC_USDT' -> 'BTCUSDT', 'B-B_USDT' -> 'BUSDT').
    3. Guarantees 0 spot-only assets are included.
    """
    futures_endpoint = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
    
    try:
        resp = requests.get(futures_endpoint, timeout=12)
        resp.raise_for_status()
        raw_list = resp.json()

        instruments = raw_list if isinstance(raw_list, list) else raw_list.get("data", [])
        
        futures_symbols = []
        for item in instruments:
            # Handle plain string array directly: ["B-BTC_USDT", "B-B_USDT", ...]
            if isinstance(item, str):
                if "USDT" in item:
                    # Strip 'B-', 'KC-', underscores to get exact perpetual ticker (e.g., 'BUSDT', 'BTCUSDT')
                    clean = item.split("-", 1)[-1].replace("_", "").upper()
                    futures_symbols.append(clean)
            elif isinstance(item, dict):
                pair = item.get("pair") or item.get("symbol", "")
                if "USDT" in pair:
                    clean = pair.split("-", 1)[-1].replace("_", "").upper()
                    futures_symbols.append(clean)

        unique_symbols = sorted(list(set(futures_symbols)))
        print(f"Loaded {len(unique_symbols)} active CoinDCX Futures perpetual contracts.")
        return unique_symbols

    except Exception as e:
        print(f"Error querying CoinDCX futures directory: {e}. Using core liquid contracts.")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


def calculate_wilders_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Vectorized Wilder's Exponentially Smoothed RSI (matching TradingView and exchange engines).
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's Smoothing formula uses alpha = 1 / period (RMA)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def send_telegram_alert(message: str):
    """Dispatches markdown-formatted alert notifications to all registered Telegram chats."""
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


def format_display_symbol(symbol: str) -> str:
    """Formats raw tickers into readable format (e.g. 'BUSDT' -> 'B/USDT', 'BTCUSDT' -> 'BTC/USDT')."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def process_futures_candle(symbol: str):
    global tracker
    now = time.time()

    # Pulls directly from the Futures Perpetual klines feed
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={CANDLE_LIMIT}"

    try:
        response = requests.get(url, timeout=8)
        data = response.json()

        # Discard invalid responses or tokens without minimum history
        if not isinstance(data, list) or len(data) < MIN_CANDLES_REQUIRED:
            return

        # Binance Futures Kline format: [0: open_time, 1: open, 2: high, 3: low, 4: close, ...]
        df = pd.DataFrame(data)
        time_series = pd.to_numeric(df[0], errors="coerce")
        close_series = pd.to_numeric(df[4], errors="coerce")

        clean_df = pd.DataFrame({"time": time_series, "close": close_series}).dropna()
        clean_df = clean_df.drop_duplicates(subset=["time"]).sort_values(by="time", ascending=True).reset_index(drop=True)

        if len(clean_df) < MIN_CANDLES_REQUIRED:
            return

        clean_df["rsi"] = calculate_wilders_rsi(clean_df["close"], period=RSI_PERIOD)

        # Extract current live 1-hour candle
        live_candle = clean_df.iloc[-1]
        current_rsi = live_candle["rsi"]
        live_price = live_candle["close"]

        if pd.isna(current_rsi):
            return

        display_name = format_display_symbol(symbol)

        if symbol not in tracker:
            tracker[symbol] = {"last_alert_time": 0, "last_tier": None}

        state = tracker[symbol]
        time_since_alert = now - state["last_alert_time"]

        # Reset state when RSI returns to neutral territory
        if RSI_STANDARD_OS < current_rsi < RSI_STANDARD_OB:
            state["last_tier"] = None
            return

        # ----------------- OVERBOUGHT SIGNALS (>= 90.0) ----------------- #
        if current_rsi >= RSI_EXTREME_OB:
            if state["last_tier"] != "EXTREME_OB" or time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🔥 *FUTURES CRITICAL OVERBOUGHT*\n\n"
                    f"*Pair:* `{display_name}` (`{symbol}`)\n"
                    f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_EXTREME_OB})\n"
                    f"*Live Futures Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "EXTREME_OB"

        elif current_rsi >= RSI_STANDARD_OB:
            if state["last_tier"] is None and time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🚨 *FUTURES RSI OVERBOUGHT*\n\n"
                    f"*Pair:* `{display_name}` (`{symbol}`)\n"
                    f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_STANDARD_OB})\n"
                    f"*Live Futures Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "STANDARD_OB"

        # ----------------- OVERSOLD SIGNALS (<= 15.0) ----------------- #
        elif current_rsi <= RSI_EXTREME_OS:
            if state["last_tier"] != "EXTREME_OS" or time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"❄️ *FUTURES CRITICAL OVERSOLD*\n\n"
                    f"*Pair:* `{display_name}` (`{symbol}`)\n"
                    f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_EXTREME_OS})\n"
                    f"*Live Futures Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "EXTREME_OS"

        elif current_rsi <= RSI_STANDARD_OS:
            if state["last_tier"] is None and time_since_alert >= COOLDOWN_SECONDS:
                msg = (
                    f"🟢 *FUTURES RSI OVERSOLD*\n\n"
                    f"*Pair:* `{display_name}` (`{symbol}`)\n"
                    f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                    f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_STANDARD_OS})\n"
                    f"*Live Futures Price:* `${live_price}`"
                )
                send_telegram_alert(msg)
                state["last_alert_time"] = now
                state["last_tier"] = "STANDARD_OS"

    except Exception:
        pass


def execute_market_sweep(symbols):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_futures_candle, sym) for sym in symbols]
        for future in as_completed(futures):
            pass


if __name__ == "__main__":
    print("Starting CoinDCX Futures-Only Live 1h RSI Scanner...")
    all_symbols = get_active_futures_pairs()

    send_telegram_alert(
        f"🤖 *CoinDCX Futures Scanner Online*\n"
        f"Monitoring `{len(all_symbols)}` active Perpetual Futures contracts.\n"
        f"*Thresholds:* RSI <= {RSI_STANDARD_OS} (Oversold) | RSI >= {RSI_STANDARD_OB} (Overbought)."
    )

    while True:
        cycle_start = time.time()
        execute_market_sweep(all_symbols)
        elapsed = time.time() - cycle_start
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
