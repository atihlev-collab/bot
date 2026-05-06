import asyncio
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from config import BOT_TOKEN, API_KEY, CHAT_ID
from scanner import get_matches, analyze_match

HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")

sent = set()
CHECK_INTERVAL = 180  # по-често за лайв


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

            count = 0

            for m in matches:
                if count >= 5:
                    break

                fid = m["fixture"]["id"]
                if fid in sent:
                    continue

                # 👉 ВЗИМАМЕ ЛАЙВ МАЧОВЕ
                status = m["fixture"]["status"]["short"]

                if status not in ["1H", "2H"]:
                    continue

                minute = m["fixture"]["status"]["elapsed"]

                # 👉 ИГРАЕМ САМО В ТОЗИ ПРОЗОРЕЦ
                if not minute or minute < 10 or minute > 75:
                    continue

                res = analyze_match(m, HEADERS)
                if not res:
                    continue

                pick, prob, odds = max(res, key=lambda x: x[1])

                # 👉 САМО GOAL ПАЗАРИ
                if pick not in ["Over 2.5", "BTTS Yes"]:
                    continue

                if prob < 72:
                    continue

                if odds < 1.6 or odds > 2.5:
                    continue

                msg = f"""🔥 LIVE GOAL

{m['teams']['home']['name']} vs {m['teams']['away']['name']}
⏱️ {minute} мин

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
