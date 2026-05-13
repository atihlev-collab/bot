# =========================================================
# PRACTICAL LIVE AI + TODAY/NIGHT SYSTEM
# RELAXED WORKING VERSION
# =========================================================

import os
os.system("pip install requests python-telegram-bot==13.15")

import requests
import time
import sqlite3
import random
import threading
import asyncio
import logging

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

BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

bot = Bot(token=BOT_TOKEN)

logging.basicConfig(level=logging.WARNING)

# =========================================================
# BLOCKED LEAGUES
# =========================================================

BLOCKED_WORDS = [

    "women",
    "feminine",
    "female",

    "youth",
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u23",

    "reserve",
    "reserves",

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
# SIGNAL CACHE
# =========================================================

sent = set()

# =========================================================
# DATABASE
# =========================================================

def init_database():

    conn = sqlite3.connect(
        "practical_live_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        fixture_id INTEGER,
        match_name TEXT,
        market TEXT,
        pressure INTEGER,
        confidence INTEGER,
        edge_value REAL,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    try:

        bot.send_message(
            chat_id=CHAT_ID,
            text=message
        )

    except Exception as e:

        print("Telegram Error:", e)

# =========================================================
# BLOCK CHECK
# =========================================================

def blocked_league(league_name):

    text = league_name.lower()

    for word in BLOCKED_WORDS:

        if word in text:
            return True

    return False

# =========================================================
# LIVE MATCHES
# =========================================================

def get_live_matches():

    url = f"{BASE_URL}/fixtures"

    params = {
        "live": "all"
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params
    )

    data = response.json()

    return data.get(
        "response",
        []
    )

# =========================================================
# MATCH STATS
# =========================================================

def get_statistics(fixture_id):

    url = f"{BASE_URL}/fixtures/statistics"

    params = {
        "fixture": fixture_id
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params
    )

    return response.json().get(
        "response",
        []
    )

# =========================================================
# EXTRACT STATS
# =========================================================

def extract(team, stat_name):

    for stat in team["statistics"]:

        if stat["type"] == stat_name:

            value = stat["value"]

            if value is None:
                return 0

            if isinstance(value, str):

                value = value.replace("%", "")

                try:
                    value = int(value)
                except:
                    return 0

            return value

    return 0

# =========================================================
# ESTIMATED xG
# =========================================================

def estimate_xg(

    shots_on,
    total_shots,
    dangerous_attacks

):

    xg = 0

    xg += shots_on * 0.25

    xg += total_shots * 0.04

    xg += dangerous_attacks * 0.012

    return round(xg, 2)

# =========================================================
# PRESSURE ENGINE
# =========================================================

def calculate_pressure(team):

    pressure = 0

    possession = extract(
        team,
        "Ball Possession"
    )

    shots_on = extract(
        team,
        "Shots on Goal"
    )

    total_shots = extract(
        team,
        "Total Shots"
    )

    corners = extract(
        team,
        "Corner Kicks"
    )

    attacks = extract(
        team,
        "Dangerous Attacks"
    )

    # =====================================================
    # RELAXED MOMENTUM
    # =====================================================

    if possession >= 52:
        pressure += 6

    if possession >= 58:
        pressure += 6

    if shots_on >= 2:
        pressure += 12

    if shots_on >= 4:
        pressure += 10

    if total_shots >= 6:
        pressure += 8

    if total_shots >= 10:
        pressure += 8

    if corners >= 2:
        pressure += 5

    if corners >= 5:
        pressure += 5

    if attacks >= 14:
        pressure += 14

    if attacks >= 22:
        pressure += 14

    # =====================================================
    # xG BOOST
    # =====================================================

    xg = estimate_xg(
        shots_on,
        total_shots,
        attacks
    )

    if xg >= 1.0:
        pressure += 8

    if xg >= 1.8:
        pressure += 8

    return pressure, xg

# =========================================================
# VALUE ENGINE
# =========================================================

def value_edge(confidence, odds):

    probability = 100 / odds

    edge = confidence - probability

    return round(edge, 2)

# =========================================================
# SAVE SIGNAL
# =========================================================

def save_signal(

    fixture_id,
    match_name,
    market,
    pressure,
    confidence,
    edge

):

    conn = sqlite3.connect(
        "practical_live_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO signals (

        fixture_id,
        match_name,
        market,
        pressure,
        confidence,
        edge_value,
        created_at

    ) VALUES (?,?,?,?,?,?,?)
    """, (

        fixture_id,
        match_name,
        market,
        pressure,
        confidence,
        edge,
        str(datetime.now())

    ))

    conn.commit()
    conn.close()

# =========================================================
# PICK LOGIC
# =========================================================

def generate_pick(country, league, home, away):

    pick = "OVER 2.5 GOALS"
    odd = "1.85"
    score = 5

    if any(
        x.lower() in country.lower()
        for x in LOW_SCORING_COUNTRIES
    ):

        pick = "UNDER 2.5 GOALS"
        odd = "1.70"
        score += 7

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
            f"{BASE_URL}/fixtures?next=60",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        valid_matches = []

        for m in matches:

            try:

                league = m["league"]["name"]

                if blocked_league(league):
                    continue

                country = m["league"]["country"]

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

        valid_matches = sorted(
            valid_matches,
            key=lambda x: x["score"],
            reverse=True
        )

        selected = valid_matches[:3]

        if not selected:

            update.message.reply_text(
                "❌ Няма мачове."
            )
            return

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

# =========================================================
# NIGHT COMMAND
# =========================================================

def night(update: Update, context: CallbackContext):

    try:

        r = requests.get(
            f"{BASE_URL}/fixtures?next=100",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        valid_matches = []

        for m in matches:

            try:

                league = m["league"]["name"]

                if blocked_league(league):
                    continue

                country = m["league"]["country"]

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

        valid_matches = sorted(
            valid_matches,
            key=lambda x: x["score"],
            reverse=True
        )

        selected = valid_matches[:3]

        if not selected:

            update.message.reply_text(
                "❌ Няма night мачове."
            )
            return

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

# =========================================================
# LIVE ANALYSIS
# =========================================================

def analyze_match(match):

    fixture_id = match["fixture"]["id"]

    if fixture_id in sent:
        return

    league = match["league"]["name"]

    if blocked_league(league):
        return

    minute = match["fixture"]["status"]["elapsed"]

    if minute is None:
        return

    # ПО-ШИРОК RANGE
    if minute < 30 or minute > 75:
        return

    home_goals = match["goals"]["home"]
    away_goals = match["goals"]["away"]

    if home_goals + away_goals >= 6:
        return

    stats = get_statistics(
        fixture_id
    )

    if len(stats) < 2:
        return

    home = stats[0]
    away = stats[1]

    home_pressure, home_xg = (
        calculate_pressure(home)
    )

    away_pressure, away_xg = (
        calculate_pressure(away)
    )

    best_pressure = max(
        home_pressure,
        away_pressure
    )

    best_xg = max(
        home_xg,
        away_xg
    )

    dominance = abs(
        home_pressure - away_pressure
    )

    # =====================================================
    # RELAXED FILTERS
    # =====================================================

    minimum_pressure = 48

    if minute >= 60:
        minimum_pressure = 52

    if minute >= 72:
        minimum_pressure = 46

    if best_pressure < minimum_pressure:
        return

    if dominance < 4:
        return

    minimum_xg = 0.8

    if minute >= 70:
        minimum_xg = 0.7

    if best_xg < minimum_xg:
        return

    confidence = min(
        best_pressure,
        92
    )

    if minute >= 70:
        confidence += 2

    if minute >= 75:
        confidence += 2

    confidence = min(
        confidence,
        95
    )

    estimated_odds = 1.75

    edge = value_edge(
        confidence,
        estimated_odds
    )

    if edge < 3:
        return

    # =====================================================
    # MARKET ENGINE
    # =====================================================

    market = "Over 0.5 Goal LIVE"

    if minute >= 55 and confidence >= 65:
        market = "Next Goal"

    if minute >= 68 and confidence >= 72:
        market = "Over 1.5 LIVE"

    home_team = match["teams"]["home"]["name"]

    away_team = match["teams"]["away"]["name"]

    score = f"{home_goals}-{away_goals}"

    match_name = (
        f"{home_team} vs {away_team}"
    )

    message = f"""
🔥 PRACTICAL LIVE AI SIGNAL

⚽ Match:
{match_name}

🏆 League:
{league}

⏱ Minute:
{minute}

📊 Score:
{score}

🔥 Pressure:
{best_pressure}/100

⚔ Dominance:
{dominance}

📈 Estimated xG:
{best_xg}

💎 Value Edge:
+{edge}%

📈 Market:
{market}

✅ Confidence:
{confidence}%
"""

    print(message)

    send_telegram(message)

    save_signal(

        fixture_id,
        match_name,
        market,
        best_pressure,
        confidence,
        edge
    )

    sent.add(fixture_id)

# =========================================================
# LIVE LOOP
# =========================================================

async def live_loop():

    while True:

        try:

            matches = get_live_matches()

            for match in matches:

                try:

                    analyze_match(match)

                except Exception as e:

                    print("Match Error:", e)

        except Exception as e:

            print("LIVE ERROR:", e)

        await asyncio.sleep(30)

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

    init_database()

    print("🚀 PRACTICAL LIVE AI SYSTEM STARTED")

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

    updater.start_polling(
        drop_pending_updates=True
    )

    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    updater.idle()

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
