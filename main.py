# =========================================================
# PRACTICAL LIVE AI SYSTEM
# SMART RESET + PREMATCH + BEST VERSION
# =========================================================

import os
os.system("pip install requests python-telegram-bot==13.15")

import requests
import time
import sqlite3
import threading
import asyncio
import logging

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.ext import (
    Updater,
    CommandHandler
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
# SCORE CACHE
# =========================================================

last_scores = {}

# =========================================================
# PREMATCH CACHE
# =========================================================

prematch_sent = {}

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

def can_send(fixture_id, cooldown=2400):

    now = time.time()

    if fixture_id in sent:

        if now - sent[fixture_id] < cooldown:
            return False

    return True

def save_sent(fixture_id):

    sent[fixture_id] = time.time()

# =========================================================
# PREMATCH DUPLICATE
# =========================================================

def can_send_prematch(key, cooldown=21600):

    now = time.time()

    if key in prematch_sent:

        if now - prematch_sent[key] < cooldown:
            return False

    return True

def save_prematch(key):

    prematch_sent[key] = time.time()

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
# UPCOMING MATCHES
# =========================================================

def get_upcoming_matches():

    matches = []

    now = datetime.now(TZ)

    for i in range(2):

        date = (
            now + timedelta(days=i)
        ).strftime("%Y-%m-%d")

        try:

            r = requests.get(
                f"{BASE_URL}/fixtures?date={date}",
                headers=HEADERS,
                timeout=20
            ).json()

            matches.extend(
                r.get("response", [])
            )

        except:
            pass

    return matches

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

    if possession >= 55:
        pressure += 8

    if possession >= 62:
        pressure += 8

    if shots_on >= 3:
        pressure += 15

    if shots_on >= 5:
        pressure += 12

    if total_shots >= 7:
        pressure += 10

    if total_shots >= 11:
        pressure += 10

    if corners >= 3:
        pressure += 6

    if corners >= 6:
        pressure += 6

    if attacks >= 16:
        pressure += 14

    if attacks >= 26:
        pressure += 14

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
# PREMATCH SCORE ENGINE
# =========================================================

def calculate_match_score(country, league, home, away):

    score = 0
    market = "OVER 2.5 GOALS"
    odd = "1.80"

    OVER_COUNTRIES = [
        "Netherlands",
        "Norway",
        "Sweden",
        "Germany",
        "Denmark",
        "Brazil",
        "Argentina",
        "USA"
    ]

    UNDER_COUNTRIES = [
        "Italy",
        "Romania",
        "Bulgaria",
        "Croatia"
    ]

   BIG_TEAMS = [

    "Manchester City",
    "Manchester United",

    "Liverpool",
    "Arsenal",
    "Chelsea",

    "Barcelona",
    "Real Madrid",

    "Bayern Munich",

    "Paris Saint Germain",
    "PSG",

    "Inter Milan",
    "AC Milan",

    "Juventus"
]

    if any(
        x.lower() in country.lower()
        for x in OVER_COUNTRIES
    ):

        score += 10
        market = "OVER 2.5 GOALS"
        odd = "1.75"

    if any(
        x.lower() in country.lower()
        for x in UNDER_COUNTRIES
    ):

        score += 8
        market = "UNDER 2.5 GOALS"
        odd = "1.70"

 if any(
    x.lower() == home.lower()
    for x in BIG_TEAMS
):

        score += 10
        market = "1"
        odd = "1.60"

  if any(
    x.lower() == away.lower()
    for x in BIG_TEAMS
):

        score += 8
        market = "2"
        odd = "1.75"

    return score, market, odd

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

    league = match["league"]["name"]

    if blocked_league(league):
        return

    country = match["league"]["country"]

    if country in BAD_COUNTRIES:
        return

    minute = match["fixture"]["status"]["elapsed"]

    if minute is None:
        return

    if minute < 30 or minute > 75:
        return

    home_goals = match["goals"]["home"]
    away_goals = match["goals"]["away"]

    total_goals = home_goals + away_goals

    if total_goals >= 6:
        return

    score = f"{home_goals}-{away_goals}"

    # =====================================================
    # RESET AFTER GOAL
    # =====================================================

    if fixture_id not in last_scores:

        last_scores[fixture_id] = score

    else:

        if last_scores[fixture_id] != score:

            if fixture_id in sent:
                del sent[fixture_id]

            last_scores[fixture_id] = score

    # =====================================================
    # DUPLICATE CHECK
    # =====================================================

    if not can_send(fixture_id):
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

    minimum_pressure = 52

    if minute >= 60:
        minimum_pressure = 56

    if best_pressure < minimum_pressure:
        return

    minimum_xg = 1.0

    if best_xg < minimum_xg:
        return

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
    # OPEN GAME MODE
    # =====================================================

    total_shots_on = (
        home_shots + away_shots
    )

    home_attacks = extract(
        home,
        "Dangerous Attacks"
    )

    away_attacks = extract(
        away,
        "Dangerous Attacks"
    )

    total_attacks = (
        home_attacks + away_attacks
    )

    # =====================================================
    # MARKET
    # =====================================================

    if (

        total_goals <= 2

        and total_shots_on >= 8

        and total_attacks >= 38

        and best_xg >= 1.8

        and minute >= 35

        and dominance < 8

    ):

        market = "OVER 1.5 LIVE"

    else:

        if dominance < 8:
            return

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

    if confidence < 70:
        return

    estimated_odds = 1.80

    edge = value_edge(
        confidence,
        estimated_odds
    )

    if edge < 6:
        return

    home_team = match["teams"]["home"]["name"]

    away_team = match["teams"]["away"]["name"]

    match_name = (
        f"{home_team} vs {away_team}"
    )
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
# PREMATCH AI
# =========================================================

async def prematch_loop():

    while True:

        try:

            matches = get_upcoming_matches()

            for m in matches:

                try:

                    league = m["league"]["name"]

                    if blocked_league(league):
                        continue

                    country = m["league"]["country"]

                    if country in BAD_COUNTRIES:
                        continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    date = datetime.fromisoformat(
                        m["fixture"]["date"].replace(
                            "Z","+00:00"
                        )
                    ).astimezone(TZ)

                    diff = (
                        date - datetime.now(TZ)
                    ).total_seconds()

                    if diff < 0 or diff > 28800:
                        continue

                    score, market, odd = (
                        calculate_match_score(
                            country,
                            league,
                            home,
                            away
                        )
                    )

                    confidence = 65 + score

                    if confidence < 78:
                        continue

                    key = f"{home}_{away}"

                    if not can_send_prematch(key):
                        continue

                    msg = f"""
🔥 PRE-MATCH AI SIGNAL

🌍 {country}
🏆 {league}

⚽ {home} vs {away}

⏰ {date.strftime("%d.%m %H:%M")}

🎯 {market}

💰 Odd:
{odd}

✅ Confidence:
{min(confidence,92)}%
"""

                    print(msg)

                    send_telegram(msg)

                    save_prematch(key)

                except Exception as e:

                    print(
                        "PREMATCH MATCH ERROR:",
                        e
                    )

        except Exception as e:

            print(
                "PREMATCH ERROR:",
                e
            )

        await asyncio.sleep(1200)

# =========================================================
# BEST COMMAND
# =========================================================

def best(update, context):

    try:

        matches = get_upcoming_matches()

        picks = []

        for m in matches:

            try:

                league = m["league"]["name"]

                if blocked_league(league):
                    continue

                country = m["league"]["country"]

                if country in BAD_COUNTRIES:
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                score, market, odd = (
                    calculate_match_score(
                        country,
                        league,
                        home,
                        away
                    )
                )

                picks.append({

                    "score": score,
                    "home": home,
                    "away": away,
                    "league": league,
                    "market": market,
                    "odd": float(odd)

                })

            except:
                pass

        picks = sorted(
            picks,
            key=lambda x: x["score"],
            reverse=True
        )

        selected = []

        total = 1

        for p in picks:

            if len(selected) >= 4:
                break

            if total < 5:

                total *= p["odd"]

                selected.append(p)

        if not selected:

            update.message.reply_text(
                "❌ Няма best selections."
            )

            return

        msg = "🔥 BEST AI TICKET\n"

        for i,p in enumerate(selected,1):

            msg += f"""

{i})

🏟 {p['home']} vs {p['away']}
🏆 {p['league']}

🎯 {p['market']}
💰 {p['odd']}
"""

        msg += f"""

💎 TOTAL:
{round(total,2)}
"""

        update.message.reply_text(msg)

    except Exception as e:

        print("BEST ERROR:", e)

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

    updater = Updater(
        token=BOT_TOKEN,
        use_context=True
    )

    dp = updater.dispatcher

    dp.add_handler(
        CommandHandler(
            "best",
            best
        )
    )

    updater.start_polling(
        drop_pending_updates=True
    )

    # LIVE THREAD
    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    # PREMATCH THREAD
    prematch_thread = threading.Thread(
        target=lambda:
        asyncio.run(prematch_loop()),
        daemon=True
    )

    prematch_thread.start()

    updater.idle()

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
