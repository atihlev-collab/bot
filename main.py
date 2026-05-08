import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import threading
import requests
import time

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================================================
# CONFIG
# =========================================================
HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

LIVE_INTERVAL = 60
PREMATCH_INTERVAL = 1800

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN)

# =========================================================
# STORAGE
# =========================================================
live_sent = {}
prematch_sent = set()
match_history = {}

# =========================================================
# BLOCKED
# =========================================================
BLOCKED_WORDS = [
    "russia",
    "russian",
    "belarus",
    "belarusian"
]

BAD_LEAGUES = [
    "reserve",
    "reserves",
    "youth",
    "u19",
    "u21",
    "u23",
    "women",
    "friendly"
]

# =========================================================
# BLOCK CHECK
# =========================================================
def blocked(country, league):

    text = f"{country} {league}".lower()

    if any(word in text for word in BLOCKED_WORDS):
        return True

    if any(word in text for word in BAD_LEAGUES):
        return True

    return False

# =========================================================
# GET STAT
# =========================================================
def get_stat(stats, name):

    try:
        for s in stats:

            if s["type"] == name:

                value = s["value"]

                if value is None:
                    return 0

                if isinstance(value, str):
                    value = value.replace("%", "")

                return int(value)

    except:
        pass

    return 0

# =========================================================
# DUPLICATE CHECK
# =========================================================
def recently_sent(key, cooldown=7200):

    now = time.time()

    if key in live_sent:

        if now - live_sent[key] < cooldown:
            return True

    return False

# =========================================================
# SAVE SIGNAL
# =========================================================
def save_signal(key):

    live_sent[key] = time.time()

# =========================================================
# PREMATCH
# =========================================================
def get_best_matches(mode="today"):

    result = []

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=150",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        for m in matches:

            try:

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                if mode == "today":

                    if hour < 8 or hour > 23:
                        continue

                if mode == "night":

                    if 8 <= hour <= 23:
                        continue

                country = m["league"]["country"]
                league = m["league"]["name"]

                if blocked(country, league):
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                fixture = m["fixture"]["id"]

                odd = 1.60
                market = "OVER 1.5 GOALS"

                try:

                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fixture}",
                        headers=HEADERS,
                        timeout=10
                    ).json()

                    odds_response = od.get("response", [])

                    if odds_response:

                        odds_map = {}

                        for b in odds_response[0]["bookmakers"]:

                            for bet in b["bets"]:

                                for v in bet["values"]:

                                    try:
                                        odds_map[v["value"]] = float(v["odd"])
                                    except:
                                        pass

                        if odds_map.get("Over 2.5"):

                            o25 = odds_map["Over 2.5"]

                            if 1.70 <= o25 <= 2.40:

                                odd = o25
                                market = "OVER 2.5 GOALS"

                        elif odds_map.get("Over 1.5"):

                            odd = odds_map["Over 1.5"]

                except:
                    pass

                result.append({
                    "fixture": fixture,
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M"),
                    "market": market,
                    "odd": odd
                })

            except:
                pass

        result = sorted(
            result,
            key=lambda x: x["odd"],
            reverse=True
        )

        return result[:3]

    except:
        return []

# =========================================================
# TODAY
# =========================================================
def today(update: Update, context: CallbackContext):

    matches = get_best_matches("today")

    if not matches:

        update.message.reply_text("❌ Няма намерени мачове.")
        return

    msg = "📈 TODAY TOP MATCHES\n"

    for g in matches:

        msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['market']}
📈 Odd: {g['odd']}
"""

    update.message.reply_text(msg)

# =========================================================
# NIGHT
# =========================================================
def night(update: Update, context: CallbackContext):

    matches = get_best_matches("night")

    if not matches:

        update.message.reply_text("❌ Няма намерени нощни мачове.")
        return

    msg = "🌙 NIGHT TOP MATCHES\n"

    for g in matches:

        msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['market']}
📈 Odd: {g['odd']}
"""

    update.message.reply_text(msg)

# =========================================================
# PREMATCH LOOP
# =========================================================
async def prematch_loop():

    while True:

        try:

            prematch_sent.clear()

            matches = get_best_matches("today")

            for g in matches:

                fixture = g["fixture"]

                if fixture in prematch_sent:
                    continue

                msg = f"""
📈 PREMATCH SIGNAL

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['market']}
📈 Odd: {g['odd']}
"""

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg
                )

                prematch_sent.add(fixture)

        except Exception as e:
            print("PREMATCH ERROR:", e)

        await asyncio.sleep(PREMATCH_INTERVAL)

