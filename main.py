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
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext
)

from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================================================
# CONFIG
# =========================================================
HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

LIVE_INTERVAL = 60
PREMATCH_INTERVAL = 7200

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN)

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
# TOP LEAGUES
# =========================================================
TOP_LEAGUES = [
    "Premier League",
    "Champions League",
    "Europa League",
    "Conference League",
    "La Liga",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "Eredivisie",
    "Primeira Liga",
    "MLS",
    "Brasileirao",
    "Copa Libertadores"
]

# =========================================================
# STORAGE
# =========================================================
history = {}
sent_signals = {}
prematch_sent = {}

# =========================================================
# BLOCK CHECK
# =========================================================
def blocked(country, league):

    text = f"{country} {league}".lower()

    if any(x in text for x in BLOCKED_WORDS):
        return True

    if any(x in text for x in BAD_LEAGUES):
        return True

    return False

# =========================================================
# DUPLICATE
# =========================================================
def can_send(key, cooldown=1500):

    now = time.time()

    if key in sent_signals:

        if now - sent_signals[key] < cooldown:
            return False

    return True


def save_signal(key):

    sent_signals[key] = time.time()

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
# GET MATCHES
# =========================================================
def get_matches(mode="today"):

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

                league = m["league"]["name"]
                country = m["league"]["country"]

                if blocked(country, league):
                    continue

                if not any(
                    x.lower() in league.lower()
                    for x in TOP_LEAGUES
                ):
                    continue

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

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                result.append({
                    "league": league,
                    "country": country,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M")
                })

            except:
                pass

        return result[:3]

    except Exception as e:

        print("MATCH ERROR:", e)
        return []

# =========================================================
# PREMATCH MATCHES
# =========================================================
def get_prematch_matches():

    result = []

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=80",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        for m in matches:

            try:

                league = m["league"]["name"]
                country = m["league"]["country"]

                if blocked(country, league):
                    continue

                if not any(
                    x.lower() in league.lower()
                    for x in TOP_LEAGUES
                ):
                    continue

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                fixture = m["fixture"]["id"]

                result.append({
                    "fixture": fixture,
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M")
                })

            except:
                pass

        return result[:3]

    except Exception as e:

        print("PREMATCH ERROR:", e)
        return []

# =========================================================
# TODAY COMMAND
# =========================================================
def today(update: Update, context: CallbackContext):

    print("TODAY COMMAND RECEIVED")

    matches = get_matches("today")

    if not matches:

        update.message.reply_text(
            "❌ Няма намерени мачове."
        )
        return

    msg = "📈 TODAY TOP MATCHES\n"

    for g in matches:

        msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}
"""

    update.message.reply_text(msg)

# =========================================================
# NIGHT COMMAND
# =========================================================
def night(update: Update, context: CallbackContext):

    print("NIGHT COMMAND RECEIVED")

    matches = get_matches("night")

    if not matches:

        update.message.reply_text(
            "❌ Няма намерени нощни мачове."
        )
        return

    msg = "🌙 NIGHT TOP MATCHES\n"

    for g in matches:

        msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}
"""

    update.message.reply_text(msg)

# =========================================================
# PREMATCH LOOP
# =========================================================
async def prematch_loop():

    while True:

        try:

            matches = get_prematch_matches()

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

🎯 GOOD PREMATCH MATCH
"""

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg
                )

                prematch_sent[fixture] = time.time()

        except Exception as e:

            print("PREMATCH LOOP ERROR:", e)

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

                    minute = m["fixture"]["status"]["elapsed"] or 0

                    if minute < 20 or minute > 75:
                        continue

                    country = m["league"]["country"]
                    league = m["league"]["name"]

                    if blocked(country, league):
                        continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

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

                    if fixture not in history:

                        history[fixture] = []

                    history[fixture].append({
                        "minute": minute,
                        "ha": ha,
                        "aa": aa,
                        "hsh": hsh,
                        "ash": ash
                    })

                    history[fixture] = history[fixture][-25:]

                    hist = history[fixture]

                    # =================================================
                    # OVER 1.5
                    # =================================================
                    over_key = f"OVER15_{fixture}"

                    if can_send(over_key):

                        over_ticks = 0

                        for h in hist:

                            if (
                                h["hsh"] >= 2
                                and h["ash"] >= 2
                            ):
                                over_ticks += 1

                        if over_ticks >= 8:

                            msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 OVER 1.5 GOALS

📊 Home shots: {hsh}
📊 Away shots: {ash}
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

                    if can_send(under_key):

                        under_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] <= 5
                                and h["aa"] <= 5
                                and h["hsh"] < 2
                                and h["ash"] < 2
                            ):
                                under_ticks += 1

                        if (
                            minute >= 25
                            and minute <= 70
                            and goals == 0
                            and under_ticks >= 10
                        ):

                            msg = f"""
❄️ LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 UNDER 1.5 GOALS

📊 Home attacks: {ha}
📊 Away attacks: {aa}

📊 Home shots: {hsh}
📊 Away shots: {ash}
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

                    if can_send(next_home_key):

                        home_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] >= h["aa"] + 5
                                and h["hsh"] >= 2
                            ):
                                home_ticks += 1

                        if home_ticks >= 8:

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

📊 Home shots: {hsh}
📊 Away shots: {ash}
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

                    if can_send(next_away_key):

                        away_ticks = 0

                        for h in hist:

                            if (
                                h["aa"] >= h["ha"] + 5
                                and h["ash"] >= 2
                            ):
                                away_ticks += 1

                        if away_ticks >= 8:

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

📊 Home shots: {hsh}
📊 Away shots: {ash}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(next_away_key)

                except Exception as e:

                    print("MATCH ERROR:", e)

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

    updater = Updater(
        token=BOT_TOKEN,
        use_context=True
    )

    dp = updater.dispatcher

    dp.add_handler(CommandHandler("today", today))
    dp.add_handler(CommandHandler("night", night))

    updater.start_polling(
        drop_pending_updates=True
    )

    print("✅ COMMANDS ACTIVE")

    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    prematch_thread = threading.Thread(
        target=start_prematch_loop,
        daemon=True
    )

    live_thread.start()
    prematch_thread.start()

    print("✅ LIVE THREAD STARTED")
    print("✅ PREMATCH THREAD STARTED")

    updater.idle()

# =========================================================
# START
# =========================================================
if __name__ == "__main__":
    main()
