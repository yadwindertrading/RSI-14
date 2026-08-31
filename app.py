import time
import requests
import pandas as pd

# ----------------- CONFIGURATION ----------------- #
# CoinDCX Pair: "B-BTC_USDT", "B-ETH_USDT", or "I-BTC_INR"
COINDCX_PAIR = "B-BTC_USDT"
INTERVAL = "1h"
RSI_PERIOD = 14
RSI_OVERBOUGHT = 80
RSI_OVERSOLD = 20

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_ID = "7203290966"

# Check frequency in seconds (every 5 minutes)
CHECK_INTERVAL_SECONDS = 300
# ------------------------------------------------- #

last_alerted_candle_time = None


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculates exact Wilder's Smoothed RSI (identical to TradingView / CoinDCX).
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's exponential smoothing alpha = 1 / period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def send_telegram_alert(message: str):
    """Sends a markdown-formatted message to your Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram error response: {response.text}")
    except Exception as e:
        print(f"Failed to reach Telegram API: {e}")


def fetch_and_evaluate():
    global last_alerted_candle_time

    # Fetch 300 historical candles so the RSI EMA smoothing matches TradingView precisely
    url = f"https://public.coindcx.com/market_data/candles/?pair={COINDCX_PAIR}&interval={INTERVAL}&limit=300"

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if not isinstance(data, list) or len(data) < RSI_PERIOD + 2:
            print("CoinDCX API returned insufficient or invalid candle data.")
            return

        # Sort candles oldest to newest
        df = pd.DataFrame(data)
        df = df.sort_values(by="time", ascending=True).reset_index(drop=True)
        df["close"] = df["close"].astype(float)

        # Compute RSI across the full history
        df["rsi"] = calculate_rsi(df["close"], period=RSI_PERIOD)

        # Index -2 is the most recently CLOSED candle; Index -1 is the actively fluctuating candle
        latest_closed_candle = df.iloc[-2]
        current_candle_time = latest_closed_candle["time"]
        current_rsi = latest_closed_candle["rsi"]
        close_price = latest_closed_candle["close"]

        print(
            f"[{COINDCX_PAIR}] Time: {current_candle_time} | "
            f"Closed 1h RSI: {current_rsi:.2f} | Close Price: {close_price}"
        )

        # Prevent duplicate alert triggers for the same candle
        if last_alerted_candle_time == current_candle_time:
            return

        if current_rsi >= RSI_OVERBOUGHT:
            msg = (
                f"🚨 *RSI OVERBOUGHT ALERT (CoinDCX)*\n\n"
                f"*Pair:* `{COINDCX_PAIR}`\n"
                f"*Timeframe:* 1 Hour\n"
                f"*RSI(14):* `{current_rsi:.2f}` (>= {RSI_OVERBOUGHT})\n"
                f"*Close Price:* `{close_price}`"
            )
            send_telegram_alert(msg)
            last_alerted_candle_time = current_candle_time

        elif current_rsi <= RSI_OVERSOLD:
            msg = (
                f"🟢 *RSI OVERSOLD ALERT (CoinDCX)*\n\n"
                f"*Pair:* `{COINDCX_PAIR}`\n"
                f"*Timeframe:* 1 Hour\n"
                f"*RSI(14):* `{current_rsi:.2f}` (<= {RSI_OVERSOLD})\n"
                f"*Close Price:* `{close_price}`"
            )
            send_telegram_alert(msg)
            last_alerted_candle_time = current_candle_time

    except Exception as e:
        print(f"Error during poll cycle: {e}")


if __name__ == "__main__":
    print(f"Starting CoinDCX 1h RSI Alert Engine for {COINDCX_PAIR}...")
    send_telegram_alert(
        f"🤖 *CoinDCX RSI Bot Active*\n"
        f"Monitoring `{COINDCX_PAIR}` on the `1h` timeframe."
    )

    while True:
        fetch_and_evaluate()
        time.sleep(CHECK_INTERVAL_SECONDS)
