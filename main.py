import os
os.system("pip install requests python-telegram-bot==13.15 joblib numpy scikit-learn")

import asyncio
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

from scanner import get_matches, analyze_match
from config import BOT_TOKEN, API_KEY, CHAT_ID

from ml_model import load_model, predict_btts
from ai_engine import ai_decision
from auto_optimize import load_config
from results_checker import check_results

# INIT
load_model()
cfg = load_config()

TZ = ZoneInfo("Europe/Sofia")
HEADERS = {"x-apisports-key": API_KEY}

CHECK_INTERVAL = 300
LIVE_INTERVAL = 60

MIN_PROB = 72
MAX_PROB = 80
MIN_ODDS = 1.60

START_BANK = 100
bank = START_BANK
LOSS_STREAK = 0

sent = set()
live_sent = set()
HYBRID = {}

logging.basicConfig(level=logging.WARNING)

# =====================
# RISK
# =====================
def risk_factor():
    factor = 1.0

    if LOSS_STREAK >= 3:
        factor *= 0.7
    if LOSS_STREAK >= 5:
        factor *= 0.5

    if bank < START_BANK * 0.85:
        factor *= 0.8

    return factor


def stake_calc(prob, odds):
    p = prob / 100
    kelly = (p * odds - 1) / (odds - 1)

    if kelly <= 0:
        return 0

    kelly *= 0.5 * risk_factor()
    stake = bank * kelly

    return max(min(stake, bank * 0.05), bank * 0.01)


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

                league = m["league"]["name"]

                if league in cfg["bad_leagues"]:
                    continue

                dt = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z","+00:00")
                ).astimezone(TZ)

                if dt <= now:
                    continue

                res = analyze_match(m, HEADERS)
                if not res:
                    continue

                pick, prob, odds = max(res, key=lambda x: x[1])

                if prob < MIN_PROB or prob > MAX_PROB:
                    continue

                if odds < MIN_ODDS:
                    continue

                msg = f"""📈 PREMATCH

{m['teams']['home']['name']} vs {m['teams']['away']['name']}
👉 {pick}
📊 {round(prob,1)}% | 💰 {odds}
"""

                await bot.send_message(chat_id=CHAT_ID, text=msg)

                sent.add(fid)
                HYBRID[fid] = True
                count += 1

        except Exception as e:
            print("ERROR:", e)

        await asyncio.sleep(CHECK_INTERVAL)


# =====================
# LIVE (BTTS ONLY)
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
                key = f"{home}_{away}"

                if key in live_sent:
                    continue

                minute = m["fixture"]["status"]["elapsed"] or 0

                if minute < 30 or minute > 72:
                    continue

                gh = m["goals"]["home"] or 0
                ga = m["goals"]["away"] or 0
                goals = gh + ga

                # RED CARD
                try:
                    if m["cards"]["red"]["home"] or m["cards"]["red"]["away"]:
                        continue
                except:
                    pass

                # GAME STATE
                if goals == 0 and minute < 40:
                    continue

                if goals == 1 and minute > 65:
                    continue

                # STATS
                try:
                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fid}",
                        headers=HEADERS
                    ).json()

                    s = sr["response"]

                    sh = int(s[0]["statistics"][2]["value"] or 0)
                    sa = int(s[1]["statistics"][2]["value"] or 0)

                    ah = int(s[0]["statistics"][0]["value"] or 0)
                    aa = int(s[1]["statistics"][0]["value"] or 0)

                except:
                    continue

                total_shots = sh + sa
                total_att = ah + aa

                pressure = total_att / max(1, minute)
                shot_rate = total_shots / max(1, minute)
                balance = abs(sh - sa)

                # 🔥 ФИЛТРИ (по-стегнати)
                if pressure < 1.3:
                    continue

                if shot_rate < 0.15:
                    continue

                if balance > 5:
                    continue

                if total_shots < 6:
                    continue

                # ODDS
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

                odd = mk.get("Yes")

                if not odd or odd < 1.55:
                    continue

                # ML FILTER
                try:
                    ml = predict_btts(sh, sa, ah, aa, goals)
                    if ml and ml < 0.55:
                        continue
                except:
                    pass

                stake = stake_calc(72, odd)

                msg = f"""🔥 LIVE BTTS

🏟 {home} vs {away}
⏱ {minute}' ({gh}:{ga})

📊 Pressure: {round(pressure,2)}
📊 Shots/min: {round(shot_rate,2)}

👉 GOAL / GOAL
📈 {odd}
💰 Stake: {round(stake,2)}
"""

                await bot.send_message(chat_id=CHAT_ID, text=msg)

                live_sent.add(key)

        except Exception as e:
            print("ERROR:", e)

        # 🔥 AUTO LEARNING (върнато)
        check_results()

        await asyncio.sleep(LIVE_INTERVAL)


# =====================
# MAIN
# =====================
async def main():
    bot = Bot(token=BOT_TOKEN)

    print("🚀 SYSTEM RUNNING")

    await asyncio.gather(
        prematch(bot),
        live(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())
