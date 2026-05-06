import asyncio
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from config import BOT_TOKEN, API_KEY, CHAT_ID
from scanner import get_matches, analyze_match

HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")

sent = set()
CHECK_INTERVAL = 300


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text
    }
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


async def run():
    while True:
        try:
            matches = get_matches(HEADERS)
            now = datetime.now(TZ)

            count = 0

            for m in matches:
                if count >= 5:
                    break

                fid = m["fixture"]["id"]
                if fid in sent:
                    continue

                dt = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                if dt <= now:
                    continue

                res = analyze_match(m, HEADERS)
                if not res:
                    continue

                pick, prob, odds = max(res, key=lambda x: x[1])

                if prob < 77:
                    continue

                if odds < 1.7 or odds > 2.3:
                    continue

                msg = f"""📈 VALUE

{m['teams']['home']['name']} vs {m['teams']['away']['name']}

👉 {pick}
📊 {round(prob,1)}% | 💰 {odds}
"""

                send_telegram(msg)

                sent.add(fid)
                count += 1

        except Exception as e:
            print("ERROR:", e)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
