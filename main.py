# =========================================================
# MAIN V3
# CLEAN BETTING SYSTEM
# =========================================================

import requests
import sqlite3
import asyncio
import threading
import time
import logging

 

from scipy.stats import poisson
from datetime import datetime, timedelta
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
# LEAGUE FILTERS
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
    "friendly",
    "friendlies",
    "u22",
    "u24",
    "olympic", 
    "reserve",
    "reserves"
]

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
# CACHE
# =========================================================

sent_live = {}

sent_prematch = {}

# =========================================================
# DATABASE
# =========================================================

def init_database():

    conn = sqlite3.connect(
        "v3_ai.db"
    )

    cursor = conn.cursor()

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS signals (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        fixture_id INTEGER,

        country TEXT,
        league TEXT,

        home_team TEXT,
        away_team TEXT,

        market TEXT,

        odd REAL,

        confidence REAL,

        result TEXT,

        created_at TEXT

    )

    """)

    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM
# =========================================================

async def send_telegram(message):

    try:

        await bot.send_message(

            chat_id=CHAT_ID,
            text=message

        )

    except Exception as e:

        print("TELEGRAM ERROR")
        print(str(e))

# =========================================================
# FILTERS
# =========================================================

def blocked_league(league):

    text = league.lower()

    for word in BLOCKED_WORDS:

        if word in text:

            return True

    return False

# =========================================================
# LIVE MATCHES
# =========================================================

def get_live_matches():

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures",

            headers=HEADERS,

            params={
                "live": "all"
            },

            timeout=20

        ).json()

        return r.get(
            "response",
            []
        )

    except:

        return []

# =========================================================
# LIVE STATISTICS
# =========================================================

def get_statistics(fixture_id):

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures/statistics",

            headers=HEADERS,

            params={
                "fixture": fixture_id
            },

            timeout=20

        ).json()

        return r.get(
            "response",
            []
        )

    except:

        return []

# =========================================================
# ODDS
# =========================================================

def get_odds(fixture_id):

    try:

        r = requests.get(

            f"{BASE_URL}/odds",

            headers=HEADERS,

            params={
                "fixture": fixture_id
            },

            timeout=20

        ).json()

        return r.get(
            "response",
            []
        )

    except:

        return []


# =========================================================
# MATCH ODDS
# =========================================================

def get_match_odds(fixture_id):

    try:

        odds = get_odds(
            fixture_id
        )

        if not odds:
            return None

        bookmakers = odds[0].get(
            "bookmakers",
            []
        )

        if not bookmakers:
            return None

        bets = bookmakers[0].get(
            "bets",
            []
        )

        for bet in bets:

            if bet.get(
                "name"
            ) == "Match Winner":

                home_odd = None
                draw_odd = None
                away_odd = None

                for value in bet.get(
                    "values",
                    []
                ):

                    if value["value"] == "Home":
                        home_odd = float(
                            value["odd"]
                        )

                    elif value["value"] == "Draw":
                        draw_odd = float(
                            value["odd"]
                        )

                    elif value["value"] == "Away":
                        away_odd = float(
                            value["odd"]
                        )

                return (
                    home_odd,
                    draw_odd,
                    away_odd
                )

        return None

    except:

        return None
# =========================================================
# EXTRACT STAT
# =========================================================

def extract(team, stat_name):

    try:

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

    except:

        pass

    return 0


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

    if shots_on == 0:
        return 0

    if total_shots < 4:
        return 0

 
    # possession

    if possession >= 55:
        pressure += 8

    if possession >= 60:
        pressure += 10

    if possession >= 65:
        pressure += 12

    # shots on target

    if shots_on >= 3:
        pressure += 15

    if shots_on >= 5:
        pressure += 20

    if shots_on >= 7:
        pressure += 25

    # total shots

    if total_shots >= 8:
        pressure += 8

    if total_shots >= 12:
        pressure += 10

    if total_shots >= 16:
        pressure += 12

    # corners

    if corners >= 4:
        pressure += 6

    if corners >= 7:
        pressure += 8

    if corners >= 10:
        pressure += 10

    # dangerous attacks

    if attacks >= 20:
        pressure += 10

    if attacks >= 35:
        pressure += 15

    if attacks >= 50:
        pressure += 20

    return min(
        pressure,
        100
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

                f"{BASE_URL}/fixtures",

                headers=HEADERS,

                params={
                    "date": date
                },

                timeout=20

            ).json()

            for match in r.get(
                "response",
                []
            ):

                fixture_time = datetime.fromisoformat(
                    match["fixture"]["date"].replace(
                        "Z",
                        "+00:00"
                    )
                )

                fixture_time = fixture_time.astimezone(
                    TZ
                )

                hours_left = (
                    fixture_time - now
                ).total_seconds() / 3600

                if 0 <= hours_left <= 12:

                    matches.append(
                        match
                    )

        except:

            pass

    matches.sort(

        key=lambda x:
        x["fixture"]["date"]

    )

    return matches




             
# =========================================================
# LIVE ANALYSIS
# =========================================================

def analyze_live_match(match):

    try:

        home_team = match["teams"]["home"]["name"]
        away_team = match["teams"]["away"]["name"]

        banned = [

            "russia",
            "belarus"

        ]

        check_text = (
            home_team +
            " " +
            away_team
        ).lower()

        for word in banned:

            if word in check_text:
                return None

        text = (
            home_team +
            " " +
            away_team
        ).lower()

        blocked = [

            "res",
            "reserve",

            "women",

            "u17",
            "u18",
            "u19",
            "u20",
            "u21",
            "u22",
            "u23"

        ]

        for word in blocked:

            if word in text:
                return None

        fixture_id = match["fixture"]["id"]

        stats = get_statistics(
            fixture_id
        )

        if len(stats) < 2:
            return None

        home_stats = stats[0]
        away_stats = stats[1]

        home_pressure = calculate_pressure(
            home_stats
        )

        away_pressure = calculate_pressure(
            away_stats
        )

        home_shots_on = extract(
            home_stats,
            "Shots on Goal"
        )

        away_shots_on = extract(
            away_stats,
            "Shots on Goal"
        )

        home_total_shots = extract(
            home_stats,
            "Total Shots"
        )

        away_total_shots = extract(
            away_stats,
            "Total Shots"
        )

        home_corners = extract(
            home_stats,
            "Corner Kicks"
        )

        away_corners = extract(
            away_stats,
            "Corner Kicks"
        )

        shots_diff = abs(
            home_shots_on -
            away_shots_on
        )

        corners_diff = abs(
            home_corners -
            away_corners
        )

        dominance = abs(
            home_pressure -
            away_pressure
        )

        minute = match["fixture"]["status"]["elapsed"]

        if not minute:
            return None

        if minute < 20:
            return None

        if minute > 84:
            return None

       

        home = match["goals"]["home"] or 0
        away = match["goals"]["away"] or 0

        total = home + away

        goal_diff = abs(
            home - away
        )

        # FAST GOALS OVERRIDE

        if (
            minute <= 40
            and
            total >= 2
            and
            goal_diff >= 2
            and
            max(
                home_pressure,
                away_pressure
            ) >= 80
        ):

            if home > away:

                return (

                    "🎯 NEXT GOAL HOME",
                    90,
                    minute,
                    90

                )

            else:

                return (

                    "🎯 NEXT GOAL AWAY",
                    90,
                    minute,
                    90

                )            
                  

        if total >= 6:
            return None

        if total == 0 and minute > 65:
            return None

        if dominance < 20:
            return None

        if shots_diff < 2:
            return None

        if corners_diff < 1:
            return None

        if max(
            home_total_shots,
            away_total_shots
        ) < 6:
            return None

        if max(
            home_pressure,
            away_pressure
        ) < 70:
            return None

             # GOAL BEFORE FT

        if (
            minute >= 75
            and
            minute <= 84
            and
            home_pressure >= 80
            and
            away_pressure >= 80
            and
            home_shots_on >= 4
            and
            away_shots_on >= 4
        ):

            return (

                "🥅 GOAL BEFORE FT",
                85,
                minute,
                85

            )

        # OVER 1.5 REMAINING GOALS

        if (
            minute <= 70
            and
            home_pressure >= 75
            and
            away_pressure >= 75
            and
            home_shots_on >= 4
            and
            away_shots_on >= 4
            and
            home_corners >= 4
            and
            away_corners >= 4
        ):

            return (

                "🚀 OVER 1.5 REMAINING GOALS",
                90,
                minute,
                90

            )

        market = "🎯 NEXT GOAL HOME"

        if away_pressure > home_pressure:
            market = "🎯 NEXT GOAL AWAY"

       
        confidence = min(
            95,
            max(
                home_pressure,
                away_pressure
            )
        )

     
        goal_probability = 50

        goal_probability += (
            max(
                home_pressure,
                away_pressure
            ) - 75
        ) * 2

        goal_probability += shots_diff * 3

        goal_probability += corners_diff * 2

        goal_probability = min(
            95,
            max(
                55,
                goal_probability
            )
        )
        return (

            market,
            confidence,
            minute, 
            goal_probability

        )

    except:

        return None

    
# =========================================================
# TEAM FORM
# =========================================================

def get_team_form(team_id):

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures",

            headers=HEADERS,

            params={
                "team": team_id,
                "last": 5
            },

            timeout=20

        ).json()

        games = r.get(
            "response",
            []
        )

        if not games:

            return None

        scored = 0
        conceded = 0

        wins = 0
        losses = 0

        over25 = 0
        btts = 0
        draws = 0

        for g in games:

            home_id = g["teams"]["home"]["id"]

            gh = g["goals"]["home"] or 0
            ga = g["goals"]["away"] or 0

            if team_id == home_id:

                team_goals = gh
                opp_goals = ga

            else:

                team_goals = ga
                opp_goals = gh

            scored += team_goals
            conceded += opp_goals

            if team_goals > opp_goals:
                wins += 1

            elif team_goals < opp_goals:
                losses += 1

            else:
                draws += 1

            if (gh + ga) >= 3:
                over25 += 1

            if gh > 0 and ga > 0:
                btts += 1

        total = len(games)

        points = wins * 3
        form_pct = round((points / 15) * 100, 2)

        unbeaten = wins + draws

        unbeaten_pct = round(
             (unbeaten / total) * 100,
             2
        )
     
        return {

            "avg_scored":
                round(scored / total, 2),

            "total_scored":
                scored,

            "avg_conceded":
                round(conceded / total, 2),

            "wins":
                wins,

            "losses":
                losses,

            "draws":
                draws,

            "unbeaten":
                unbeaten,

            "unbeaten_pct":
                unbeaten_pct,

            "over25":
                over25,

            "btts":
                btts, 

            "played":
                total,
           "form_pct":
               form_pct
        }

    except:

        return None

# =========================================================
# POISSON
# =========================================================

def poisson_over25(home_attack, away_attack):

    

    prob = 0

    for h in range(8):

        for a in range(8):

            total = h + a

            p = (
                poisson.pmf(
                    h,
                    home_attack
                )
                *
                poisson.pmf(
                    a,
                    away_attack
                )
            )

            if total >= 3:

                prob += p

    return round(
        prob * 100,
        2
    )

# =========================================================
# BTTS POISSON
# =========================================================

def poisson_btts(home_attack, away_attack):

    prob = 0

    for h in range(8):

        for a in range(8):

            p = (
                poisson.pmf(
                    h,
                    home_attack
                )
                *
                poisson.pmf(
                    a,
                    away_attack
                )
            )

            if h > 0 and a > 0:

                prob += p

    return round(
        prob * 100,
        2
    )

# =========================================================
# FORM SCORE
# =========================================================

def calculate_form_score(

    home_form,
    away_form

):
    
    score = 0

    score += home_form["form_pct"] * 0.5
    score += away_form["form_pct"] * 0.5

    score += (
        home_form["over25"]
        +
        away_form["over25"]
    ) * 2

    score += (
        home_form["btts"]
        +
        away_form["btts"]
    ) * 2

    return min(
        100,
        round(score, 2)
    )
    
# =========================================================
# HOME WIN SCORE
# =========================================================

def home_win_score(

    home_form,
    away_form

):

    score = 0

    score += (
        home_form["total_scored"]
        -
        away_form["total_scored"]
    ) * 1

    score += (
        away_form["losses"]
        -
        home_form["losses"]
    ) * 3

    score += (
        home_form["form_pct"]
        -
        away_form["form_pct"]
    )

    score += (
        home_form["avg_scored"]
        -
        away_form["avg_scored"]
    ) * 8

    score += (
        away_form["avg_conceded"]
        -
        home_form["avg_conceded"]
    ) * 5

    return round(score, 2)
    
# =========================================================
# LEAGUE WEIGHT
# =========================================================

TOP_GOAL_COUNTRIES = [

    "Netherlands",
    "Norway",
    "Sweden",
    "Denmark",
    "Belgium",
    "Austria"

]

LOW_GOAL_COUNTRIES = [

    "Peru",
    "Paraguay",
    "Bolivia",
    "Ecuador",
    "Venezuela"

]

def league_score(country, market):

    score = 0

    if country in TOP_GOAL_COUNTRIES:

        if market == "⚽ OVER 2.5":
            score += 10

        elif market == "💎 BTTS":
            score += 8

    if country in LOW_GOAL_COUNTRIES:

        if market == "⚽ OVER 2.5":
            score -= 10

        elif market == "💎 BTTS":
            score -= 8

    return score

# =========================================================
# FAIR ODDS
# =========================================================

def fair_odds(probability):

    if probability <= 0:
        return 999

    return round(
        100 / probability,
        2
    )

# =========================================================
# VALUE
# =========================================================

def value_edge(

    probability,
    odd

):

    market_prob = 100 / odd

    return round(
        probability - market_prob,
        2
    )
    
# =========================================================
# SAVE SIGNAL
# =========================================================

def save_signal(

    fixture_id,
    country,
    league,

    home,
    away,

    market,

    odd,
    confidence

):

    conn = sqlite3.connect(
        "v3_ai.db"
    )

    cur = conn.cursor()

    cur.execute(

        """
        INSERT INTO signals (

            fixture_id,

            country,
            league,

            home_team,
            away_team,

            market,

            odd,
            confidence,

            created_at

        )

        VALUES (?,?,?,?,?,?,?,?,?)
        """,

        (

            fixture_id,

            country,
            league,

            home,
            away,

            market,

            odd,
            confidence,

            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        )

    )

    conn.commit()
    conn.close()

# =========================================================
# PREMATCH SCORE
# =========================================================

def calculate_final_score(

    form_score,
    poisson_score,

    value_score,
    league_bonus

):

    score = (

        form_score * 0.30 +

        poisson_score * 0.30 +

        value_score * 0.25 +

        league_bonus * 0.15

    )

    return round(score, 2)

# =========================================================
# CONFIDENCE
# =========================================================

def confidence_from_score(score):

    if score >= 95:
        return 95

    if score >= 90:
        return 90

    if score >= 85:
        return 85

    if score >= 80:
        return 80

    return 0

# =========================================================
# PREMATCH ANALYSIS
# =========================================================

def analyze_prematch_match(match):

    try:

        fixture_id = match["fixture"]["id"]
        match_odds = get_match_odds(
            fixture_id
        )

        country = match["league"]["country"]
        league = match["league"]["name"]

        if country in [

            "Russia",
            "Belarus"

        ]:
            return None

        bad_words = [

            "u17",
            "u18",
            "u19",
            "u20",
            "u21",
            "u23",

            "women",

            "reserve",
            "reserves",

            "friendly",

            "russia",
            "russian",

            "belarus",
            "belarusian",

        ]

        league_text = (
            country +
            " " +
            league
        ).lower()

        for word in bad_words:

            if word in league_text:
                return None

        if country in BAD_COUNTRIES:
            return None

        if blocked_league(league):
            return None

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        if home.endswith(" W"):
            return None

        if away.endswith(" W"):
            return None

        if " II" in home:
            return None

        if " II" in away:
            return None

        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]

        home_form = get_team_form(home_id)
        away_form = get_team_form(away_id)

        if not home_form or not away_form:
            return None

        if (
            home_form["played"] < 5
            or
            away_form["played"] < 5
        ):
            return None

        if (
            away_form["avg_scored"] < 0.8
            and
            home_form["avg_scored"] < 1.0
        ):
            return None

        over_prob = poisson_over25(

            home_form["avg_scored"],
            away_form["avg_scored"]

        )

        btts_prob = poisson_btts(

            home_form["avg_scored"],
            away_form["avg_scored"]

        )

        form_score = calculate_form_score(
            home_form,
            away_form
        )

        signals = []

        
        # HOME WIN

        home_score = home_win_score(
            home_form,
            away_form
        )

        home_edge = (
            home_form["wins"]
            -
            away_form["wins"]
        )

        form_gap = (
            home_form["form_pct"]
            -
            away_form["form_pct"]
        )

        home_value = False

        if match_odds:

            if (
                match_odds[0] is not None
                and
                match_odds[0] >= 2.20
                and
                home_score >= 90
            ):
                home_value = True

        if (
            home_score >= 72
            and
            home_form["unbeaten_pct"] >= 60
            and
            home_form["wins"] >= 3
            and
            home_edge >= 2
            and
            form_gap >= 15
            and
            home_form["avg_scored"] >= 1.4
            and
            home_form["avg_conceded"] <= 1.5
            and
            away_form["avg_conceded"] >= 1.2
        ):

            signals.append(

                (
                    "💎 VALUE HOME WIN"
                    if home_value
                    else
                    "🏆 HOME WIN",

                    confidence_from_score(
                        min(
                            95,
                            home_score
                        )
                    ),

                    min(
                        95,
                        round(
                            home_score,
                            1
                        )
                    )
                )

            )

        # AWAY WIN

        away_score = (

            (
                away_form["total_scored"]
                -
                home_form["total_scored"]
            ) * 1

            +

            (
                home_form["losses"]
                -
                away_form["losses"]
            ) * 3

            +

            (
                away_form["form_pct"]
                -
                home_form["form_pct"]
            )

            +

            (
                away_form["avg_scored"]
                -
                home_form["avg_scored"]
            ) * 8

            +

            (
                home_form["avg_conceded"]
                -
                away_form["avg_conceded"]
            ) * 5

        )

        away_edge = (
            away_form["wins"]
            -
            home_form["wins"]
        )

        away_gap = (
            away_form["form_pct"]
            -
            home_form["form_pct"]
        )

        away_value = False

        if match_odds:

            if (
                match_odds[2] is not None
                and
                match_odds[2] >= 2.50
                and
                away_score >= 90
            ):
                away_value = True

        if (
            away_score >= 72
            and
            away_form["unbeaten_pct"] >= 60
            and
            away_form["wins"] >= 3
            and
            away_edge >= 2
            and
            away_gap >= 15
            and
            away_form["avg_scored"] >= 1.4
            and
            away_form["avg_conceded"] <= 1.5
            and
            home_form["avg_conceded"] >= 1.2
        ):

            signals.append(

                (
                    "💎 VALUE AWAY WIN"
                    if away_value
                    else
                    "✈️ AWAY WIN",

                    confidence_from_score(
                        min(
                            95,
                            away_score
                        )
                    ),

                    min(
                        95,
                        round(
                            away_score,
                            1
                        )
                    )
                )

            )

        # OVER 2.5

        over_league = league_score(
            country,
            "⚽ OVER 2.5"
        )

        over_final = calculate_final_score(

            form_score,
            over_prob,

            10,
            over_league

        )

        over_conf = confidence_from_score(
            over_final
        )

        if (
            over_prob >= 68
            and
            over_conf >= 85
            and
            home_form["avg_scored"] >= 1.2
            and
            away_form["avg_scored"] >= 1.0
            and
            home_form["avg_conceded"] >= 1.0
            and
            away_form["avg_conceded"] >= 1.0
            and
            (
                home_form["over25"]
                +
                away_form["over25"]
            ) >= 5
        ):

            signals.append(

                (
                    "⚽ OVER 2.5",
                    over_conf,
                    round(
                        over_prob,
                        1
                    )
                )

            )

        # BTTS

        btts_league = league_score(
            country,
            "💎 BTTS"
        )

        btts_final = calculate_final_score(

            form_score,
            btts_prob,

            10,
            btts_league

        )

        btts_conf = confidence_from_score(
            btts_final
        )

        if (
            btts_prob >= 65
            and
            btts_conf >= 85
            and
            home_form["avg_scored"] >= 1.2
            and
            away_form["avg_scored"] >= 1.0
            and
            home_form["btts"] >= 2
            and
            away_form["btts"] >= 2
        ):

            signals.append(

                (
                    "💎 BTTS",
                    btts_conf,
                    round(
                        btts_prob,
                        1
                    )
                )

            )
         
        signals.sort(
            reverse=True,
            key=lambda x: x[2]
        )

        signals = signals[:2]

        return signals

    except Exception as e:

        print(
            "PREMATCH ERROR:",
            str(e)
        )

        return None
        
# =========================================================
# SEND PREMATCH SIGNAL
# =========================================================

async def send_prematch_signal(

    fixture_id,

    match_date,
    kickoff_time,

    country,
    league,

    home,
    away,

    market,

    confidence,
    probability, 
    odds_text

):

    message = f"""
