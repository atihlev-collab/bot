import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import threading
import requests

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
    "friendly",
    "next pro",
    "amateur",
    "regional"
]

# =========================================================
# STORAGE
# =========================================================
history = {}

prematch_sent = set()

# HARD ANTI SPAM
already_sent_matches = set()

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
# HARD ANTI SPAM
# =========================================================
def signal_sent(home, away, market):

    key = f"{home}_{away}_{market}".lower()

    return key in already_sent_matches


def save_signal(home, away, market):

    key = f"{home}_{away}_{market}".lower()

    already_sent_matches.add(key)

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
# PREMATCH MATCHES
# =========================================================
def get_prematch_matches():

    result = []

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=300",
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

                fixture = m["fixture"]["id"]

                if fixture in prematch_sent:
                    continue

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                score = 0
                market = "OVER 1.5 GOALS"

                # =================================================
                # LEAGUE SCORE
                # =========================================================

                if "Bundesliga" in league:
                    score += 10
                    market = "OVER 2.5 GOALS"

                if "Eredivisie" in league:
                    score += 10
                    market = "OVER 2.5 GOALS"

                if "Premier League" in league:
                    score += 9
                    market = "OVER 2.5 GOALS"

                if "Champions League" in league:
                    score += 9
                    market = "OVER 2.5 GOALS"

                if "MLS" in league:
                    score += 8
                    market = "OVER 2.5 GOALS"

                if "La Liga" in league:
                    score += 8

                if "Serie A" in league:
                    score += 7

                if "Libertadores" in league:
                    score += 7

                if "Championship" in league:
                    score += 6

                if "Turkey" in country:
                    score += 6

                if "Belgium" in country:
                    score += 6

                if "Austria" in country:
                    score += 6

                if "Switzerland" in country:
                    score += 5

                # =================================================
                # BIG TEAMS
                # =========================================================

                big_teams = [
                    "Manchester",
                    "Liverpool",
                    "Arsenal",
                    "Chelsea",
                    "Barcelona",
                    "Real Madrid",
                    "Bayern",
                    "Dortmund",
                    "PSG",
                    "Inter",
                    "Milan",
                    "Juventus",
                    "Ajax",
                    "Benfica",
                    "Porto"
                ]

                if any(x.lower() in home.lower() for x in big_teams):
                    score += 3

                if any(x.lower() in away.lower() for x in big_teams):
                    score += 3

                result.append({
                    "fixture": fixture,
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M"),
                    "score": score,
                    "market": market
                })

            except:
                pass

        result = sorted(
            result,
            key=lambda x: x["score"],
            reverse=True
        )

        return result[:6]

    except Exception as e:

        print("PREMATCH ERROR:", e)
        return []

# =========================================================
# TODAY
# =========================================================
def today(update: Update, context: CallbackContext):

    matches = get_prematch_matches()

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

🎯 {g['market']}
"""

    update.message.reply_text(msg)

# =========================================================
# NIGHT
# =========================================================
def night(update: Update, context: CallbackContext):

    matches = get_prematch_matches()

    if not matches:

        update.message.reply_text(
            "❌ Няма намерени нощни мачове."
        )
        return

    msg = "🌙 NIGHT TOP MATCHES\n"

    for g in matches:

        hour = int(g["time"].split(":")[0])

        if hour <= 8:

            msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['market']}
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

🎯 {g['market']}
"""

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg
                )

                prematch_sent.add(fixture)

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

                    fixture = f"{home}_{away}"

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={m['fixture']['id']}",
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

                    # =========================================================
                    # HISTORY
                    # =========================================================

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

                    # =========================================================
                    # OVER 1.5
                    # =========================================================

                    if not signal_sent(home, away, "OVER15"):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(home, away, "OVER15")

                    # =========================================================
                    # UNDER 1.5
                    # =========================================================

                    if not signal_sent(home, away, "UNDER15"):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(home, away, "UNDER15")

                    # =========================================================
                    # NEXT GOAL HOME
                    # =========================================================

                    if not signal_sent(home, away, "NEXTHOME"):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(home, away, "NEXTHOME")

                    # =========================================================
                    # NEXT GOAL AWAY
                    # =========================================================

                    if not signal_sent(home, away, "NEXTAWAY"):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            save_signal(home, away, "NEXTAWAY")

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
