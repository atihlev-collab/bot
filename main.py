#=========================================================
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
    "reserves",
    "academy",
    "amateur"
]

BAD_COUNTRIES = [

    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia",

    "Russia",
    "Belarus",
    "Israel",
    "Nicaragua",
    "Guatemala",
    "Honduras",
    "El Salvador"
]


# =========================================================
# CACHE
# =========================================================

sent_live = {}

sent_prematch = {}
team_form_cache = {}

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

    cursor.execute("""

    CREATE TABLE IF NOT EXISTS odds_history (

        fixture_id INTEGER PRIMARY KEY,

        home_odd REAL,

        draw_odd REAL,

        away_odd REAL,

        updated_at TEXT

    )

    """)




    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    try:

        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )

        

    except Exception as e:

        print("TELEGRAM ERROR")
        print(repr(e))

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

        print(
            "GET ODDS FOR:",
            fixture_id
        )

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

            print(
                "BET NAME =",
                bet.get("name")
            )

            if bet.get("name") in [
                "Match Winner",
                "1X2",
                "Winner"
            ]:

                home_odd = None
                draw_odd = None
                away_odd = None

                for value in bet.get(
                    "values",
                    []
                ):

                    print(
                        "VALUE =",
                        value
                    )

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

                print(
                    "ODDS FOUND:",
                    home_odd,
                    draw_odd,
                    away_odd
                )

                if (
                    home_odd is not None
                    and
                    draw_odd is not None
                    and
                    away_odd is not None
                ):

                    return (
                        home_odd,
                        draw_odd,
                        away_odd
                    )

                print(
                    "INCOMPLETE ODDS:",
                    home_odd,
                    draw_odd,
                    away_odd
                )

        return None

    except Exception as e:

        print(
            "GET MATCH ODDS ERROR:",
            repr(e)
        )

        return None

# =========================================================
# ODDS DROP
# =========================================================

def odds_drop_check(

    fixture_id,
    home_odd,
    draw_odd,
    away_odd

):

    try:

        conn = sqlite3.connect(
            "v3_ai.db"
        )

        cur = conn.cursor()

        cur.execute(

            """
            SELECT

                home_odd,
                draw_odd,
                away_odd

            FROM odds_history

            WHERE fixture_id = ?
            """,

            (fixture_id,)
        )

        row = cur.fetchone()

        drop_home = False
        drop_away = False

        home_drop_text = ""
        away_drop_text = ""

        if row:

            old_home = row[0]
            old_away = row[2]

            if (
                old_home
                and
                home_odd
            ):

                drop_percent = (
                    (old_home - home_odd)
                    / old_home
                ) * 100

                if 4 <= drop_percent <= 25:

                    drop_home = True

                    home_drop_text = (
                        f"{old_home} → {home_odd}"
                    )

            if (
                old_away
                and
                away_odd
            ):

                drop_percent = (
                    (old_away - away_odd)
                    / old_away
                ) * 100

                if 4 <= drop_percent <= 25:

                    drop_away = True

                    away_drop_text = (
                        f"{old_away} → {away_odd}"
                    )

            cur.execute(

                """
                UPDATE odds_history

                SET

                home_odd = ?,
                draw_odd = ?,
                away_odd = ?,
                updated_at = ?

                WHERE fixture_id = ?
                """,

                (

                    home_odd,
                    draw_odd,
                    away_odd,

                    datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                    fixture_id

                )

            )

        else:

            cur.execute(

                """
                INSERT INTO odds_history (

                    fixture_id,

                    home_odd,
                    draw_odd,
                    away_odd,

                    updated_at

                )

                VALUES (?,?,?,?,?)
                """,

                (

                    fixture_id,

                    home_odd,
                    draw_odd,
                    away_odd,

                    datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                )

            )

        conn.commit()
        conn.close()

        return (

            drop_home,
            drop_away,

            home_drop_text,
            away_drop_text

        )

    except:

        return (
            False,
            False,
            "",
            ""
        )

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

    if shots_on == 0 and attacks < 35:
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

    if attacks >= 70:
        pressure += 10

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

                if 0 <= hours_left <= 6:

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

