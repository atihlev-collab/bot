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
# STORAGE
# =========================================================
sent_signals = {}
history = {}

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
def can_send(key, cooldown=7200):

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
# COMMANDS
# =========================================================
def today(update: Update, context: CallbackContext):

    update.message.reply_text(
        "✅ LIVE SYSTEM ACTIVE"
    )

def night(update: Update, context: CallbackContext):

    update.message.reply_text(
        "🌙 NIGHT MODE ACTIVE"
    )

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

                    # =================================================
                    # STATS
                    # =================================================
                    ha = get_stat(hs, "Attacks")
                    aa = get_stat(as_, "Attacks")

                    hsh = get_stat(hs, "Shots on Goal")
                    ash = get_stat(as_, "Shots on Goal")

                    total_attacks = ha + aa
                    total_shots = hsh + ash

                    # =================================================
                    # HISTORY
                    # =================================================
                    if fixture not in history:

                        history[fixture] = []

                    history[fixture].append({
                        "minute": minute,
                        "ha": ha,
                        "aa": aa,
                        "hsh": hsh,
                        "ash": ash,
                        "total_attacks": total_attacks,
                        "total_shots": total_shots
                    })

                    # пазим само последните 20 проверки
                    history[fixture] = history[fixture][-20:]

                    hist = history[fixture]

                    # =================================================
                    # OVER 1.5
                    # =================================================
                    # И ДВАТА ОТБОРА АТАКУВАТ
                    # ИМА УДАРИ
                    # 20 МИНУТИ ТЕМПО
                    # =================================================
                    over_key = f"OVER15_{fixture}"

                    if can_send(over_key):

                        active_ticks = 0

                        for h in hist:

                            if (
                                h["total_attacks"] >= 15
                                and h["total_shots"] >= 3
                                and h["ha"] >= 5
                                and h["aa"] >= 5
                            ):
                                active_ticks += 1

                        if active_ticks >= 8:

                            msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 OVER 1.5 GOALS

📊 Total attacks: {total_attacks}
📊 Total shots: {total_shots}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(over_key)

                    # =================================================
                    # UNDER 1.5
                    # =================================================
                    # НЯМА АТАКИ
                    # НЯМА УДАРИ
                    # DEAD GAME
                    # =================================================
                    under_key = f"UNDER15_{fixture}"

                    if can_send(under_key):

                        dead_ticks = 0

                        for h in hist:

                            if (
                                h["total_attacks"] <= 4
                                and h["total_shots"] == 0
                            ):
                                dead_ticks += 1

                        if (
                            minute >= 25
                            and minute <= 70
                            and goals == 0
                            and dead_ticks >= 10
                        ):

                            msg = f"""
❄️ LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 UNDER 1.5 GOALS

📊 Total attacks: {total_attacks}
📊 Total shots: {total_shots}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(under_key)

                    # =================================================
                    # NEXT GOAL HOME
                    # =================================================
                    # ДОМАШНИЯТ НАТИСКА 20 МИНУТИ
                    # =================================================
                    next_home_key = f"NEXTHOME_{fixture}"

                    if can_send(next_home_key):

                        dom_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] >= 15
                                and h["hsh"] >= 3
                                and h["ha"] >= h["aa"] + 5
                            ):
                                dom_ticks += 1

                        if dom_ticks >= 8:

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

                        dom_ticks = 0

                        for h in hist:

                            if (
                                h["aa"] >= 15
                                and h["ash"] >= 3
                                and h["aa"] >= h["ha"] + 5
                            ):
                                dom_ticks += 1

                        if dom_ticks >= 8:

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
# THREAD
# =========================================================
def start_live_loop():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        live_loop()
    )

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
        target=start_live_loop
    )

    live_thread.start()

    updater.idle()

# =========================================================
# START
# =========================================================
if __name__ == "__main__":
    main()
