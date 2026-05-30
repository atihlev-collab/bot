# =========================================================
# PRACTICAL LIVE AI SYSTEM
# SMART RESET + PREMATCH AI VERSION
# =========================================================


import requests
import time
import sqlite3
import threading
import asyncio
import logging

import numpy as np

from scipy.stats import poisson
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot
import asyncio

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
]

# =========================================================
# BAD COUNTRIES
# =========================================================

BAD_COUNTRIES = [

    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia",

    "Russia",
    "Belarus",
    "Israel"
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
# BET365 VALUE CACHE
# =========================================================

odds_cache = {}

opening_odds = {}

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

        print(
            "SENDING TO TELEGRAM..."
        )

        asyncio.create_task(
            bot.send_message(
                chat_id=CHAT_ID,
                text=message
            )
        )

        print(
            "TELEGRAM SENT"
        )

    except Exception as e:

        print(
            "TELEGRAM ERROR:"
        )

        print(
            str(e)
        )        

#========================================================
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

    for i in range(1):

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
# BET365 VALUE ENGINE
# =========================================================

def odds_drop_signal(

    home,
    away,
    odd

):

    key = f"{home}_{away}"

    try:

        odd = float(odd)

    except:

        return 0, 0

    now = time.time()

    # =====================================================
    # FIRST TIME
    # =====================================================

    if key not in odds_cache:

        odds_cache[key] = {

            "odd": odd,
            "time": now

        }

        return 0, 0

    old_odd = odds_cache[key]["odd"]

    old_time = odds_cache[key]["time"]

    # =====================================================
    # DROP
    # =====================================================

    drop = round(

        old_odd - odd,

        2

    )

    # =====================================================
    # VELOCITY
    # =====================================================

    minutes = max(

        (now - old_time) / 60,

        1

    )

    velocity = round(

        drop / minutes,

        3

    )

    # =====================================================
    # UPDATE CACHE
    # =====================================================

    odds_cache[key] = {

        "odd": odd,
        "time": now

    }

    return drop, velocity


# =========================================================
# REAL ODDS API
# =========================================================

def get_match_odds(fixture_id):

    try:

        url = f"{BASE_URL}/odds"

        params = {

            "fixture": fixture_id

        }

        response = requests.get(

            url,
            headers=HEADERS,
            params=params,
            timeout=20

        ).json()

        data = response.get(
            "response",
            []
        )

        if not data:
            return None

        best_market = None

        sharp_odd = None
        soft_odd = None

        for item in data:

            for bookmaker in item.get(
                "bookmakers",
                []
            ):

                name = bookmaker.get(
                    "name",
                    ""
                )

                for bet in bookmaker.get(
                    "bets",
                    []
                ):

                    # =================================================
                    # MATCH WINNER
                    # =================================================

                    if bet["name"] == "Match Winner":

                        values = bet.get(
                            "values",
                            []
                        )

                        for v in values:

                            try:

                                odd = float(
                                    v["odd"]
                                )

                            except:
                                continue

                            value_name = v["value"]

                            # HOME
                            if value_name == "Home":

                                if name in [

                                    "Pinnacle",
                                    "Bet365"

                                ]:

                                    sharp_odd = odd
                                    best_market = (
                                        "🏠 HOME WIN"
                                    )

                                elif name in [

                                    "Betano",
                                    "1xBet"

                                ]:

                                    soft_odd = odd

                            # AWAY
                            elif value_name == "Away":

                                if name in [

                                    "Pinnacle",
                                    "Bet365"

                                ]:

                                    if (
                                        sharp_odd is None
                                        or odd < sharp_odd
                                    ):

                                        sharp_odd = odd
                                        best_market = (
                                            "✈ AWAY WIN"
                                        )

                                elif name in [

                                    "Betano",
                                    "1xBet"

                                ]:

                                    soft_odd = odd

                    # =================================================
                    # OVER 2.5
                    # =================================================

                    elif bet["name"] == "Goals Over/Under":

                        values = bet.get(
                            "values",
                            []
                        )

                        for v in values:

                            if (

                                v["value"]
                                ==
                                "Over 2.5"

                            ):

                                try:

                                    odd = float(
                                        v["odd"]
                                    )

                                except:
                                    continue

                                if name in [

                                    "Pinnacle",
                                    "Bet365"

                                ]:

                                    if (
                                        sharp_odd is None
                                        or odd < sharp_odd
                                    ):

                                        sharp_odd = odd
                                        best_market = (
                                            "⚽ OVER 2.5 GOALS"
                                        )

                                elif name in [

                                    "Betano",
                                    "1xBet"

                                ]:

                                    soft_odd = odd

        if sharp_odd is None:
            return None

        return {

            "sharp_odd": sharp_odd,
            "soft_odd": soft_odd,
            "market": best_market

        }

    except:

        return None
 # =========================================================
# POISSON ENGINE
# =========================================================

def poisson_probability(

    home_attack,
    away_attack

):

    try:

        home_lambda = round(
            home_attack,
            2
        )

        away_lambda = round(
            away_attack,
            2
        )

        max_goals = 6

        home_probs = [

            poisson.pmf(
                i,
                home_lambda
            )

            for i in range(
                max_goals
            )

        ]

        away_probs = [

            poisson.pmf(
                i,
                away_lambda
            )

            for i in range(
                max_goals
            )

        ]

        matrix = np.outer(

            home_probs,
            away_probs

        )

        over25 = 0

        for h in range(max_goals):

            for a in range(max_goals):

                if h + a >= 3:

                    over25 += matrix[h][a]

        btts = 0

        for h in range(1, max_goals):

            for a in range(1, max_goals):

                btts += matrix[h][a]

        return {

            "over25": round(
                over25 * 100,
                2
            ),

            "btts": round(
                btts * 100,
                2
            )

        }

    except:

        return {

            "over25": 0,
            "btts": 0

        }     
   # =========================================================
# ADVANCED SHARP MARKET ENGINE
# CLEAN PROBABILITY + CLV + REGIME
# =========================================================

sharp_history = {}

closing_history = {}

# =========================================================
# CLEAN SHARP PROBABILITY
# =========================================================

def clean_probability(odd):

    try:

        raw_probability = (

            1 / float(odd)

        ) * 100

        # bookmaker margin removal
        clean_prob = round(

            raw_probability * 0.97,

            2

        )

        return clean_prob

    except:

        return 0

# =========================================================
# OPENING ODDS HISTORY
# =========================================================

def track_opening_odds(

    match_key,
    odd

):

    try:

        odd = float(odd)

    except:

        return

    if match_key not in opening_odds:

        opening_odds[match_key] = {

            "opening": odd,
            "time": time.time()

        }

# =========================================================
# CLOSING LINE VALUE
# =========================================================

def calculate_clv(

    match_key,
    current_odd

):

    try:

        current_odd = float(current_odd)

    except:

        return 0

    if match_key not in opening_odds:

        return 0

    opening = opening_odds[match_key][
        "opening"
    ]

    clv = round(

        (
            opening
            -
            current_odd
        )

        / opening * 100,

        2

    )

    return clv

# =========================================================
# MARKET REGIME DETECTION
# =========================================================

def market_regime(

    match_key,
    odd

):

    try:

        odd = float(odd)

    except:

        return "UNKNOWN"

    if match_key not in sharp_history:

        sharp_history[match_key] = []

    sharp_history[match_key].append(
        odd
    )

    # keep only last 6
    sharp_history[match_key] = (

        sharp_history[match_key][-6:]

    )

    history = sharp_history[match_key]

    if len(history) < 4:

        return "NORMAL"

    drops = 0
    rises = 0

    for i in range(

        1,
        len(history)

    ):

        if history[i] < history[i-1]:

            drops += 1

        elif history[i] > history[i-1]:

            rises += 1

    # =====================================================
    # STABLE SHARP
    # =====================================================

    if drops >= 4 and rises <= 1:

        return "STABLE_SHARP"

    # =====================================================
    # CHAOTIC
    # =====================================================

    if rises >= 2 and drops >= 2:

        return "CHAOTIC"

    return "NORMAL"

# =========================================================
# AGGRESSIVE SHARP ALERT
# =========================================================

def aggressive_sharp_move(

    drop,
    velocity

):

    try:

        drop = float(drop)
        velocity = float(velocity)

    except:

        return False

    if (

        drop >= 0.25

        and

        velocity >= 0.03

    ):

        return True

    return False     
 
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

# =========================================================
# PREMATCH VALUE ENGINE
# =========================================================

def calculate_match_score(
    country,
    league,
    home,
    away
):

    score = 0

    market = "⚽ OVER 2.5 GOALS"
    odd = "1.80"

    league_text = (
        league.lower()
    )

    match_text = (
        f"{home} {away}".lower()
    )

    # =====================================================
    # LEAGUE QUALITY
    # =====================================================

    if country in [

       "England",
       "Spain",
       "Germany",
       "Italy",
       "France",
       "Netherlands",
       "Portugal",

       "Brazil",
       "Argentina",
       "Norway",
       "Sweden",
       "Denmark",
       "Japan",
       "USA"

   ]:

        score += 8

    # =====================================================
    # INTERNATIONAL TOURNAMENTS
    # =====================================================

    if any(

        x in league_text

        for x in [

            "champions",
            "europa",
            "conference",
            "libertadores",
            "world cup",
            "euro",
            "copa america",
            "nations league"

        ]

    ):

        score += 8

    # =====================================================
    # SMART LEAGUE FILTER
    # =====================================================

    bad_words = [

        "women",
        "u19",
        "u21",
        "u23",
        "reserve",
        "regional"

    ]

    if any(

        x in league_text

        for x in bad_words

    ):

        score -= 100

    # =====================================================
    # FRIENDLY FILTER
    # =====================================================

    national_teams = [

        "Brazil",
        "Argentina",
        "Germany",
        "France",
        "Spain",
        "Portugal",
        "England",
        "Italy",
        "Netherlands",
        "Belgium",
        "Croatia",
        "Uruguay",
        "Mexico",
        "USA",
        "Japan"

    ]

    # block random club friendlies
    if (

        "friendly" in league_text

        and not any(

            x.lower() in match_text

            for x in national_teams

        )

    ):

        score -= 25

    # =====================================================
    # SUMMER / ACTIVE LEAGUES BONUS
    # =====================================================

    active_countries = [

        "Norway",
        "Sweden",
        "Denmark",
        "Finland",
        "Iceland",

        "Brazil",
        "Argentina",
        "Chile",
        "Colombia",
        "Uruguay",
        "Paraguay",

        "USA",
        "Mexico",

        "Japan",
        "South Korea",
        "Australia"

    ]

    if country in active_countries:

        score += 4

       # =====================================================
    # MARKET FIT
    # =====================================================

    # GOAL LEAGUES
    if country in [

        "Netherlands",
        "Germany",
        "Norway",
        "Sweden"

    ]:

        market = "⚽ OVER 2.5 GOALS"

        odd = "1.80"

        score += 4

    # UNDER LEAGUES
    elif country in [

        "Italy",
        "Romania",
        "Bulgaria"

    ]:

        market = "📉 UNDER 2.5 GOALS"

        odd = "1.75"

        score += 4

    # DENMARK / BELGIUM
    elif country in [

        "Denmark",
        "Belgium"

    ]:

        market = "⚽ OVER 2.5 GOALS"

        odd = "1.80"

        score += 4

    # =====================================================
    # BIG TEAMS
    # =====================================================

    big_teams = [

        "Manchester City",
        "Liverpool",
        "Arsenal",
        "Barcelona",
        "Real Madrid",
        "Bayern",
        "PSG",
        "Ajax",
        "PSV",
        "Benfica",
        "Flamengo"

    ]

    if home in big_teams:

        score += 2

    if away in big_teams:

        score += 2

    # =====================================================
    # VALUE STYLE ODDS
    # =====================================================

    try:

        odd_value = float(odd)

        # sweet spot
        if 1.70 <= odd_value <= 2.05:

            score += 6

        elif odd_value > 2.40:

            score -= 8

    except:

        pass

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
# SELF LEARNING AI ENGINE
# RESULT TRACKING + AUTO LEARNING
# =========================================================

learning_stats = {}

# =========================================================
# RESULT DATABASE
# =========================================================

def init_learning_database():

    conn = sqlite3.connect(
        "learning_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS learning_results (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        fixture_id INTEGER,
        match_name TEXT,

        market TEXT,

        country TEXT,
        league TEXT,

        minute INTEGER,

        confidence REAL,
        pressure REAL,
        xg REAL,

        odd REAL,

        edge REAL,

        odds_drop REAL,
        velocity REAL,

        result TEXT,

        created_at TEXT

    )

    """)

    conn.commit()

    conn.close()

# =========================================================
# SAVE LEARNING SIGNAL
# =========================================================

def save_learning_signal(

    fixture_id,
    match_name,

    market,

    country,
    league,

    minute,

    confidence,
    pressure,
    xg,

    odd,

    edge,

    drop,
    velocity

):

    conn = sqlite3.connect(
        "learning_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    INSERT INTO learning_results (

        fixture_id,
        match_name,

        market,

        country,
        league,

        minute,

        confidence,
        pressure,
        xg,

        odd,

        edge,

        odds_drop,
        velocity,

        result,

        created_at

    )

    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)

    """, (

        fixture_id,
        match_name,

        market,

        country,
        league,

        minute,

        confidence,
        pressure,
        xg,

        odd,

        edge,

        drop,
        velocity,

        "PENDING",

        str(datetime.now())

    ))

    conn.commit()

    conn.close()