def analyze_live_match(fixture):
    try:
        fixture_id = fixture["fixture"]["id"]
        
        home_goals = fixture["goals"].get("home", 0) or 0
        away_goals = fixture["goals"].get("away", 0) or 0
        current_goals = home_goals + away_goals
        

        home_team = fixture["teams"]["home"]["name"]
        away_team = fixture["teams"]["away"]["name"]


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

        stats = get_statistics(
            fixture_id
        )

        if len(stats) < 2:
            return None

        home_stats = stats[0]
        away_stats = stats[1]

        home_red = extract(
            home_stats,
            "Red Cards"
        )

        away_red = extract(
            away_stats,
            "Red Cards"
        )

        home_pressure = calculate_pressure(
            home_stats
        )

        away_pressure = calculate_pressure(
            away_stats
        )

        # FORM BONUS

        home_form = get_team_form(
            fixture["teams"]["home"]["id"],
            venue="home"
        )

        away_form = get_team_form(
            fixture["teams"]["away"]["id"],
            venue="away"
        )

        if home_form:

           home_pressure += min(
               15,
               round(home_form["form_pct"] / 8)
        )

        if away_form:

            away_pressure += min(
            15,
            round(away_form["form_pct"] / 8)
        )

        home_pressure = min(
            home_pressure,
            100
        )

        away_pressure = min(
            away_pressure,
            100
        )

        if home_form and home_form["avg_scored"] < 1.0:

            home_pressure -= 8

        if away_form and away_form["avg_scored"] < 1.0:

            away_pressure -= 8
     
        if home_red > away_red:

            home_pressure -= 25
            away_pressure += 15

        home_xg = extract(
            home_stats,
            "Expected Goals"
        )

        away_xg = extract(
            away_stats,
            "Expected Goals"
        )

        if home_xg >= 1.0:

            home_pressure += 10

        elif home_xg >= 0.9:

            home_pressure += 5

        if away_xg >= 1.0:

            away_pressure += 10

        elif away_xg >= 0.9:

           away_pressure += 5

        if away_red > home_red:

            away_pressure -= 25
            home_pressure += 15

        home_shots_on = extract(
            home_stats,
            "Shots on Goal"
        )

        away_shots_on = extract(
            away_stats,
            "Shots on Goal"
        )

        if home_shots_on == 0:

            home_pressure -= 15

        if away_shots_on == 0:

            away_pressure -= 15

        if home_shots_on == 0:

            away_pressure += 5

        if away_shots_on == 0:

            home_pressure += 5
          
        home_total_shots = extract(
            home_stats,
            "Total Shots"
        )

        away_total_shots = extract(
            away_stats,
            "Total Shots"
        )

        if home_shots_on >= 6:
            home_pressure += 5

        if away_shots_on >= 6:
            away_pressure += 5
        
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

        minute = fixture["fixture"]["status"]["elapsed"]

        if not minute:
            return None

        if minute < 20:
            return None

        if minute > 80:
            return None

       

        home = fixture["goals"]["home"] or 0
        away = fixture["goals"]["away"] or 0

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
                  

        if total >= 7:
            return None

        if dominance < 15:
            return None

        if shots_diff < 1:
            return None


        if max(
            home_total_shots,
            away_total_shots
        ) < 5:
            return None

        if max(
            home_pressure,
            away_pressure
        ) < 65:
            return None

        

        # OVER 1.5 REMAINING GOALS

        if (
            minute <= 70
            and
            home_pressure >= 60
            and
            away_pressure >= 60
            and
            home_shots_on >= 2
            and
            away_shots_on >= 2
            and              
            home_corners >= 2
            and
            away_corners >= 2
        ):

            return (

                "🚀 OVER 1.5 REMAINING GOALS",
                90,
                minute,
                90

            )

        # NEXT GOAL ONLY UNTIL 80'

        if minute > 80:

            return None

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

        

        goal_probability = min(
            95,
            max(
                55,
                goal_probability
             
            )
        )
     
        if goal_probability < 79:
            return None
      
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

