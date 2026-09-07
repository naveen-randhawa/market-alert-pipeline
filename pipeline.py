import os
import sqlite3
import time
from datetime import datetime, timezone
import pandas as pd
import requests

# Fallback to config.py for local development if environment variables aren't set
try:
    import config
    DEFAULT_API_KEY = getattr(config, "API_KEY", "")
    DEFAULT_WEBHOOK_URL = getattr(config, "WEBHOOK_URL", "")
except ImportError:
    DEFAULT_API_KEY = ""
    DEFAULT_WEBHOOK_URL = ""

API_KEY = os.getenv("ALPHA_VANTAGE_KEY", DEFAULT_API_KEY)
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)

TICKERS = ["AAPL", "MSFT", "NVDA"]
DB_NAME = "market_data.db"
VOLATILITY_THRESHOLD_PCT = 1.5  # Test at 0.0, then set 1.5 once tested


def extract_data(batch_time):
    """Extract real-time equity quotes from Alpha Vantage."""
    records = []
    base_url = "https://www.alphavantage.co/query"

    for symbol in TICKERS:
        print(f"[EXTRACT] Fetching quote for {symbol}...")
        params = {
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": API_KEY,
        }

        try:
            response = requests.get(base_url, params=params, timeout=10)
            data = response.json()

            if "Note" in data:
                print(
                    f"[WARN] Alpha Vantage rate limit reached: {data['Note']}"
                )
                break

            quote = data.get("Global Quote", {})
            if not quote:
                print(f"[WARN] No quote returned for {symbol}. Response: {data}")
                continue

            price = float(quote.get("05. price", 0.0))
            change = float(quote.get("09. change", 0.0))
            change_percent = float(
                quote.get("10. change percent", "0.0%").replace("%", "")
            )
            volume = int(quote.get("06. volume", 0))
            trading_day = quote.get("07. latest trading day", "")

            records.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "price_change": change,
                    "change_percent": change_percent,
                    "volume": volume,
                    "trading_day": trading_day,
                    "ingested_at": batch_time,
                }
            )

            # Politeness delay to prevent free-tier API throttling
            time.sleep(12)

        except Exception as e:
            print(f"[ERROR] Failed fetching {symbol}: {e}")

    return pd.DataFrame(records)


def load_to_sqlite(df):
    """Store cleaned data into a local relational database."""
    conn = sqlite3.connect(DB_NAME)
    df.to_sql("equity_quotes", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()
    print(f"[LOAD] Successfully saved {len(df)} records into {DB_NAME}.")


def run_volatility_detection_and_alert(batch_time):
    """Query the database for movements within the current batch and alert Discord."""
    conn = sqlite3.connect(DB_NAME)
    query = """
    SELECT symbol, price, price_change, change_percent, volume, trading_day
    FROM equity_quotes
    WHERE ingested_at = ?
      AND ABS(change_percent) >= ?
    """
    anomalies = pd.read_sql_query(
        query, conn, params=(batch_time, VOLATILITY_THRESHOLD_PCT)
    )
    conn.close()

    if not anomalies.empty:
        print(f"[ALERT] {len(anomalies)} volatility events found. Sending to Discord...")
        send_discord_alert(anomalies)
    else:
        print(f"[MONITOR] Nominal market conditions. No swings >= {VOLATILITY_THRESHOLD_PCT}%.")


def send_discord_alert(anomalies_df):
    """Send a structured, professional embed card to Discord."""
    fields = []
    for _, row in anomalies_df.iterrows():
        is_positive = row["change_percent"] >= 0
        direction_icon = "🟢" if is_positive else "🔴"
        sign = "+" if is_positive else ""
        
        fields.append({
            "name": f"{direction_icon} {row['symbol']}",
            "value": (
                f"**Price:** ${row['price']:,.2f}\n"
                f"**Change:** `{sign}{row['change_percent']:.2f}%` (${sign}{row['price_change']:.2f})\n"
                f"**Volume:** {row['volume']:,}"
            ),
            "inline": True  # Places stocks side-by-side in columns
        })

    # Discord rich embed payload
    payload = {
        "username": "Finn Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2784/2784403.png",
        "embeds": [
            {
                "title": "Market Volatility Update",
                "description": (
                    f"**Trigger:** Daily price movement |Δ| ≥ `{VOLATILITY_THRESHOLD_PCT}%`\n"
                    f"**Source:** Alpha Vantage Global Quote Engine"
                ),
                "color": 3447003,  # Sleek dark blue/slate accent bar
                "fields": fields,
                "footer": {
                    "text": f"Batch Ingestion • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                }
            }
        ]
    }

    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if res.status_code in [200, 204]:
            print("[DISPATCH] Discord rich embed alert delivered successfully!")
        else:
            print(f"[ERROR] Discord webhook rejected with status: {res.status_code}")
    except Exception as e:
        print(f"[ERROR] Webhook error: {e}")


if __name__ == "__main__":
    print("[START] Running financial ETL pipeline...")
    current_batch = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    df = extract_data(current_batch)
    if not df.empty:
        load_to_sqlite(df)
        run_volatility_detection_and_alert(current_batch)
    else:
        print("[WARN] No records ingested. Check your API key or network.")
    print("[FINISH] Pipeline completed.")