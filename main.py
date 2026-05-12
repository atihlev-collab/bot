import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import random
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

# ПО-БЪРЗ LIVE CHECK
LIVE_INTERVAL = 30

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
# LOW SCORING COUNTRIES
# =========================================================
LOW_SCORING_COUNTRIES = [
    "Poland",
    "Romania",
    "Bulgaria",
    "Croatia",
    "Slovenia"
]

# =========================================================
# NIGHT COUNTRIES
# =========================================================
NIGHT_COUNTRIES = [
    "Brazil",
    "Argentina",
    "USA",
    "Mexico",
    "Colombia",
    "Chile",
    "Uruguay",
    "Paraguay",
    "Peru"
]

# =========================================================
# STORAGE
# =========================================================
history = {}
sent_signals = set()

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
# UNIQUE SIGNAL
# =========================================================
def signal_exists(fixture_id, market):

    return f"{fixture_id}_{market}" in sent_signals

def save_signal(fixture_id, market):

    sent_signals.add(
        f"{fixture_id}_{market}"
    )

# =========================================================
# PICK LOGIC
# =========================================================
def generate_pick(country, league, home, away):

    pick = "OVER 2.5 GOALS"
    odd = "1.85"
    score = 5

    # =====================================================
    # LOW SCORING COUNTRIES
    # =====================================================
    if any(
        x.lower() in country.lower()
        for x in LOW_SCORING_COUNTRIES
    ):

        pick = "UNDER 2.5 GOALS"
        odd = "1.70"
        score += 7

    # =====================================================
    # LEAGUES
    # =====================================================
    elif (
        "Premier League" in league
        or "Champions League" in league
    ):

        pick = "GOAL GOAL"
        odd = "1.80"
        score += 10

    elif (
        "Bundesliga" in league
        or "Eredivisie" in league
        or "MLS" in league
        or "Brasileirao" in league
    ):

        pick = "OVER 2.5 GOALS"
        odd = "1.75"
        score += 9

    elif (
        "Serie A" in league
        or "Ligue 1" in league
    ):

        pick = "UNDER 2.5 GOALS"
        odd = "1.65"
        score += 8

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
        "PSG",
        "Inter",
        "Milan",
        "Juventus",
        "Flamengo",
        "Palmeiras",
        "River Plate",
        "Boca Juniors"
    ]

    if any(
        x.lower() in home.lower()
        for x in big_teams
    ):

        pick = "1"
        odd = "1.65"
        score += 5

    elif any(
        x.lower() in away.lower()
        for x in big_teams
    ):

        pick = "2"
        odd = "1.75"
        score += 5

    return pick, odd, score

# =========================================================
# TODAY COMMAND
# =========================================================
def today(update: Update, context: CallbackContext):

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=50",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        valid_matches = []

        for m in matches:

            try:

                league = m["league"]["name"]
                country = m["league"]["country"]

                if blocked(country, league):
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                if hour < 8 or hour > 23:
                    continue

                pick, odd, score = generate_pick(
                    country,
                    league,
                    home,
                    away
                )

                valid_matches.append({
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M"),
                    "pick": pick,
                    "odd": odd,
                    "score": score
                })

            except:
                pass

        if not valid_matches:

            update.message.reply_text(
                "❌ Няма подходящи мачове."
            )
            return

        valid_matches = sorted(
            valid_matches,
            key=lambda x: x["score"],
            reverse=True
        )

        picks_count = random.randint(1, 3)

        selected = valid_matches[:picks_count]

        msg = "📈 TODAY BEST PICKS\n"

        for g in selected:

            msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['pick']}
💰 Odd: {g['odd']}
"""

        update.message.reply_text(msg)

    except Exception as e:

        print("TODAY ERROR:", e)

        update.message.reply_text(
            "❌ Грешка при today."
        )

# =========================================================
# NIGHT COMMAND
# =========================================================
def night(update: Update, context: CallbackContext):

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=80",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        valid_matches = []

        for m in matches:

            try:

                league = m["league"]["name"]
                country = m["league"]["country"]

                if blocked(country, league):
                    continue

                if not any(
                    x.lower() in country.lower()
                    for x in NIGHT_COUNTRIES
                ):
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                if 8 <= hour <= 23:
                    continue

                pick, odd, score = generate_pick(
                    country,
                    league,
                    home,
                    away
                )

                score += 2

                valid_matches.append({
                    "country": country,
                    "league": league,
                    "home": home,
                    "away": away,
                    "time": date.strftime("%H:%M"),
                    "pick": pick,
                    "odd": odd,
                    "score": score
                })

            except:
                pass

        if not valid_matches:

            update.message.reply_text(
                "❌ Няма нощни мачове."
            )
            return

        valid_matches = sorted(
            valid_matches,
            key=lambda x: x["score"],
            reverse=True
        )

        picks_count = random.randint(1, 3)

        selected = valid_matches[:picks_count]

        msg = "🌙 NIGHT BEST PICKS\n"

        for g in selected:

            msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['pick']}
💰 Odd: {g['odd']}
"""

        update.message.reply_text(msg)

    except Exception as e:

        print("NIGHT ERROR:", e)

        update.message.reply_text(
            "❌ Грешка при night."
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

                    fixture = str(
                        m["fixture"]["id"]
                    )

                    status = m["fixture"]["status"]["short"]

                    if status in ["FT", "AET", "PEN"]:

                        history.pop(fixture, None)

                        continue

                    minute = (
                        m["fixture"]["status"]["elapsed"]
                        or 0
                    )

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

                    # =================================================
                    # HISTORY
                    # =================================================
                    if fixture not in history:

                        history[fixture] = []

                    last_minute = None

                    if len(history[fixture]) > 0:
                        last_minute = history[fixture][-1]["minute"]

                    if last_minute != minute:

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
                    # OVER 2.5
                    # =================================================
                    if not signal_exists(fixture, "OVER25"):

                        over_ticks = 0

                        for h in hist:

                            if (
                                (
                                    h["hsh"] >= 2
                                    or h["ash"] >= 2
                                )
                                and (
                                    h["ha"] + h["aa"]
                                ) >= 12
                            ):
                                over_ticks += 1

                        if (
                            goals <= 2
                            and over_ticks >= 2
                        ):

                            save_signal(
                                fixture,
                                "OVER25"
                            )

                            msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 OVER 2.5 GOALS
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                    # =================================================
                    # NEXT GOAL HOME
                    # =================================================
                    if not signal_exists(fixture, "NEXTHOME"):

                        home_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] >= h["aa"] + 2
                                and h["hsh"] >= 1
                            ):
                                home_ticks += 1

                        if home_ticks >= 2:

                            save_signal(
                                fixture,
                                "NEXTHOME"
                            )

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

                    # =================================================
                    # NEXT GOAL AWAY
                    # =================================================
                    if not signal_exists(fixture, "NEXTAWAY"):

                        away_ticks = 0

                        for h in hist:

                            if (
                                h["aa"] >= h["ha"] + 2
                                and h["ash"] >= 1
                            ):
                                away_ticks += 1

                        if away_ticks >= 2:

                            save_signal(
                                fixture,
                                "NEXTAWAY"
                            )

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

    dp.add_handler(
        CommandHandler("today", today)
    )

    dp.add_handler(
        CommandHandler("night", night)
    )

    print("✅ COMMANDS LOADED")

    updater.start_polling(
        drop_pending_updates=True
    )

    print("✅ POLLING STARTED")

    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    print("✅ LIVE THREAD STARTED")

    updater.idle()

if __name__ == "__main__":
    main()