# =========================================================
# LIVE LOOP
# =========================================================
async def live_loop():

    while True:

        try:

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=HEADERS,
                timeout=20
            ).json()

            matches = r.get("response", [])

            for m in matches:

                try:

                    fixture = m["fixture"]["id"]

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    country = m["league"]["country"]
                    league = m["league"]["name"]

                    if blocked(country, league):
                        continue

                    minute = m["fixture"]["status"]["elapsed"] or 0

                    if minute < 20 or minute > 75:
                        continue

                    gh = m["goals"]["home"] or 0
                    ga = m["goals"]["away"] or 0

                    goals = gh + ga

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture}",
                        headers=HEADERS,
                        timeout=10
                    ).json()

                    stats = sr.get("response", [])

                    if len(stats) < 2:
                        continue

                    hs = stats[0]["statistics"]
                    as_ = stats[1]["statistics"]

                    ha = get_stat(hs, "Attacks")
                    aa = get_stat(as_, "Attacks")

                    hsh = get_stat(hs, "Shots on Goal")
                    ash = get_stat(as_, "Shots on Goal")

                    # =================================================
                    # HISTORY
                    # =================================================
                    if fixture not in match_history:

                        match_history[fixture] = []

                    match_history[fixture].append({
                        "ha": ha,
                        "aa": aa,
                        "hsh": hsh,
                        "ash": ash
                    })

                    history = match_history[fixture][-5:]

                    avg_attacks = sum(
                        x["ha"] + x["aa"] for x in history
                    ) / len(history)

                    avg_shots = sum(
                        x["hsh"] + x["ash"] for x in history
                    ) / len(history)

                    # =================================================
                    # OVER 1.5
                    # =================================================
                    over_key = f"OVER15_{fixture}"

                    if not recently_sent(over_key):

                        if (
                            minute >= 20
                            and minute <= 75
                            and avg_attacks >= 6
                            and avg_shots >= 1
                        ):

                            msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 OVER 1.5 GOALS

📊 Avg attacks: {round(avg_attacks,1)}
📊 Avg shots: {round(avg_shots,1)}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(over_key)

                    # =================================================
                    # UNDER 1.5
                    # =================================================
                    under_key = f"UNDER15_{fixture}"

                    if not recently_sent(under_key):

                        if (
                            minute >= 55
                            and minute <= 70
                            and goals == 0
                            and avg_attacks <= 1
                            and avg_shots == 0
                        ):

                            msg = f"""
❄️ LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 UNDER 1.5 GOALS

📊 Avg attacks: {round(avg_attacks,1)}
📊 Avg shots: {round(avg_shots,1)}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(under_key)

                    # =================================================
                    # NEXT GOAL HOME
                    # =================================================
                    next_home_key = f"NEXTHOME_{fixture}"

                    if not recently_sent(next_home_key):

                        if (
                            minute >= 20
                            and minute <= 75
                            and ha >= aa + 2
                        ):

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 NEXT GOAL HOME

📊 Home attacks: {ha}
📊 Away attacks: {aa}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(next_home_key)

                    # =================================================
                    # NEXT GOAL AWAY
                    # =================================================
                    next_away_key = f"NEXTAWAY_{fixture}"

                    if not recently_sent(next_away_key):

                        if (
                            minute >= 20
                            and minute <= 75
                            and aa >= ha + 2
                        ):

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 NEXT GOAL AWAY

📊 Home attacks: {ha}
📊 Away attacks: {aa}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(next_away_key)

                except Exception as e:
                    print("LIVE MATCH ERROR:", e)

        except Exception as e:
            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# THREADS
# =========================================================
def start_live_loop():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(live_loop())

def start_prematch_loop():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(prematch_loop())

# =========================================================
# MAIN
# =========================================================
def main():

    print("🚀 LIVE SYSTEM RUNNING")

    updater = Updater(BOT_TOKEN, use_context=True)

    dp = updater.dispatcher

    dp.add_handler(CommandHandler("today", today))
    dp.add_handler(CommandHandler("night", night))

    updater.start_polling()

    live_thread = threading.Thread(
        target=start_live_loop
    )

    prematch_thread = threading.Thread(
        target=start_prematch_loop
    )

    live_thread.start()
    prematch_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