🔥 PREMATCH V3

🏆 {home} vs {away}

🗓 Date: {match_date}
🕒 Kickoff: {kickoff_time}

🌍 {country}
🏟 {league}

📊 Market:
{market}

📈 Rating:
{probability}

💰 Odds:
{odds_text}

💎 Confidence:
{confidence}%

⭐ Rating Class:
{"ELITE" if probability >= 95 else "STRONG"}

🏅 Model Rank:
TOP 5 PICK

📋 Filter:
TOP5 MODEL PICK
"""

    await send_telegram(message)

    save_signal(

        fixture_id,

        country,
        league,

        home,
        away,

        market,

        0,
        confidence

    )

# =========================================================
# PREMATCH LOOP
# =========================================================

def prematch_loop():

    print("PREMATCH SCAN START")

    matches = get_upcoming_matches()

    print(
        f"Matches found: {len(matches)}"
    )

    all_signals = []

    for match in matches:

        signals = analyze_prematch_match(
            match
        )

        if not signals:
            continue

        fixture_id = match["fixture"]["id"]
        
        match_odds = get_match_odds(
            fixture_id
        )

        odds_text = "-"

    if match_odds:

        home_odd = match_odds[0]
        away_odd = match_odds[2]

        if home_odd is not None and away_odd is not None:

            odds_text = (
                f"H:{home_odd} | A:{away_odd}"
            )
        country = match["league"]["country"]
        league = match["league"]["name"]

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        fixture_time = datetime.fromisoformat(
            match["fixture"]["date"].replace(
                "Z",
                "+00:00"
            )
        ).astimezone(TZ)

        match_date = fixture_time.strftime(
            "%d.%m.%Y"
        )

        kickoff_time = fixture_time.strftime(
            "%H:%M"
        )

        for market, confidence, probability in signals:

            all_signals.append(

                (
                    probability,
                    fixture_id,

                    match_date,
                    kickoff_time,

                    country,
                    league,

                    home,
                    away,

                    market,
                    confidence
                )

            )

    all_signals.sort(
        reverse=True,
        key=lambda x: x[0]
    )

    top_signals = all_signals[:5]

    for (
        probability,
        fixture_id,

        match_date,
        kickoff_time,

        country,
        league,

        home,
        away,

        market,
        confidence
    ) in top_signals:

        key = f"{fixture_id}"

        if key in sent_prematch:

            if (
                time.time()
                -
                sent_prematch[key]
            ) < 86400:

                continue

        sent_prematch[key] = time.time()

        print(
            market,
            confidence,
            probability
        )

        asyncio.run(

            send_prematch_signal(

                fixture_id,

                match_date,
                kickoff_time,

                country,
                league,

                home,
                away,

                market,

                confidence,
                probability

            )

        )

# =========================================================
# LIVE LOOP
# =========================================================

def live_loop():

    matches = get_live_matches()

    print(f"Live matches: {len(matches)}")

    print("LIVE SCAN START")

    for match in matches:

        signal = analyze_live_match(
            match
        )

        if not signal:
            continue

        fixture_id = match["fixture"]["id"]

        home_goals = match["goals"]["home"] or 0
        away_goals = match["goals"]["away"] or 0

        key = (
            f"live_{fixture_id}_"
            f"{home_goals}_{away_goals}"
        )

        if key in sent_live:
            continue

        sent_live[key] = time.time()

        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]

        home_goals = (
            match["goals"]["home"] or 0
        )

        away_goals = (
            match["goals"]["away"] or 0
        )

        minute = signal[2]

        goal_probability = signal[3]

        confidence = signal[1]

        stats = get_statistics(
            fixture_id
        )

        home_pressure = 0
        away_pressure = 0

        home_shots = 0
        away_shots = 0

        home_corners = 0
        away_corners = 0

        if len(stats) >= 2:

            home_pressure = calculate_pressure(
                stats[0]
            )

            away_pressure = calculate_pressure(
                stats[1]
            )

            home_shots = extract(
                stats[0],
                "Shots on Goal"
            )

            away_shots = extract(
                stats[1],
                "Shots on Goal"
            )

            home_corners = extract(
                stats[0],
                "Corner Kicks"
            )

            away_corners = extract(
                stats[1],
                "Corner Kicks"
            )

        asyncio.run(

            send_telegram(

                f"""
🔥 LIVE SIGNAL

🏆 {home} vs {away}

📊 Score:
{match["goals"]["home"] or 0} - {match["goals"]["away"] or 0}

⏱ Minute: {minute}

{signal[0]}

🔥 Home Pressure: {home_pressure}
🔥 Away Pressure: {away_pressure}

🎯 Shots On Target:
{home_shots} - {away_shots}

🚩 Corners:
{home_corners} - {away_corners}

💎 Confidence: {confidence}%

🎯 Goal Probability:
{goal_probability}%
"""

            )

        )

if __name__ == "__main__":

    print("MAIN V3 STARTED")
   
    init_database()

    while True:

        prematch_loop()

        live_loop()

        time.sleep(300)


