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
                print(f"[WARN] Alpha Vantage rate limit reached: {data['Note']}")
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

            records.append({
                "symbol": symbol,
                "price": price,
                "price_change": change,
                "change_percent": change_percent,
                "volume": volume,
                "trading_day": trading_day,
                "ingested_at": batch_time,
            })

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


def export_eod_summary(batch_time):
    """Generate a daily CSV report file for GitHub Actions artifact retention."""
    os.makedirs("reports", exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    
    query = """
    SELECT symbol, price, price_change, change_percent, volume, trading_day
    FROM equity_quotes
    WHERE ingested_at = ?
    ORDER BY change_percent DESC
    """
    df = pd.read_sql_query(query, conn, params=(batch_time,))
    conn.close()

    if df.empty:
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    csv_path = f"reports/market_summary_{date_str}.csv"
    df.to_csv(csv_path, index=False)
    print(f"[REPORT] Daily session report exported to {csv_path}")
    return csv_path


def send_discord_eod_digest(summary_df):
    """Send an End-of-Day market closing summary card to Discord."""
    fields = []
    for _, row in summary_df.iterrows():
        is_positive = row["change_percent"] >= 0
        direction_icon = "🟢" if is_positive else "🔴"
        sign = "+" if is_positive else ""
        
        fields.append({
            "name": f"{direction_icon} {row['symbol']}",
            "value": (
                f"**Close:** ${row['price']:,.2f}\n"
                f"**Change:** `{sign}{row['change_percent']:.2f}%` (${sign}{row['price_change']:.2f})\n"
                f"**Volume:** {row['volume']:,}"
            ),
            "inline": True
        })

    payload = {
        "username": "Finn Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2784/2784403.png",
        "embeds": [
            {
                "title": "📊 End-of-Day Market Digest",
                "description": (
                    "**Session Close Summary**\n"
                    "Alpha Vantage Global Quote Engine • SQLite ETL Pipeline"
                ),
                "color": 3447003,
                "fields": fields,
                "footer": {
                    "text": f"Batch Run: EOD Production • Cloud Runner (Ubuntu) • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                }
            }
        ]
    }

    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if res.status_code in [200, 204]:
            print("[DISPATCH] Discord EOD digest delivered successfully!")
        else:
            print(f"[ERROR] Discord webhook rejected with status: {res.status_code}")
    except Exception as e:
        print(f"[ERROR] Webhook error: {e}")


if __name__ == "__main__":
    print("[START] Running End-of-Day financial ETL pipeline...")
    current_batch = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    df = extract_data(current_batch)
    if not df.empty:
        load_to_sqlite(df)
        send_discord_eod_digest(df)
        export_eod_summary(current_batch)
    else:
        print("[WARN] No records ingested. Check your API key or network.")
    print("[FINISH] Pipeline completed.")