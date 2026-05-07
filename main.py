import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import requests
from telegram import Bot
from datetime import datetime
from zoneinfo import ZoneInfo

from config import BOT_TOKEN, API_KEY, CHAT_ID
from scanner import get_matches, analyze_match

TZ = ZoneInfo("Europe/Sofia")

HEADERS = {
    "x-apisports-key": API_KEY
}

CHECK_INTERVAL = 300
LIVE_INTERVAL = 60

sent = set()
live_sent = set()
HYBRID = {}

logging.basicConfig(level=logging.WARNING)

# =====================
# PREMATCH
# =====================
async def prematch(bot):

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

                if prob < 68:
                    continue

                if odds < 1.45:
                    continue

                msg = f"""📈 PREMATCH

🏟 {m['teams']['home']['name']} vs {m['teams']['away']['name']}

👉 {pick}
📊 {round(prob,1)}%
💰 {odds}
"""

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg
                )

                sent.add(fid)
                HYBRID[fid] = True
                count += 1

        except Exception as e:
            print("PREMATCH ERROR:", e)

        await asyncio.sleep(CHECK_INTERVAL)


# =====================
# LIVE ENGINE
# =====================
async def live(bot):

    while True:

        try:

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=HEADERS
            ).json()

            for m in r.get("response", []):

                fid = m["fixture"]["id"]

                if fid not in HYBRID:
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                minute = m["fixture"]["status"]["elapsed"] or 0

                if minute < 20 or minute > 75:
                    continue

                # =====================
                # STATS
                # =====================
                try:

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fid}",
                        headers=HEADERS
                    ).json()

                    s = sr["response"]

                    # SHOTS
                    sh = int(s[0]["statistics"][2]["value"] or 0)
                    sa = int(s[1]["statistics"][2]["value"] or 0)

                    # ATTACKS
                    ah = int(s[0]["statistics"][0]["value"] or 0)
                    aa = int(s[1]["statistics"][0]["value"] or 0)

                except:
                    continue

                total_shots = sh + sa
                total_attacks = ah + aa

                pressure = total_attacks / max(1, minute)
                shot_rate = total_shots / max(1, minute)

                dominance_home = ah - aa
                dominance_away = aa - ah

                # =====================
                # ODDS
                # =====================
                try:

                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fid}",
                        headers=HEADERS
                    ).json()

                    mk = {
                        v["value"]: float(v["odd"])
                        for b in od["response"][0]["bookmakers"]
                        for bet in b["bets"]
                        for v in bet["values"]
                    }

                except:
                    continue

                # =====================
                # OVER 1.5
                # =====================
                key_over = f"{fid}_OVER"

                if key_over not in live_sent:

                    if minute >= 20:

                        if pressure >= 1.15:

                            if shot_rate >= 0.13:

                                if total_shots >= 5:

                                    odd = (
                                        mk.get("Over 1.5")
                                        or mk.get("Over 1.5 Goals")
                                    )

                                    if odd and odd >= 1.35:

                                        msg = f"""🔥 LIVE OVER 1.5

🏟 {home} vs {away}
⏱ {minute}'

📊 Attacks: {total_attacks}
📊 Shots: {total_shots}

👉 OVER 1.5 GOALS
💰 Odd: {odd}
"""

                                        await bot.send_message(
                                            chat_id=CHAT_ID,
                                            text=msg
                                        )

                                        live_sent.add(key_over)

                # =====================
                # UNDER 1.5
                # =====================
                key_under = f"{fid}_UNDER"

                if key_under not in live_sent:

                    if 25 <= minute <= 70:

                        if pressure <= 0.70:

                            if shot_rate <= 0.07:

                                if total_shots <= 3:

                                    odd = (
                                        mk.get("Under 1.5")
                                        or mk.get("Under 1.5 Goals")
                                    )

                                    if odd and odd >= 1.50:

                                        msg = f"""❄️ LIVE UNDER 1.5

🏟 {home} vs {away}
⏱ {minute}'

📊 Attacks: {total_attacks}
📊 Shots: {total_shots}

👉 UNDER 1.5 GOALS
💰 Odd: {odd}
"""

                                        await bot.send_message(
                                            chat_id=CHAT_ID,
                                            text=msg
                                        )

                                        live_sent.add(key_under)

                # =====================
                # NEXT GOAL HOME
                # =====================
                key_home = f"{fid}_HOME"

                if key_home not in live_sent:

                    if dominance_home >= 20:

                        if sh >= sa + 4:

                            if pressure >= 1.20:

                                odd = (
                                    mk.get(home)
                                    or mk.get("Home")
                                )

                                if odd and odd >= 1.50:

                                    msg = f"""⚡ NEXT GOAL HOME

🏟 {home} vs {away}
⏱ {minute}'

📊 Home attacks domination
📊 Shots: {sh} - {sa}

👉 NEXT GOAL {home}
💰 Odd: {odd}
"""

                                    await bot.send_message(
                                        chat_id=CHAT_ID,
                                        text=msg
                                    )

                                    live_sent.add(key_home)

                # =====================
                # NEXT GOAL AWAY
                # =====================
                key_away = f"{fid}_AWAY"

                if key_away not in live_sent:

                    if dominance_away >= 20:

                        if sa >= sh + 4:

                            if pressure >= 1.20:

                                odd = (
                                    mk.get(away)
                                    or mk.get("Away")
                                )

                                if odd and odd >= 1.50:

                                    msg = f"""⚡ NEXT GOAL AWAY

🏟 {home} vs {away}
⏱ {minute}'

📊 Away attacks domination
📊 Shots: {sh} - {sa}

👉 NEXT GOAL {away}
💰 Odd: {odd}
"""

                                    await bot.send_message(
                                        chat_id=CHAT_ID,
                                        text=msg
                                    )

                                    live_sent.add(key_away)

        except Exception as e:
            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)


# =====================
# MAIN
# =====================
async def main():

    bot = Bot(token=BOT_TOKEN)

    print("🚀 LIVE SYSTEM RUNNING")

    await asyncio.gather(
        prematch(bot),
        live(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())