def get_team_form(team_id, venue=None):

    cache_key = f"{team_id}_{venue}"

    if cache_key in team_form_cache:

        cache_time, data = team_form_cache[cache_key]

        if time.time() - cache_time < 21600:
            return data

    try:

        r = requests.get(

            f"{BASE_URL}/fixtures",

            headers=HEADERS,

            params={
                "team": team_id,
                "last": 10
            },

            timeout=20

        ).json()

        games = r.get(
            "response",
            []
        )

        if not games:
            return None

        filtered_games = []

        for g in games:

            home_id = g["teams"]["home"]["id"]

            if venue == "home":

                if home_id == team_id:
                    filtered_games.append(g)

            elif venue == "away":

                if home_id != team_id:
                    filtered_games.append(g)

            else:

                filtered_games.append(g)

        games = filtered_games

        if len(games) < 3:
            return None

        scored = 0
        conceded = 0

        wins = 0
        losses = 0
        draws = 0

        over25 = 0
        btts = 0

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

        recent_games = games[:5]        

        recent_points = 0                

        for g in recent_games:          

            home_id = g["teams"]["home"]["id"]  

            gh = g["goals"]["home"] or 0        
            ga = g["goals"]["away"] or 0        

            if team_id == home_id:              

                team_goals = gh                
                opp_goals = ga                  

            else:                             

                team_goals = ga                 
                opp_goals = gh                  
             
            if team_goals > opp_goals:          

                recent_points += 3             

            elif team_goals == opp_goals:      

                recent_points += 1             

        recent_form_pct = round(                
            (recent_points / 15) * 100,        
            2                                   
        )                                      

        total = len(games)

        points = wins * 3

        form_pct = round(
            (points / (total * 3)) * 100,
            2
        )

        unbeaten = wins + draws

        unbeaten_pct = round(
            (unbeaten / total) * 100,
            2
        )

        result = {

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
                form_pct,
         
            "recent_form_pct":
                recent_form_pct
        }

        team_form_cache[cache_key] = (
            time.time(),
            result
        )

        return result

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

        form_score * 0.40 +

        poisson_score * 0.35 +

        value_score * 0.20 +

        league_bonus * 0.05

    )

    return round(score, 2)



# =========================================================
# CONFIDENCE
# =========================================================

def confidence_from_score(score):

    if score >= 95:
        return 95

    elif score >= 92:
        return 92

    elif score >= 90:
        return 90

    elif score >= 88:
        return 88

    elif score >= 85:
        return 85

    elif score >= 82:
        return 82

    elif score >= 80:
        return 80

    elif score >= 77:
        return 77

    elif score >= 75:
        return 75

    elif score >= 72:
        return 72

    elif score >= 70:
        return 70

    return 65
# =========================================================
# PREMATCH ANALYSIS
# =========================================================

