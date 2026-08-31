import time
import requests
import pandas as pd
import pandas_ta as ta

# ----------------- CONFIGURATION ----------------- #
# Pair format examples: "B-BTC_USDT", "B-ETH_USDT", "I-BTC_INR"
COINDCX_PAIR = "B-BTC_USDT" 
INTERVAL = "1h"
RSI_PERIOD = 14
RSI_OVERBOUGHT = 80
RSI_OVERSOLD = 20

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8871724356:AAEQb7OP9gvoDLDKebLIpywuGdE8aVFka3A"
TELEGRAM_CHAT_ID = "7203290966"

CHECK_INTERVAL_SECONDS = 300  # Check every 5 minutes
# ------------------------------------------------- #

last_alerted_candle_time = None

def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram error: {response.text}")
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

def fetch_and_evaluate():
    global last_alerted_candle_time
    url = f"https://public.coindcx.com/market_data/candles/?pair={COINDCX_PAIR}&interval={INTERVAL}&limit=50"
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if not isinstance(data, list) or len(data) < RSI_PERIOD + 2:
            print("Insufficient candle data received.")
            return

        # Sort ascending by time
        df = pd.DataFrame(data)
        df = df.sort_values(by="time", ascending=True).reset_index(drop=True)
        df["close"] = df["close"].astype(float)

        # Compute RSI(14)
        df["rsi"] = ta.rsi(df["close"], length=RSI_PERIOD)

        # Latest completed candle is at index -2
        latest_closed_candle = df.iloc[-2]
        current_candle_time = latest_closed_candle["time"]
        current_rsi = latest_closed_candle["rsi"]
        close_price = latest_closed_candle["close"]

        print(f"[{COINDCX_PAIR}] Closed Candle RSI: {current_rsi:.2f} | Price: {close_price}")

        # Prevent duplicate alerts for the same 1h candle
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
        print(f"Error fetching data: {e}")

if __name__ == "__main__":
    print(f"Starting CoinDCX RSI Alert Bot for {COINDCX_PAIR}...")
    # Send a startup notification to confirm the bot is active on Telegram
    send_telegram_alert(f"🤖 *CoinDCX RSI Bot Started*\nMonitoring `{COINDCX_PAIR}` on 1-hour timeframe.")
    
    while True:
        fetch_and_evaluate()
        time.sleep(CHECK_INTERVAL_SECONDS)
