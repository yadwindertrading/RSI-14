import os
import time
import threading
import requests
import pandas as pd
from http.server import HTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
CANDLE_LIMIT = 250             # 250 bars ensures full Wilder's RMA mathematical convergence
MIN_CANDLES_REQUIRED = 50      # Minimum historical bars required to compute reliable RSI

# Alert Thresholds
RSI_STANDARD_OB = 90.0
RSI_EXTREME_OB = 95.0

RSI_STANDARD_OS = 12.0
RSI_EXTREME_OS = 9.0

COOLDOWN_SECONDS = 15 * 60     # 15-minute cooldown reminder for persistent extreme setups
CYCLE_INTERVAL_SECONDS = 180   # 3-minute full sweep interval (paces requests safely)
HEARTBEAT_INTERVAL_SECONDS = 6 * 3600  # 6-hour status ping
MAX_WORKERS = 6                # Concurrency pool size
# --------------------------------------------------------- #

# Telegram Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_IDS = ["7203290966", "630462102"]

# State tracker: { symbol: {"last_alert_time": float, "last_tier": str} }
tracker = {}

# Reusable HTTP Session with connection pooling and automated retries
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    raise_on_status=False
)
adapter = HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15)
session.mount("https://", adapter)
session.mount("http://", adapter)

# Standard browser headers to avoid cloud WAF blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}


