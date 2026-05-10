import os
import sqlite3
import asyncio
import logging
import threading
import requests
import re

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
PREMATCH_INTERVAL = 1800

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN)

# =========================================================
# SQLITE
# =========================================================
conn = sqlite3.connect(
    "signals.db",
    check_same_thread=False
)

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS sent_signals (
    fixture_id TEXT,
    market TEXT,
    PRIMARY KEY (fixture_id, market)
)
""")

conn.commit()

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
# NORMALIZE
# =========================================================
def normalize(text):

    text = str(text).lower().strip()

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)

    return text

# =========================================================
# UNIQUE KEY
# =========================================================
def unique_key(home, away, market):

    home = normalize(home)
    away = normalize(away)
    market = normalize(market)

    return f"{home}_{away}_{market}"

# =========================================================
# SIGNAL DATABASE
# =========================================================
def can_send_signal(fixture_id, market):

    cursor.execute(
        """
        SELECT 1 FROM sent_signals
        WHERE fixture_id=? AND market=?
        """,
        (str(fixture_id), market)
    )

    row = cursor.fetchone()

    return row is None

def save_signal(fixture_id, market):

    try:

        cursor.execute(
            """
            INSERT OR IGNORE INTO sent_signals
            (fixture_id, market)
            VALUES (?, ?)
            """,
            (str(fixture_id), market)
        )

        conn.commit()

    except:
        pass

def clear_match_signals(fixture_id):

    try:

        cursor.execute(
            """
            DELETE FROM sent_signals
            WHERE fixture_id=?
            """,
            (str(fixture_id),)
        )

        conn.commit()

    except:
        pass

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
# PREMATCH
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
                    "prematch"
                )

                if prematch_key in prematch_sent:
                    continue

                score = 0
                market = "OVER 1.5 GOALS"

                # =====================================================
                # LEAGUE SCORING
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

                if "La Liga" in league:
                    score += 8

                if "Serie A" in league:
                    score += 7

                if "Brazil" in country:
                    score += 6

                if "Argentina" in country:
                    score += 5

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
                    "Juventus"
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

        return result[:20]

    except Exception as e:

        print("PREMATCH ERROR:", e)
        return []

# =========================================================
# TODAY COMMAND
# =========================================================
def today(update: Update, context: CallbackContext):

    matches = get_prematch_matches()

    if not matches:

        update.message.reply_text(
            "❌ Няма мачове."
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
# NIGHT COMMAND
# =========================================================
def night(update: Update, context: CallbackContext):

    matches = get_prematch_matches()

    if not matches:

        update.message.reply_text(
            "❌ Няма нощни мачове."
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

        processed_fixtures = set()

        try:

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=HEADERS,
                timeout=20
            ).json()

            matches = r.get("response", [])

            for m in matches:

                try:

                    fixture_id = m["fixture"]["id"]

                    # =================================================
                    # PROCESS ONLY ONCE
                    # =================================================
                    if fixture_id in processed_fixtures:
                        continue

                    processed_fixtures.add(fixture_id)

                    status = m["fixture"]["status"]["short"]

                    # =================================================
                    # CLEAR AFTER FT
                    # =================================================
                    if status in ["FT", "AET", "PEN"]:

                        clear_match_signals(
                            fixture_id
                        )

                        continue

                    minute = (
                        m["fixture"]["status"]["elapsed"]
                        or 0
                    )

                    # =================================================
                    # LIVE WINDOW
                    # =================================================
                    if minute < 1 or minute > 75:
                        continue

                    country = m["league"]["country"]
                    league = m["league"]["name"]

                    if blocked(country, league):
                        continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    gh = m["goals"]["home"] or 0
                    ga = m["goals"]["away"] or 0

                    fixture_name = unique_key(
                        home,
                        away,
                        "history"
                    )

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}",
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

                    hsh = get_stat(
                        hs,
                        "Shots on Goal"
                    )

                    ash = get_stat(
                        as_,
                        "Shots on Goal"
                    )

                    # =================================================
                    # HISTORY
                    # =================================================
                    if fixture_name not in history:
                        history[fixture_name] = []

                    history[fixture_name].append({
                        "minute": minute,
                        "ha": ha,
                        "aa": aa,
                        "hsh": hsh,
                        "ash": ash
                    })

                    # =================================================
                    # LAST 15 MINUTES
                    # =================================================
                    history[fixture_name] = [
                        x for x in history[fixture_name]
                        if minute - x["minute"] <= 15
                    ]

                    hist = history[fixture_name]

                    # =================================================
                    # OVER 1.5 GOALS
                    # BOTH TEAMS ATTACKING
                    # =================================================
                    if can_send_signal(
                        fixture_id,
                        "OVER15"
                    ):

                        active_minutes = 0

                        for h in hist:

                            if (
                                h["hsh"] >= 2
                                and h["ash"] >= 2
                                and h["ha"] >= 15
                                and h["aa"] >= 15
                            ):
                                active_minutes += 1

                        if (
                            len(hist) >= 12
                            and active_minutes >= 12
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

                            save_signal(
                                fixture_id,
                                "OVER15"
                            )

                    # =================================================
                    # NEXT GOAL HOME
                    # ONLY HOME PRESSURE
                    # =================================================
                    if can_send_signal(
                        fixture_id,
                        "NEXTHOME"
                    ):

                        pressure_minutes = 0

                        for h in hist:

                            if (
                                h["ha"] > h["aa"]
                                and h["hsh"] >= 2
                            ):
                                pressure_minutes += 1

                        if (
                            len(hist) >= 12
                            and pressure_minutes >= 12
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

                            save_signal(
                                fixture_id,
                                "NEXTHOME"
                            )

                    # =================================================
                    # NEXT GOAL AWAY
                    # ONLY AWAY PRESSURE
                    # =================================================
                    if can_send_signal(
                        fixture_id,
                        "NEXTAWAY"
                    ):

                        pressure_minutes = 0

                        for h in hist:

                            if (
                                h["aa"] > h["ha"]
                                and h["ash"] >= 2
                            ):
                                pressure_minutes += 1

                        if (
                            len(hist) >= 12
                            and pressure_minutes >= 12
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

                            save_signal(
                                fixture_id,
                                "NEXTAWAY"
                            )

                except Exception as e:

                    print("MATCH ERROR:", e)

        except Exception as e:

            print("LIVE ERROR:", e)

        # =================================================
        # CLEAN HISTORY
        # =================================================
        if len(history) > 500:
            history.clear()

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# THREADS
# =========================================================
def start_live_loop():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        live_loop()
    )

def start_prematch_loop():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        prematch_loop()
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
        CommandHandler(
            "today",
            today
        )
    )

    dp.add_handler(
        CommandHandler(
            "night",
            night
        )
    )

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