def analyze_prematch_match(match):

    try:

        fixture_id = match["fixture"]["id"]
        match_odds = get_match_odds(
            fixture_id
        )
        if not match_odds:
            return None

        if (
           match_odds[0] is None
           or
           match_odds[2] is None
       ):
           return None

        home_drop = False
        away_drop = False

        if match_odds:

            (
                home_drop,
                away_drop,

                home_drop_text,
                away_drop_text

            ) = odds_drop_check(

                fixture_id,

                match_odds[0],
                match_odds[1],
                match_odds[2]

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

        home_form = get_team_form(
            home_id,
            venue="home"
        )

        away_form = get_team_form(
            away_id,
            venue="away"
        )
        if not home_form or not away_form:
            return None

        if (
            home_form["played"] < 3
            or
            away_form["played"] < 3
        ):
            return None

        if (
            away_form["avg_scored"] < 0.8
            and
            home_form["avg_scored"] < 1.0
        ):
            return None
        print(                 
            "OVER ANALYZE:",    
            home,               
            away                
        )                       

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
        
        print(                  
            "SIGNALS START:",  
            home,              
            away               
        )                       
     
        signals = []

        
                # HOME WIN

        home_score = home_win_score(
            home_form,
            away_form
        )

                
        # FORM COLLAPSE BONUS

        if (
            away_form["losses"] >= 5
            or
            away_form["form_pct"] <= 35
        ):

            home_score += 8

        # SUPER FORM BONUS

        if (
            home_form["form_pct"] >= 80
            and
            home_form["wins"] >= 6
        ):   

           home_score += 5

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

        recent_gap = (                        
            home_form["recent_form_pct"]      
            -
            away_form["recent_form_pct"]      
        )                                     

        home_super_value = False
        home_value = False

        if match_odds:

            if (
                match_odds[0] is not None
            ):

                edge = value_edge(
                    min(95, home_score),
                    match_odds[0]
                )

                if edge >= 15:
                 
                    home_super_value = True

                elif edge >= 10:

                    home_value = True       
         
        home_odds_ok = True

        if (
            match_odds
            and
            match_odds[0] is not None
        ):

            home_odds_ok = (
                1.45 <= match_odds[0] <= 2.10
            )


        if (
            home_score >= 35
            and
            home_odds_ok
            and
            home_form["unbeaten_pct"] >= 60
            and
            home_form["wins"] >= 2
            and
            home_edge >= 2
            and
            form_gap >= 10
            and
            recent_gap >= 10
            and
            home_form["avg_scored"] >= 1.5
            and
            home_form["avg_conceded"] <= 1.3
            and
            away_form["avg_conceded"] >= 1.2
        ):

            signals.append(

                (                                   
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
       
        
        # FORM COLLAPSE BONUS

        if (
            home_form["losses"] >= 5
            or
            home_form["form_pct"] <= 35
        ):

            away_score += 8

        # SUPER FORM BONUS

        if (
            away_form["form_pct"] >= 80
            and
            away_form["wins"] >= 6
        ):

            away_score += 5
 
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

        recent_away_gap = (                   
            away_form["recent_form_pct"]       
            -
            home_form["recent_form_pct"]    
        )                                     
             
        away_super_value = False
        away_value = False

        if match_odds:
         
            if (
                match_odds[2] is not None
            ):

               edge = value_edge(
                   min(95, away_score),
                   match_odds[2]
              )

               if edge >= 15:

                   away_super_value = True

               elif edge >= 10:

                   away_value = True

     
        away_odds_ok = True

        if match_odds and match_odds[2] is not None:

            away_odds_ok = (
               1.55 <= match_odds[2] <= 2.60
            )


        if (
            away_score >= 35
            and
            away_odds_ok
            and
            away_form["unbeaten_pct"] >= 60
            and
            away_form["wins"] >= 2
            and
            away_edge >= 2
            and
            away_gap >= 10
            and
            recent_away_gap >= 10
            and
            away_form["avg_scored"] >= 1.5
            and
            away_form["avg_conceded"] <= 1.3
            and
            home_form["avg_conceded"] >= 1.2
        ):

            signals.append(

                (                                 
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

     
        print(
            "OVER CHECK:",
            home,
            away,
            over_prob,
            home_form["over25"],
            away_form["over25"]
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

        print(                          
            "OVER DETAILS:",            
            home,                       
            away,                       
            over_prob,                  
            over_conf,                   
            home_form["avg_scored"],    
            away_form["avg_scored"],    
            home_form["avg_conceded"],   
            away_form["avg_conceded"]    
        )                                

        if (
            over_prob >= 60
            and
            over_conf >= 65
            and
            home_form["avg_scored"] >= 1.0
            and
            away_form["avg_scored"] >= 0.9
            and
            home_form["avg_conceded"] >= 1.0
            and
            away_form["avg_conceded"] >= 1.0
            and
            (
                home_form["over25"]
                +
                away_form["over25"]
            ) >= 4
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
         
        print(
            "BTTS CHECK:",
            home,
            away,
            btts_prob,
            home_form["btts"],
            away_form["btts"]
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
            btts_prob >= 60
            and
            btts_conf >= 65
            and
            home_form["avg_scored"] >= 1.0
            and
            away_form["avg_scored"] >= 0.9
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

            print("CHECKING:", home, "vs", away)
            
         
        signals.sort(
            reverse=True,
            key=lambda x: x[2]
        )

        signals = signals[:2]
     
        print(
            "RETURN SIGNALS:", 
            home, 
            away, 
            len(signals)
        )
     
        return signals

    except Exception as e:
                           
        print(
            "PREMATCH ERROR:",
            repr(e)
        )

        return None
        
# =========================================================
# SEND PREMATCH SIGNAL
# =========================================================

def send_prematch_signal(

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
    odds_text,
    drop_text

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

📉 Odds Drop:
{drop_text}

💎 Confidence:
{confidence}%

⭐ Rating Class:
{"ELITE" if probability >= 95 else "STRONG"}

🏅 Model Rank:
TOP 5 PICK

📋 Filter:
TOP5 MODEL PICK

{"📉 ODDS DROP" if drop_text != "-" else ""}
"""

    send_telegram(message)

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

        print(
            "MATCH ODDS:",
            fixture_id,
            match_odds
        )

        home_drop = False
        away_drop = False

        home_drop_text = ""
        away_drop_text = ""

        if match_odds:

            (
                home_drop,
                away_drop,

                home_drop_text,
                away_drop_text

            ) = odds_drop_check(

                fixture_id,

                match_odds[0],
                match_odds[1],
                match_odds[2]

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

        # ==========================================
        # ODDS DROP HOME
        # ==========================================

        if home_drop:

            all_signals.append(

                (
                    999,
                    fixture_id,

                    match_date,
                    kickoff_time,

                    country,
                    league,

                    home,
                    away,
                 
                    "📉 ODDS DROP HOME",
                    90,
                    str(match_odds[0]),
                    home_drop_text
                )

            )

        # ==========================================
        # ODDS DROP AWAY
        # ==========================================

        if away_drop:

            all_signals.append(

                (
                    999,
                    fixture_id,

                    match_date,
                    kickoff_time,

                    country,
                    league,

                    home,
                    away,

                    "📉 ODDS DROP AWAY",
                    90,
                    str(match_odds[2]),
                    away_drop_text
                 )
             )

   
     
        for market, confidence, probability in signals:

            odds_text = "-"
            drop_text = "-"

            if match_odds:

                if (
                    "HOME WIN" in market
                    and
                    match_odds[0] is not None
                ):

                    odds_text = str(
                        match_odds[0]
                    )

                    if home_drop:

                        drop_text = home_drop_text

                elif (
                    "AWAY WIN" in market
                    and
                    match_odds[2] is not None
                ):

                    odds_text = str(
                        match_odds[2]
                    )

                    if away_drop:

                        drop_text = away_drop_text

                elif (
                    "BTTS" in market
                ):

                    odds_text = "BTTS"

                elif (
                    "OVER" in market
                ):

                    odds_text = "OVER"

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
                    confidence,
                    odds_text,
                    drop_text
                )

            )

    all_signals.sort(
        reverse=True,
        key=lambda x: x[0]
    )

    special_signals = [

        s for s in all_signals

        if (
            "VALUE" in s[8]
            or
            s[11] != "-"
        )

    ]

    top_signals = all_signals[:3]

    for s in special_signals:

        if s not in top_signals:

            top_signals.append(s)

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
        confidence,
        odds_text,
        drop_text

    ) in top_signals:

        key = f"{fixture_id}_{market}"

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

        print("DEBUG MARKET =", market)
        print("DEBUG ODDS_TEXT =", odds_text)

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
            probability,
            odds_text,
            drop_text

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
         
if __name__ == "__main__":

    print("MAIN V3 STARTED")
   
    init_database()

    while True:

        prematch_loop()

        live_loop()

        time.sleep(300)