# =========================================================
# UPDATE RESULT
# =========================================================

def update_signal_result(

    fixture_id,
    result

):

    conn = sqlite3.connect(
        "learning_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    UPDATE learning_results

    SET result=?

    WHERE fixture_id=?

    """, (

        result,
        fixture_id

    ))

    conn.commit()

    conn.close()

# =========================================================
# AUTO LEARNING ANALYSIS
# =========================================================

def learning_analysis():

    conn = sqlite3.connect(
        "learning_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    SELECT

        market,
        confidence,
        country,
        result

    FROM learning_results

    WHERE result != 'PENDING'

    """)

    rows = cursor.fetchall()

    conn.close()

    if not rows:

        return

    market_stats = {}

    country_stats = {}

    confidence_stats = {}

    # =====================================================
    # ANALYZE
    # =====================================================

    for row in rows:

        market = row[0]

        confidence = int(row[1])

        country = row[2]

        result = row[3]

        win = 1 if result == "WIN" else 0

        # =================================================
        # MARKET
        # =================================================

        if market not in market_stats:

            market_stats[market] = {

                "wins":0,
                "total":0

            }

        market_stats[market]["total"] += 1

        market_stats[market]["wins"] += win

        # =================================================
        # COUNTRY
        # =================================================

        if country not in country_stats:

            country_stats[country] = {

                "wins":0,
                "total":0

            }

        country_stats[country]["total"] += 1

        country_stats[country]["wins"] += win

        # =================================================
        # CONFIDENCE
        # =================================================

        bucket = (

            confidence // 5

        ) * 5

        if bucket not in confidence_stats:

            confidence_stats[bucket] = {

                "wins":0,
                "total":0

            }

        confidence_stats[bucket]["total"] += 1

        confidence_stats[bucket]["wins"] += win

    # =====================================================
    # SAVE LEARNING
    # =====================================================

    learning_stats["markets"] = market_stats

    learning_stats["countries"] = country_stats

    learning_stats["confidence"] = confidence_stats