def get_active_futures_pairs():
    """
    1. Fetches active CoinDCX futures contracts directly from active_instruments endpoint.
    2. Maps CoinDCX pairs (e.g. 'B-BTC_USDT') to both CoinDCX format and Binance ticker ('BTCUSDT').
    """
    futures_endpoint = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
    
    try:
        resp = session.get(futures_endpoint, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        raw_list = resp.json()

        instruments = raw_list if isinstance(raw_list, list) else raw_list.get("data", [])
        
        pairs = []
        for item in instruments:
            coindcx_pair = ""
            if isinstance(item, str):
                coindcx_pair = item
            elif isinstance(item, dict):
                coindcx_pair = item.get("pair") or item.get("symbol", "")

            if "USDT" in coindcx_pair:
                binance_clean = coindcx_pair.split("-", 1)[-1].replace("_", "").upper()
                pairs.append((binance_clean, coindcx_pair))

        # Deduplicate by Binance symbol
        seen = set()
        unique_pairs = []
        for b_sym, c_pair in pairs:
            if b_sym not in seen:
                seen.add(b_sym)
                unique_pairs.append((b_sym, c_pair))

        unique_pairs.sort(key=lambda x: x[0])
        print(f"Loaded {len(unique_pairs)} active CoinDCX Futures perpetual contracts.", flush=True)
        return unique_pairs

    except Exception as e:
        print(f"Error querying CoinDCX futures directory: {e}. Using core fallback contracts.", flush=True)
        return [
            ("BTCUSDT", "B-BTC_USDT"),
            ("ETHUSDT", "B-ETH_USDT"),
            ("SOLUSDT", "B-SOL_USDT"),
            ("BNBUSDT", "B-BNB_USDT"),
            ("XRPUSDT", "B-XRP_USDT")
        ]


def calculate_wilders_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Vectorized Wilder's Exponentially Smoothed RSI (matching TradingView and exchange engines).
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

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
            session.post(url, json=payload, timeout=8)
        except Exception as e:
            print(f"Telegram dispatch failed for {chat_id}: {e}", flush=True)


def format_display_symbol(symbol: str) -> str:
    """Formats raw tickers into readable format (e.g. 'BTCUSDT' -> 'BTC/USDT')."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def fetch_candles(binance_sym: str, coindcx_pair: str):
    """
    Primary: Fetches from Binance Futures API.
    Verified Secondary Fallback: Uses the official CoinDCX REST candles endpoint (api.coindcx.com)
    which returns 250 bars without geoblocks.
    """
    # 1. Primary Route: Binance Futures
    binance_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_sym}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    try:
        res = session.get(binance_url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) >= MIN_CANDLES_REQUIRED:
                df = pd.DataFrame(data)
                clean_df = pd.DataFrame({
                    "time": pd.to_numeric(df[0], errors="coerce"),
                    "close": pd.to_numeric(df[4], errors="coerce")
                }).dropna()
                return clean_df.drop_duplicates(subset=["time"]).sort_values(by="time", ascending=True).reset_index(drop=True)
    except Exception:
        pass

    # 2. Verified Fallback Route: CoinDCX Official REST Candles Endpoint
    coindcx_url = f"https://api.coindcx.com/market_data/candles?pair={coindcx_pair}&interval={INTERVAL}"
    try:
        res = session.get(coindcx_url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) >= MIN_CANDLES_REQUIRED:
                df = pd.DataFrame(data)
                time_col = "time" if "time" in df.columns else "timestamp"
                clean_df = pd.DataFrame({
                    "time": pd.to_numeric(df[time_col], errors="coerce"),
                    "close": pd.to_numeric(df["close"], errors="coerce")
                }).dropna()
                # CoinDCX delivers newest candle first; sort chronologically for RMA RSI calculation
                return clean_df.drop_duplicates(subset=["time"]).sort_values(by="time", ascending=True).reset_index(drop=True)
    except Exception:
        pass

    return None


def process_futures_candle(token_tuple):
    """Processes live 1-hour candles for a given contract and checks alert conditions."""
    global tracker
    binance_sym, coindcx_pair = token_tuple
    now = time.time()

    clean_df = fetch_candles(binance_sym, coindcx_pair)
    if clean_df is None or len(clean_df) < MIN_CANDLES_REQUIRED:
        return False

    clean_df["rsi"] = calculate_wilders_rsi(clean_df["close"], period=RSI_PERIOD)

    live_candle = clean_df.iloc[-1]
    current_rsi = live_candle["rsi"]
    live_price = live_candle["close"]

    if pd.isna(current_rsi):
        return False

    display_name = format_display_symbol(binance_sym)

    if binance_sym not in tracker:
        tracker[binance_sym] = {"last_alert_time": 0, "last_tier": None}

    state = tracker[binance_sym]
    time_since_alert = now - state["last_alert_time"]

    # Reset state when RSI returns completely to neutral territory
    if RSI_STANDARD_OS < current_rsi < RSI_STANDARD_OB:
        state["last_tier"] = None
        return True

    # ----------------- OVERBOUGHT SIGNALS (>= 90.0) ----------------- #
    if current_rsi >= RSI_EXTREME_OB:
        if state["last_tier"] != "EXTREME_OB" or time_since_alert >= COOLDOWN_SECONDS:
            msg = (
                f"🔥 *FUTURES CRITICAL OVERBOUGHT*\n\n"
                f"*Pair:* `{display_name}` (`{binance_sym}`)\n"
                f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_EXTREME_OB})\n"
                f"*Live Futures Price:* `${live_price}`"
            )
            send_telegram_alert(msg)
            state["last_alert_time"] = now
            state["last_tier"] = "EXTREME_OB"

    elif current_rsi >= RSI_STANDARD_OB:
        if state["last_tier"] != "STANDARD_OB" or time_since_alert >= COOLDOWN_SECONDS:
            msg = (
                f"🚨 *FUTURES RSI OVERBOUGHT*\n\n"
                f"*Pair:* `{display_name}` (`{binance_sym}`)\n"
                f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_STANDARD_OB})\n"
                f"*Live Futures Price:* `${live_price}`"
            )
            send_telegram_alert(msg)
            state["last_alert_time"] = now
            state["last_tier"] = "STANDARD_OB"

    # ----------------- OVERSOLD SIGNALS (<= 12.0) ----------------- #
    elif current_rsi <= RSI_EXTREME_OS:
        if state["last_tier"] != "EXTREME_OS" or time_since_alert >= COOLDOWN_SECONDS:
            msg = (
                f"❄️ *FUTURES CRITICAL OVERSOLD*\n\n"
                f"*Pair:* `{display_name}` (`{binance_sym}`)\n"
                f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_EXTREME_OS})\n"
                f"*Live Futures Price:* `${live_price}`"
            )
            send_telegram_alert(msg)
            state["last_alert_time"] = now
            state["last_tier"] = "EXTREME_OS"

    elif current_rsi <= RSI_STANDARD_OS:
        if state["last_tier"] != "STANDARD_OS" or time_since_alert >= COOLDOWN_SECONDS:
            msg = (
                f"🟢 *FUTURES RSI OVERSOLD*\n\n"
                f"*Pair:* `{display_name}` (`{binance_sym}`)\n"
                f"*Timeframe:* 1 Hour (Live Futures Candle)\n"
                f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_STANDARD_OS})\n"
                f"*Live Futures Price:* `${live_price}`"
            )
            send_telegram_alert(msg)
            state["last_alert_time"] = now
            state["last_tier"] = "STANDARD_OS"

    return True


def execute_market_sweep(pairs):
    success_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_futures_candle, pair) for pair in pairs]
        for future in as_completed(futures):
            try:
                if future.result():
                    success_count += 1
            except Exception:
                pass
    return success_count


if __name__ == "__main__":
    print("Starting CoinDCX Futures-Only Live 1h RSI Scanner...", flush=True)
    all_pairs = get_active_futures_pairs()

    send_telegram_alert(
        f"🤖 *CoinDCX Futures Scanner Online*\n\n"
        f"• *Contracts:* `{len(all_pairs)}` active perpetuals\n"
        f"• *Timeframe:* `1 Hour (Live Candle)`\n"
        f"• *Paced Interval:* ~3 Minutes per full market sweep\n"
        f"• *Thresholds:* RSI <= {RSI_STANDARD_OS} / {RSI_EXTREME_OS} (Oversold) | RSI >= {RSI_STANDARD_OB} / {RSI_EXTREME_OB} (Overbought)\n"
        f"• *Heartbeat:* Status ping every 6 hours."
    )

    last_heartbeat_time = time.time()

    while True:
        cycle_start = time.time()
        success_count = execute_market_sweep(all_pairs)

        # 6-hour status ping
        if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL_SECONDS:
            send_telegram_alert(
                f"💓 *System Status (6-Hour Heartbeat)*\n\n"
                f"• *Status:* Active & Scanning\n"
                f"• *Monitored Contracts:* `{len(all_pairs)}`\n"
                f"• *Timeframe:* `1h`\n"
                f"• *Parameters:* RSI <= {RSI_STANDARD_OS} | RSI >= {RSI_STANDARD_OB}"
            )
            last_heartbeat_time = time.time()

        elapsed = time.time() - cycle_start
        print(f"Cycle completed in {elapsed:.2f}s | Successfully ingested: {success_count}/{len(all_pairs)} tokens.", flush=True)
        sleep_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)
