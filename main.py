import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import requests
from telegram import Bot
from datetime import datetime
from zoneinfo import ZoneInfo

from config import BOT_TOKEN, API_KEY, CHAT_ID
from scanner import get_matches, analyze_match

BOT = Bot(token=BOT_TOKEN)

HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")

sent = set()

CHECK_INTERVAL = 300  # 5 минути


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

                # време на мача
                dt = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                if dt <= now:
                    continue

                # анализ от PRO scanner
                res = analyze_match(m, HEADERS)
                if not res:
                    continue

                pick, prob, odds = max(res, key=lambda x: x[1])

                # 🔥 основен филтър (най-важното)
                if prob < 77:
                    continue

                # odds контрол
                if odds < 1.70 or odds > 2.30:
                    continue

                msg = f"""📈 VALUE

{m['teams']['home']['name']} vs {m['teams']['away']['name']}

👉 {pick}
📊 {round(prob,1)}% | 💰 {odds}
"""

                await BOT.send_message(chat_id=CHAT_ID, text=msg)

                sent.add(fid)
                count += 1

        except Exception as e:
            print("ERROR:", e)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    print("🚀 STABLE BOT RUNNING 24/7")
    asyncio.run(run())