# =========================================================
# SMART AUTO BONUS
# =========================================================

def get_learning_bonus(

    market,
    country,
    confidence

):

    bonus = 0

    # =====================================================
    # MARKET BONUS
    # =====================================================

    if (

        "markets" in learning_stats

        and

        market in learning_stats["markets"]

    ):

        data = learning_stats["markets"][market]

        if data["total"] >= 10:

            hitrate = (

                data["wins"]
                /
                data["total"]

            ) * 100

            if hitrate >= 65:

                bonus += 4

            elif hitrate <= 45:

                bonus -= 4

    # =====================================================
    # COUNTRY BONUS
    # =====================================================

    if (

        "countries" in learning_stats

        and

        country in learning_stats["countries"]

    ):

        data = learning_stats["countries"][country]

        if data["total"] >= 10:

            hitrate = (

                data["wins"]
                /
                data["total"]

            ) * 100

            if hitrate >= 62:

                bonus += 3

            elif hitrate <= 45:

                bonus -= 3

    # =====================================================
    # CONFIDENCE BONUS
    # =====================================================

    bucket = (

        int(confidence) // 5

    ) * 5

    if (

        "confidence" in learning_stats

        and

        bucket in learning_stats["confidence"]

    ):

        data = learning_stats["confidence"][bucket]

        if data["total"] >= 10:

            hitrate = (

                data["wins"]
                /
                data["total"]

            ) * 100

            if hitrate >= 70:

                bonus += 4

            elif hitrate <= 50:

                bonus -= 4

    return bonus 

