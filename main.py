# =========================================================
# PRACTICAL LIVE AI SYSTEM
# BALANCED VERSION
# =========================================================

import os
os.system("pip install requests python-telegram-bot==13.15")

import requests
import time
import sqlite3
import threading
import asyncio
import logging

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

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
# BAD COUNTRIES
# =========================================================

BAD_COUNTRIES = [

    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia"
]

# =========================================================
# SIGNAL CACHE
# =========================================================

sent = {}

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
# DUPLICATE PROTECTION
# =========================================================

def can_send(fixture_id, cooldown=5400):

    now = time.time()

    if fixture_id in sent:

        if now - sent[fixture_id] < cooldown:
            return False

    return True

def save_sent(fixture_id):

    sent[fixture_id] = time.time()

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
        params=params,
        timeout=20
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
        params=params,
        timeout=20
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

    xg += shots_on * 0.28
    xg += total_shots * 0.05
    xg += dangerous_attacks * 0.015

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
    # POSSESSION
    # =====================================================

    if possession >= 55:
        pressure += 8

    if possession >= 62:
        pressure += 8

    # =====================================================
    # SHOTS ON TARGET
    # =====================================================

    if shots_on >= 3:
        pressure += 15

    if shots_on >= 5:
        pressure += 12

    # =====================================================
    # TOTAL SHOTS
    # =====================================================

    if total_shots >= 7:
        pressure += 10

    if total_shots >= 11:
        pressure += 10

    # =====================================================
    # CORNERS
    # =====================================================

    if corners >= 3:
        pressure += 6

    if corners >= 6:
        pressure += 6

    # =====================================================
    # DANGEROUS ATTACKS
    # =====================================================

    if attacks >= 16:
        pressure += 14

    if attacks >= 26:
        pressure += 14

    # =====================================================
    # xG BOOST
    # =====================================================

    xg = estimate_xg(
        shots_on,
        total_shots,
        attacks
    )

    if xg >= 1.1:
        pressure += 10

    if xg >= 1.8:
        pressure += 10

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
# LIVE ANALYSIS
# =========================================================

def analyze_match(match):

    fixture_id = match["fixture"]["id"]

    if not can_send(fixture_id):
        return

    league = match["league"]["name"]

    if blocked_league(league):
        return

    country = match["league"]["country"]

    if country in BAD_COUNTRIES:
        return

    minute = match["fixture"]["status"]["elapsed"]

    if minute is None:
        return

    # =====================================================
    # MINUTES
    # =====================================================

    if minute < 30 or minute > 75:
        return

    home_goals = match["goals"]["home"]
    away_goals = match["goals"]["away"]

    total_goals = home_goals + away_goals

    if total_goals >= 5:
        return

    # =====================================================
    # STATS
    # =====================================================

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
    # FILTERS
    # =====================================================

    minimum_pressure = 52

    if minute >= 60:
        minimum_pressure = 56

    if best_pressure < minimum_pressure:
        return

    if dominance < 8:
        return

    minimum_xg = 1.0

    if best_xg < minimum_xg:
        return

    # =====================================================
    # SHOTS FILTER
    # =====================================================

    home_shots = extract(
        home,
        "Shots on Goal"
    )

    away_shots = extract(
        away,
        "Shots on Goal"
    )

    if max(home_shots, away_shots) < 4:
        return

    # =====================================================
    # MARKET
    # =====================================================

    if home_pressure > away_pressure:

        market = (
            f"NEXT GOAL HOME "
            f"({match['teams']['home']['name']})"
        )

    else:

        market = (
            f"NEXT GOAL AWAY "
            f"({match['teams']['away']['name']})"
        )

    # =====================================================
    # CONFIDENCE
    # =====================================================

    confidence = min(
        best_pressure,
        90
    )

    if minute >= 60:
        confidence += 2

    if minute >= 70:
        confidence += 2

    confidence = min(
        confidence,
        95
    )

    estimated_odds = 1.80

    edge = value_edge(
        confidence,
        estimated_odds
    )

    if edge < 6:
        return

    # =====================================================
    # MATCH INFO
    # =====================================================

    home_team = match["teams"]["home"]["name"]

    away_team = match["teams"]["away"]["name"]

    score = f"{home_goals}-{away_goals}"

    match_name = (
        f"{home_team} vs {away_team}"
    )

    # =====================================================
    # MESSAGE
    # =====================================================

    message = f"""
🔥 PRACTICAL LIVE AI SIGNAL

🌍 Country:
{country}

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

    save_sent(fixture_id)

# =========================================================
# LIVE LOOP
# =========================================================

async def live_loop():

    while True:

        try:

            matches = get_live_matches()

            print(
                f"[{datetime.now()}] "
                f"Live matches: {len(matches)}"
            )

            for match in matches:

                try:

                    analyze_match(match)

                except Exception as e:

                    print(
                        "Match Error:",
                        e
                    )

        except Exception as e:

            print(
                "LIVE ERROR:",
                e
            )

        await asyncio.sleep(45)

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

    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    while True:
        time.sleep(60)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
