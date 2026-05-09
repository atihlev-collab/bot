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
# FILES
# =========================================================
LIVE_SENT_FILE = "live_sent.txt"

# =========================================================
# LOAD LIVE SIGNALS
# =========================================================
try:

    with open(LIVE_SENT_FILE, "r") as f:

        live_sent = set(
            x.strip() for x in f.readlines()
        )

except:

    live_sent = set()

# =========================================================
# STORAGE
# =========================================================
history = {}

prematch_sent = set()

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
# SAVE SIGNAL
# =========================================================
def save_live_signal(key):

    if key in live_sent:
        return

    live_sent.add(key)

    with open(LIVE_SENT_FILE, "a") as f:
        f.write(key + "\n")

# =========================================================
# UNIQUE KEY
# =========================================================
def unique_key(home, away, market):

    return f"{home.strip()}_{away.strip()}_{market}".lower()

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

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                prematch_key = unique_key(
                    home,
                    away,
                    "PREMATCH"
                )

                if prematch_key in prematch_sent:
                    continue

                score = 0
                market = "OVER 1.5 GOALS"

                # =====================================================
                # LEAGUE SCORE
                # =====================================================

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

                if "Norway" in country:
                    score += 5

                if "Sweden" in country:
                    score += 5

                if "Denmark" in country:
                    score += 5

                if "Argentina" in country:
                    score += 5

                if "Brazil" in country:
                    score += 6

                # =====================================================
                # BIG TEAMS
                # =====================================================

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
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M"),
                    "score": score,
                    "market": market,
                    "prematch_key": prematch_key
                })

            except:
                pass

        result = sorted(
            result,
            key=lambda x: x["score"],
            reverse=True
        )

        return result[:10]

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

                if g["prematch_key"] in prematch_sent:
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

                prematch_sent.add(
                    g["prematch_key"]
                )

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

                    fixture_name = f"{home}_{away}"

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

                    # =====================================================
                    # HISTORY
                    # =====================================================

                    if fixture_name not in history:

                        history[fixture_name] = []

                    history[fixture_name].append({
                        "minute": minute,
                        "ha": ha,
                        "aa": aa,
                        "hsh": hsh,
                        "ash": ash
                    })

                    history[fixture_name] = history[fixture_name][-25:]

                    hist = history[fixture_name]

                    # =====================================================
                    # OVER 1.5
                    # =====================================================

                    over_key = unique_key(
                        home,
                        away,
                        "OVER15"
                    )

                    if over_key not in live_sent:

                        over_ticks = 0

                        for h in hist:

                            if (
                                h["hsh"] >= 2
                                and h["ash"] >= 2
                            ):
                                over_ticks += 1

                        if (
                            over_ticks >= 10
                            and hsh >= 3
                            and ash >= 3
                        ):

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

                            save_live_signal(over_key)

                    # =====================================================
                    # UNDER 1.5
                    # =====================================================

                    under_key = unique_key(
                        home,
                        away,
                        "UNDER15"
                    )

                    if under_key not in live_sent:

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
                            and under_ticks >= 12
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

                            save_live_signal(under_key)

                    # =====================================================
                    # NEXT GOAL HOME
                    # =====================================================

                    next_home_key = unique_key(
                        home,
                        away,
                        "NEXTHOME"
                    )

                    if next_home_key not in live_sent:

                        home_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] >= h["aa"] + 5
                                and h["hsh"] >= 2
                            ):
                                home_ticks += 1

                        if (
                            home_ticks >= 10
                            and hsh >= 3
                        ):

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

                            save_live_signal(next_home_key)

                    # =====================================================
                    # NEXT GOAL AWAY
                    # =====================================================

                    next_away_key = unique_key(
                        home,
                        away,
                        "NEXTAWAY"
                    )

                    if next_away_key not in live_sent:

                        away_ticks = 0

                        for h in hist:

                            if (
                                h["aa"] >= h["ha"] + 5
                                and h["ash"] >= 2
                            ):
                                away_ticks += 1

                        if (
                            away_ticks >= 10
                            and ash >= 3
                        ):

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

                            save_live_signal(next_away_key)

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