# =========================================================
# LIVE STATS
# =========================================================

stats = {

    "total":0,

    "72_79":0,
    "80_84":0,
    "85_95":0,

    "corners":0,
    "btts":0,
    "next_goal":0,
    "goals":0
}

def update_live_stats(

    market,
    confidence

):

    stats["total"] += 1

    if confidence >=72 and confidence<=79:

        stats["72_79"] +=1

    elif confidence>=80 and confidence<=84:

        stats["80_84"] +=1

    elif confidence>=85:

        stats["85_95"] +=1


    if "CORNERS" in market:

        stats["corners"] +=1

    elif "BTTS" in market:

        stats["btts"] +=1

    elif "NEXT GOAL" in market:

        stats["next_goal"] +=1

    elif "GOAL" in market:

        stats["goals"] +=1


def show_stats():

    if stats["total"]==0:

        return

    msg=f"""
📊 LIVE AI STATS

🔥 Total:
{stats["total"]}

72-79:
{stats["72_79"]}

80-84:
{stats["80_84"]}

85+:
{stats["85_95"]}

📐 Corners:
{stats["corners"]}

💎 BTTS:
{stats["btts"]}

🎯 Next Goal:
{stats["next_goal"]}

⚽ Goals:
{stats["goals"]}
"""

    send_telegram(msg)
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

    if minute < 30 or minute > 85:
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

       # =====================================================
    # FILTERS
    # =====================================================

    minimum_pressure = 60

    if minute >= 60:

        minimum_pressure = 65

    if best_pressure < minimum_pressure:

        return

    # леко отпуснато
    if dominance < 12:

        return

    minimum_xg = 1.3

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

    # 5 беше прекалено
    if max(
        home_shots,
        away_shots
    ) < 4:

        return

    # ранни мачове
    if minute < 35:

        return

    if total_goals >= 5:

        return
    # =====================================================
    # MARKET
    # =====================================================

    total_corners = (
        extract(home, "Corner Kicks")
        +
        extract(away, "Corner Kicks")
    )

    total_xg = home_xg + away_xg

    market = None
    bonus_market = ""

    home_name = match["teams"]["home"]["name"]
    away_name = match["teams"]["away"]["name"]

    # =====================================================
    # OVER GOALS
    # двата отбора атакуват
    # =====================================================

    if (

        total_goals <= 3
        and total_xg >= 2.2
        and home_shots >= 3
        and away_shots >= 3

    ):

        market = f"⚽ OVER {total_goals+1}.5 GOALS"

   

       # =====================================================
    # NEXT GOAL
    # =====================================================

    elif (

        dominance >= 25

        and total_xg < 2.2
        
        and abs(home_goals - away_goals) <= 1
        
        and (

            (
                home_xg >= 1.0
                and away_xg <= 0.8
            )

            or

           (
                away_xg >= 1.0
                and home_xg <= 0.8
           )

       )

    ):

     if home_xg > away_xg:

        market = f"🎯 NEXT GOAL HOME ({home_name})"

    else:

        market = f"🎯 NEXT GOAL AWAY ({away_name})"
    # =====================================================
    # LATE GOAL
    # =====================================================

    elif (

        minute >= 35
        and best_pressure >= 68
        and abs(home_goals-away_goals) < 4

    ):

        market = "🔥 GOAL 35-85"

       # =====================================================
    # CARDS
    # =====================================================

    elif (

        minute >= 55
        and minute <= 85
        and abs(home_goals-away_goals) <= 2

    ):

        total_fouls = (
            extract(home, "Fouls")
            +
            extract(away, "Fouls")
        )

        total_cards = (
            extract(home, "Yellow Cards")
            +
            extract(away, "Yellow Cards")
        )

        if (

            total_fouls >= 14
            and total_cards >= 2

        ):

            market = "🟨 LIVE OVER CARDS"
        # =====================================================
    # CORNERS
    # =====================================================

    elif (

        minute >= 50
        and minute <= 85

    ):

        if (

            home_goals < away_goals
            and home_pressure >= 60
            and total_corners >= 5

        ):

            market = (
                f"📐 OVER {total_corners+2}.5 CORNERS"
            )

        elif (

            away_goals < home_goals
            and away_pressure >= 60
            and total_corners >= 5

        ):

            market = (
                f"📐 OVER {total_corners+2}.5 CORNERS"
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

    # LIVE само 80%+
    if confidence < 80:
        return

    # =====================================================
    # MATCH INFO
    # =====================================================

    home_team = match["teams"]["home"]["name"]

    away_team = match["teams"]["away"]["name"]

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
{home_team} vs {away_team}

🏆 League:
{league}

⏱ Minute:
{minute}

📊 Score:
{home_goals}-{away_goals}

🔥 Pressure:
{best_pressure}/100

⚔ Dominance:
{dominance}

📈 Estimated xG:
{round(best_xg,2)}

💎 Value Edge:
+{round(edge,2)}%

📈 Market:
{market}
"""

    if bonus_market != "":

        message += f"""

💎 Bonus:
{bonus_market}
"""

    message += f"""

✅ Confidence:
{confidence}%
"""

    send_telegram(
        message
    )

    update_live_stats(
        market,
        confidence
    )

    save_signal(

        fixture_id,
        match_name,
        market,
        best_pressure,
        confidence,
        edge
    )

    save_sent(
        fixture_id
    )



# =========================================================
# DAILY BET SLIP
# =========================================================

daily_ticket_sent = False

async def daily_ticket():

    global daily_ticket_sent

    now = datetime.now(TZ)

    # само в 13:00
    if now.hour != 13:

        daily_ticket_sent = False
        return

    # само веднъж
    if daily_ticket_sent:
        return

    matches = get_upcoming_matches()

    picks = []

    total_odds = 1.0

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

            # =====================================================
            # DATE / TIME
            # =====================================================

            date = datetime.fromisoformat(

                m["fixture"]["date"].replace(
                    "Z","+00:00"
                )

            ).astimezone(TZ)

            # само днешни мачове
            if date.date() != now.date():

                continue

            # миналите ги прескачаме
            if date < now:

                continue

            kickoff = date.strftime(
              "%d.%m %H:%M"
            )

            text = (
                home + " " + away
            ).lower()

            # =====================================================
            # EXTRA TRASH FILTER
            # =====================================================

            if any(

                x in text

                for x in [

                    " women",
                    " kvinn",
                    " female",
                    " ladies",

                    " w ",

                    " u19",
                    " u21",
                    " u23"

                ]

            ):

                continue

            # =====================================================
            # SCORE ENGINE
            # =====================================================

            score, market, odd = (

                calculate_match_score(
                    country,
                    league,
                    home,
                    away
                )
            )

            confidence = 58 + score

            # =====================================================
            # ONLY STRONG SIGNALS
            # =====================================================

            if confidence < 90:
                continue

            odd = float(odd)

            # =====================================================
            # VALUE ODDS RANGE
            # =====================================================

            if odd < 1.65:
                continue

            if odd > 2.05:
                continue

            # =====================================================
            # ONLY REAL MARKETS
            # =====================================================

            if market not in [

                "⚽ OVER 2.5 GOALS",
                "📉 UNDER 2.5 GOALS"

            ]:

                continue

            # =====================================================
            # SUMMER ACTIVE LEAGUES
            # =====================================================

            preferred = [

                "Norway",
                "Sweden",
                "Denmark",

                "Brazil",
                "Argentina",
                "Chile",
                "Uruguay",

                "Japan",
                "USA",
                "Finland",
                "Iceland"

            ]

            if country in preferred:

                confidence += 4

            # =====================================================
            # SAVE PICK
            # =====================================================

            picks.append(

                (
                    confidence,
                    home,
                    away,
                    market,
                    odd,
                    league,
                    country,
                    kickoff
                )

            )

        except:
            pass

    # =========================================================
    # SORT STRONGEST
    # =========================================================

    picks = sorted(
        picks,
        reverse=True
    )

    final_picks = picks[:3]

    if len(final_picks) < 3:
        return

    # =========================================================
    # MESSAGE
    # =========================================================

    msg = "🔥 DAILY AI BET SLIP\n\n"

    for p in final_picks:

        msg += (

            f"🌍 {p[6]}\n"

            f"🏆 {p[5]}\n"

            f"⚽ {p[1]} vs {p[2]}\n"

            f"⏰ {p[7]}\n"

            f"🎯 {p[3]}\n"

            f"💰 Odd: {p[4]}\n"

            f"✅ Confidence: {p[0]}%\n\n"

        )

        total_odds *= p[4]

    msg += (
        f"💎 TOTAL ODDS: "
        f"{round(total_odds,2)}"
    )

    send_telegram(msg)

    daily_ticket_sent = True
# =========================================================
# PREMATCH AI
# =========================================================

async def prematch_loop():

    while True:

        try:

            await daily_ticket()

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

                    # =================================================
                    # DATE FIRST
                    # =================================================

                    date = datetime.fromisoformat(

                        m["fixture"]["date"].replace(
                            "Z","+00:00"
                        )

                    ).astimezone(TZ)

                    diff = (

                        date - datetime.now(TZ)

                    ).total_seconds()

                    # само следващите 3 часа
                    if diff < 0:
                        continue

                    if diff > 10800:
                        continue

                    # само днешни мачове
                    if date.date() != datetime.now(TZ).date():
                        continue

                    # =================================================
                    # ODDS AFTER FILTER
                    # =================================================

                    fixture_id = m["fixture"]["id"]

                    odds_data = get_match_odds(
                        fixture_id
                    )

                    if odds_data is None:
                        continue
                    sharp_odd = (
                        odds_data["sharp_odd"]
                    )

                    soft_odd = (
                        odds_data["soft_odd"]
                    )

                    market = (
                        odds_data["market"]
                    )

                    odd = sharp_odd

                    # =================================================
                    # BLOCK WOMEN
                    # =================================================

                    text = (
                        home + " " + away
                    ).lower()

                    if any(

                        x in text

                        for x in [

                            " kvinner",
                            " women",
                            " female",
                            " ladies",
                            " w"

                        ]

                    ):

                        continue

                   

                    # =================================================
                    # SCORE ENGINE
                    # =================================================

                    score, market, fake_odd = (

                        calculate_match_score(

                            country,
                            league,
                            home,
                            away

                        )

                    )

                    confidence = 58 + score

                    # =================================================
                    # SIMPLE ATTACK MODEL
                    # =================================================

                    home_attack = round(

                        (
                            len(home) % 5
                        ) + 1.2,

                        2

                    )

                    away_attack = round(

                        (
                            len(away) % 5
                        ) + 1.2,

                        2

                    )

                    # =================================================
                    # POISSON
                    # =================================================

                    poisson_data = poisson_probability(

                        home_attack,
                        away_attack

                    )

                    over25_prob = poisson_data["over25"]

                    # =================================================
                    # FAIR ODDS
                    # =================================================

                    if market == "⚽ OVER 2.5 GOALS":

                        fair_odd = round(

                            100 / max(
                                over25_prob,
                                1
                            ),

                            2

                        )

                    else:

                        fair_odd = odd
                    # =================================================
                    # IMPLIED PROBABILITY
                    # =================================================

                    market_probability = round(

                        (1 / odd) * 100,

                        2

                    )

                    our_probability = confidence

                    true_edge = round(

                        our_probability
                        -
                        market_probability,

                        2

                    )

                    # =================================================
                    # FAIR ODD VALUE
                    # =================================================

                    if odd > fair_odd:

                        confidence += 5

                    # =================================================
                    # SHARP / SOFT VALUE
                    # =================================================

                    if soft_odd:

                        soft_edge = round(

                            (
                                soft_odd
                                -
                                sharp_odd
                            )

                            /
                            sharp_odd * 100,

                            2

                        )

                        if soft_edge >= 8:

                            confidence += 10
                            
                        elif soft_edge >= 5:

                            confidence += 6
                    # =================================================
                    # DROP + VELOCITY
                    # =================================================

                    drop, velocity = (

                        odds_drop_signal(

                            home,
                            away,
                            odd

                        )

                    )

                    if (

                        drop >= 0.25

                        or

                        velocity >= 0.03

                    ):

                        confidence += 12

                    # =================================================
                    # LEAGUE BONUS
                    # =================================================

                    if "Premier" in league:

                        confidence += 4

                    elif "La Liga" in league:

                        confidence += 3

                    elif "Serie A" in league:

                        confidence += 2

                    elif "Cup" in league:

                        confidence -= 6

                    confidence += min(

                        len(home) % 5,

                        4

                    )

                    confidence = min(
                        confidence,
                        92
                    )

                    # =================================================
                    # FILTERS
                    # =================================================

                    if confidence < 92:
                        continue

                    if true_edge < 12:
                        continue

                    # =================================================
                    # DUPLICATE
                    # =================================================

                    key = f"{home}_{away}"

                    if not can_send_prematch(key):
                        continue

                    # =================================================
                    # MESSAGE
                    # =================================================

                    msg = f"""
🔥 PRE-MATCH AI SIGNAL

🌍 {country}
🏆 {league}

⚽ {home} vs {away}

⏰ {date.strftime("%d.%m %H:%M")}

🎯 {market}

💰 Sharp Odd:
{sharp_odd}

📉 Odds Drop:
{drop}

⚡ Velocity:
{velocity}

💎 True Edge:
+{true_edge}%

📊 Over 2.5 Prob:
{over25_prob}%

⚖ Fair Odd:
{fair_odd}

✅ Confidence:
{confidence}%
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

        await asyncio.sleep(3600)
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
    
    init_learning_database()

    print("🚀 PRACTICAL LIVE AI SYSTEM STARTED")

    # LIVE
    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    # PREMATCH
    prematch_thread = threading.Thread(
        target=lambda:
        asyncio.run(prematch_loop()),
        daemon=True
    )

    prematch_thread.start()

    while True:
        time.sleep(60)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()

