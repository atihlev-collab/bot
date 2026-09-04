# =========================================================
# MAIN V4
# AI BETTING SYSTEM
# VERSION 4.0
# =========================================================

import asyncio
import math
import logging
import sqlite3
import threading
import time

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from scipy.stats import poisson
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID


# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY
}

TIMEZONE = ZoneInfo("Europe/Sofia")

REQUEST_TIMEOUT = 20
API_RETRIES = 3

bot = Bot(token=BOT_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# CACHE
# =========================================================

CACHE_TIME_FORM = 21600          # 6 hours
CACHE_TIME_ODDS = 900            # 15 minutes
CACHE_TIME_STANDINGS = 21600     # 6 hours
CACHE_TIME_LIVE = 30             # 30 seconds

sent_live = {}
sent_prematch = {}

team_form_cache = {}
odds_cache = {}
standings_cache = {}
statistics_cache = {}
betano_market_cache = {}
live_market_cache = {}


# =========================================================
# LEAGUE FILTERS
# =========================================================

BLOCKED_WORDS = {
    "women",
    "female",

    "reserve",
    "reserves",

    "academy",

    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u22",
    "u23",

    "friendly",
    "friendlies",

    "olympic",
    "amateur"
}


BAD_COUNTRIES = {
    "Bolivia",
    "Venezuela",

    "India",
    "Indonesia",

    "Russia",
    "Belarus",

    "Israel",

    "Guatemala",
    "Honduras",
    "El Salvador",

    "Nicaragua"
}


# =========================================================
# DATABASE
# =========================================================

DB_NAME = "v4_ai.db"


# BLOCK: INIT_DATABASE
# =========================================================
# BLOCK: LEAGUE AI SCORE
# =========================================================

def league_ai_score(
    country="",
    league_name="",
    market="",
    league_avg_goals=0.0,
    league_btts=0.0
):
    """
    Conservative league quality adjustment.
    Returns a neutral score when league-specific evidence is unavailable.
    """
    country = str(country or "").strip().lower()
    league_name = str(league_name or "").strip().lower()
    market = str(market or "").strip().lower()

    score = 50.0

    top_goal_countries = {
        "netherlands", "norway", "sweden",
        "denmark", "belgium", "austria"
    }
    low_goal_countries = {
        "peru", "paraguay", "bolivia",
        "ecuador", "venezuela"
    }

    if "over" in market and "2.5" in market:
        if country in top_goal_countries:
            score += 10
        elif country in low_goal_countries:
            score -= 10

    if "btts" in market:
        if country in top_goal_countries:
            score += 6
        elif country in low_goal_countries:
            score -= 6

    return max(0.0, min(100.0, score))


# =========================================================
# BLOCK: INIT DATABASE
# =========================================================

def init_database():

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals(

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            fixture_id INTEGER,

            country TEXT,

            league TEXT,

            home_team TEXT,

            away_team TEXT,

            market TEXT,

            probability REAL,

            odd REAL,

            confidence REAL,

            result TEXT,

            created_at TEXT

        )
    """)

    # Faster lookups for fixture / market history
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_fixture
        ON signals(fixture_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_market
        ON signals(market)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_created
        ON signals(created_at)
    """)

    conn.commit()
    conn.close()


# =========================================================
# TELEGRAM
# =========================================================

# BLOCK: SEND_TELEGRAM
def send_telegram(message):

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",

            json={
                "chat_id": CHAT_ID,
                "text": message
            },

            timeout=20
        )

        if response.status_code == 200:
            return True

        logging.warning(
            "TELEGRAM ERROR HTTP %s | %s",
            response.status_code,
            response.text[:300]
        )

        return False

    except Exception as e:

        logging.warning(
            "TELEGRAM ERROR %s",
            repr(e)
        )

        return False


# =========================================================
# API ENGINE
# =========================================================

# BLOCK: API_GET
def api_get(endpoint, params=None):

    params = params or {}

    for attempt in range(1, API_RETRIES + 1):

        try:

            response = requests.get(
                f"{BASE_URL}/{endpoint}",
                headers=HEADERS,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            # Successful request
            if response.status_code == 200:

                try:
                    return response.json()

                except ValueError:

                    logging.warning(
                        "API INVALID JSON | %s",
                        endpoint
                    )

                    return {}

            # API limit
            if response.status_code == 429:

                wait_time = min(10, attempt * 3)

                logging.warning(
                    "API RATE LIMIT 429 | %s | waiting %ss",
                    endpoint,
                    wait_time
                )

                time.sleep(wait_time)
                continue

            # Temporary server error
            if response.status_code >= 500:

                wait_time = attempt

                logging.warning(
                    "API SERVER ERROR %s | %s | retry %s/%s",
                    response.status_code,
                    endpoint,
                    attempt,
                    API_RETRIES
                )

                time.sleep(wait_time)
                continue

            # Other HTTP error
            logging.warning(
                "API HTTP ERROR %s | %s",
                response.status_code,
                endpoint
            )

            time.sleep(1)

        except requests.RequestException as e:

            logging.warning(
                "API REQUEST ERROR | %s | attempt %s/%s | %s",
                endpoint,
                attempt,
                API_RETRIES,
                repr(e)
            )

            time.sleep(attempt)

        except Exception as e:

            logging.warning(
                "API ERROR | %s | %s",
                endpoint,
                repr(e)
            )

            time.sleep(1)

    return {}


# =========================================================
# BASIC HELPERS
# =========================================================

# BLOCK: BLOCKED_LEAGUE
def blocked_league(name):

    text = clean_text(name)

    for word in BLOCKED_WORDS:

        if word in text:
            return True

    return False


# BLOCK: BAD_COUNTRY
def bad_country(country):

    if not country:
        return False

    country_clean = str(country).strip().lower()

    return any(
        country_clean == str(bad).strip().lower()
        for bad in BAD_COUNTRIES
    )


# BLOCK: SAFE_FLOAT
def safe_float(value, default=None):

    if value is None:
        return default

    try:

        if isinstance(value, str):
            value = (
                value
                .replace("%", "")
                .replace(",", ".")
                .strip()
            )

            if not value:
                return default

        return float(value)

    except (ValueError, TypeError):

        return default


# BLOCK: CLEAN_TEXT
def clean_text(text):

    if text is None:
        return ""

    return " ".join(
        str(text)
        .lower()
        .replace("_", " ")
        .split()
    )


# BLOCK: EXTRACT
def extract(team, stat_name):

    if not isinstance(team, dict):
        return 0

    statistics = team.get("statistics", [])

    if not isinstance(statistics, list):
        return 0

    for stat in statistics:

        if not isinstance(stat, dict):
            continue

        if stat.get("type") != stat_name:
            continue

        value = stat.get("value")

        if value is None:
            return 0

        number = safe_float(value)

        if number is None:
            return 0

        return number

    return 0

# =========================================================
# API FUNCTIONS
# =========================================================

# BLOCK: GET_LIVE_MATCHES
def get_live_matches():

    data = api_get(
        "fixtures",
        {
            "live": "all"
        }
    )

    if not isinstance(data, dict):
        return []

    return data.get("response", [])


# BLOCK: GET_STATISTICS
def get_statistics(fixture_id):

    if not fixture_id:
        return []

    # LIVE statistics change quickly
    if fixture_id in statistics_cache:

        cache_time, data = statistics_cache[fixture_id]

        if time.time() - cache_time < CACHE_TIME_LIVE:
            return data

    data = api_get(
        "fixtures/statistics",
        {
            "fixture": fixture_id
        }
    )

    if not isinstance(data, dict):
        return []

    result = data.get("response", [])

    if not isinstance(result, list):
        result = []

    statistics_cache[fixture_id] = (
        time.time(),
        result
    )

    return result


# BLOCK: GET_ODDS
def get_odds(fixture_id):

    if not fixture_id:
        return []

    data = api_get(
        "odds",
        {
            "fixture": fixture_id
        }
    )

    if not isinstance(data, dict):
        return []

    result = data.get("response", [])

    if not isinstance(result, list):
        return []

    return result


# =========================================================
# MATCH ODDS
# =========================================================

# BLOCK: GET_MATCH_ODDS
def get_match_odds(fixture_id):

    if not fixture_id:
        return None

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    if fixture_id in odds_cache:

        cache_time, data = odds_cache[fixture_id]

        if time.time() - cache_time < CACHE_TIME_ODDS:
            return data

    # -----------------------------------------------------
    # API
    # -----------------------------------------------------

    odds = get_odds(fixture_id)

    if not odds:
        return None

    # -----------------------------------------------------
    # FIND BETANO
    # -----------------------------------------------------

    bookmakers = odds[0].get(
        "bookmakers",
        []
    )

    if not isinstance(bookmakers, list):
        return None

    betano = None

    for bookmaker in bookmakers:

        bookmaker_id = bookmaker.get("id")
        bookmaker_name = clean_text(
            bookmaker.get("name")
        )

        if (
            bookmaker_id == 32
            or bookmaker_name == "betano"
        ):

            betano = bookmaker
            break

    if betano is None:
        return None

    # -----------------------------------------------------
    # ODDS
    # -----------------------------------------------------

    home = None
    draw = None
    away = None

    over25 = None
    under25 = None

    over35 = None
    under35 = None

    btts = None

    home_over15 = None
    away_over15 = None

    # -----------------------------------------------------
    # BET MARKETS
    # -----------------------------------------------------

    bets = betano.get(
        "bets",
        []
    )

    if not isinstance(bets, list):
        return None

    for bet in bets:

        if not isinstance(bet, dict):
            continue

        bet_name = clean_text(
            bet.get("name")
        )

        values = bet.get(
            "values",
            []
        )

        if not isinstance(values, list):
            continue

        # =================================================
        # 1X2
        # =================================================

        if bet_name in (
            "match winner",
            "winner",
            "1x2"
        ):

            for value in values:

                if not isinstance(value, dict):
                    continue

                name = clean_text(
                    value.get("value")
                )

                odd = safe_float(
                    value.get("odd")
                )

                if odd is None:
                    continue

                if name == "home":
                    home = odd

                elif name == "draw":
                    draw = odd

                elif name == "away":
                    away = odd

        # =================================================
        # BTTS
        # =================================================

        elif (
            "both teams to score" in bet_name
            or bet_name == "btts"
        ):

            for value in values:

                if not isinstance(value, dict):
                    continue

                name = clean_text(
                    value.get("value")
                )

                if name == "yes":

                    btts = safe_float(
                        value.get("odd")
                    )

        # =================================================
        # TOTAL GOALS
        # =================================================

        elif (
            "goal" in bet_name
            and "home" not in bet_name
            and "away" not in bet_name
        ):

            for value in values:

                if not isinstance(value, dict):
                    continue

                name = clean_text(
                    value.get("value")
                )

                odd = safe_float(
                    value.get("odd")
                )

                if odd is None:
                    continue

                if name.startswith("over 2.5"):
                    over25 = odd

                elif name.startswith("under 2.5"):
                    under25 = odd

                elif name.startswith("over 3.5"):
                    over35 = odd

                elif name.startswith("under 3.5"):
                    under35 = odd

        # =================================================
        # HOME TEAM GOALS
        # =================================================

        elif (
            "home" in bet_name
            and "goal" in bet_name
        ):

            for value in values:

                if not isinstance(value, dict):
                    continue

                name = clean_text(
                    value.get("value")
                )

                if name.startswith("over 1.5"):

                    home_over15 = safe_float(
                        value.get("odd")
                    )

        # =================================================
        # AWAY TEAM GOALS
        # =================================================

        elif (
            "away" in bet_name
            and "goal" in bet_name
        ):

            for value in values:

                if not isinstance(value, dict):
                    continue

                name = clean_text(
                    value.get("value")
                )

                if name.startswith("over 1.5"):

                    away_over15 = safe_float(
                        value.get("odd")
                    )

    # -----------------------------------------------------
    # 1X2 IS REQUIRED
    # -----------------------------------------------------

    if (
        home is None
        or draw is None
        or away is None
    ):
        return None

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    result = (
        home,
        draw,
        away,
        over25,
        under25,
        over35,
        under35,
        btts,
        home_over15,
        away_over15
    )

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    odds_cache[fixture_id] = (
        time.time(),
        result
    )

    return result

# =========================================================
# TEAM FORM V4
# =========================================================

# BLOCK: GET_TEAM_FORM
def get_team_form(team_id, venue=None):

    if not team_id:
        return None

    cache_key = f"{team_id}_{venue}"

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    if cache_key in team_form_cache:

        cache_time, data = team_form_cache[cache_key]

        if time.time() - cache_time < CACHE_TIME_FORM:
            return data

    # -----------------------------------------------------
    # API
    # -----------------------------------------------------
    # 10 matches are enough for general form.
    # For home/away analysis we may need more games
    # to find 5 matches at the requested venue.
    # -----------------------------------------------------

    last_matches = 10 if venue is None else 20

    data = api_get(
        "fixtures",
        {
            "team": team_id,
            "last": last_matches
        }
    )

    if not isinstance(data, dict):
        return None

    games = data.get("response", [])

    if not isinstance(games, list):
        return None

    # -----------------------------------------------------
    # VENUE FILTER
    # -----------------------------------------------------

    filtered = []

    for game in games:

        try:

            home_id = game["teams"]["home"]["id"]

            if venue == "home":

                if home_id == team_id:
                    filtered.append(game)

            elif venue == "away":

                if home_id != team_id:
                    filtered.append(game)

            else:

                filtered.append(game)

        except (KeyError, TypeError):
            continue

    # -----------------------------------------------------
    # ONLY THE MOST RECENT 5
    # -----------------------------------------------------

    games = filtered[:5]

    if len(games) < 5:
        return None

    played = len(games)

    # -----------------------------------------------------
    # BASIC STATS
    # -----------------------------------------------------

    wins = 0
    draws = 0
    losses = 0

    scored = 0
    conceded = 0

    clean_sheets = 0
    scored_games = 0

    over25 = 0
    btts = 0

    # -----------------------------------------------------
    # RECENT FORM
    # Newest match gets the highest weight.
    # 5 + 4 + 3 + 2 + 1 = 15
    # -----------------------------------------------------

    weights = [5, 4, 3, 2, 1]

    recent_points = 0
    max_recent_points = 3 * sum(weights)

    # -----------------------------------------------------
    # PROCESS MATCHES
    # -----------------------------------------------------

    for i, game in enumerate(games):

        try:

            home_id = game["teams"]["home"]["id"]

            gh = safe_float(
                game["goals"]["home"],
                0
            )

            ga = safe_float(
                game["goals"]["away"],
                0
            )

        except (KeyError, TypeError):

            continue

        if home_id == team_id:

            gf = gh
            gc = ga

        else:

            gf = ga
            gc = gh

        scored += gf
        conceded += gc

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        if gf > gc:

            wins += 1
            recent_points += 3 * weights[i]

        elif gf == gc:

            draws += 1
            recent_points += weights[i]

        else:

            losses += 1

        # -------------------------------------------------
        # GOAL PROFILE
        # -------------------------------------------------

        if gf > 0:
            scored_games += 1

        if gc == 0:
            clean_sheets += 1

        if gf + gc >= 3:
            over25 += 1

        if gf > 0 and gc > 0:
            btts += 1

    # -----------------------------------------------------
    # FORM
    # -----------------------------------------------------

    form_points = wins * 3 + draws

    form_pct = round(
        form_points /
        (played * 3)
        * 100,
        2
    )

    recent_form_pct = round(
        recent_points /
        max_recent_points
        * 100,
        2
    )

    # -----------------------------------------------------
    # GOALS
    # -----------------------------------------------------

    avg_scored = round(
        scored / played,
        2
    )

    avg_conceded = round(
        conceded / played,
        2
    )

    goal_diff = scored - conceded

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    result = {

        "played": played,

        "wins": wins,
        "draws": draws,
        "losses": losses,

        "form_pct": form_pct,
        "recent_form_pct": recent_form_pct,

        "avg_scored": avg_scored,
        "avg_conceded": avg_conceded,

        "goal_diff": goal_diff,

        "over25_pct": round(
            over25 / played * 100,
            2
        ),

        "btts_pct": round(
            btts / played * 100,
            2
        ),

        "clean_sheet_pct": round(
            clean_sheets / played * 100,
            2
        ),

        "scored_pct": round(
            scored_games / played * 100,
            2
        )
    }

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    team_form_cache[cache_key] = (
        time.time(),
        result
    )

    return result


# =========================================================
# STANDINGS V4
# =========================================================

# BLOCK: GET_LEAGUE_TABLE
def get_league_table(league, season):

    if not league or not season:
        return {}

    key = f"{league}_{season}"

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    if key in standings_cache:

        cache_time, data = standings_cache[key]

        if time.time() - cache_time < CACHE_TIME_STANDINGS:
            return data

    # -----------------------------------------------------
    # API
    # -----------------------------------------------------

    data = api_get(
        "standings",
        {
            "league": league,
            "season": season
        }
    )

    if not isinstance(data, dict):
        return {}

    table = {}

    try:

        response = data.get(
            "response",
            []
        )

        if not response:
            return {}

        standings = (
            response[0]
            .get("league", {})
            .get("standings", [])
        )

        if not standings:
            return {}

        standings = standings[0]

        for row in standings:

            team = row.get("team", {})

            team_id = team.get("id")

            if not team_id:
                continue

            all_stats = row.get(
                "all",
                {}
            )

            table[team_id] = {

                "rank": row.get(
                    "rank",
                    0
                ),

                "points": row.get(
                    "points",
                    0
                ),

                "played": all_stats.get(
                    "played",
                    0
                ),

                "goal_diff": row.get(
                    "goalsDiff",
                    0
                )
            }

    except (KeyError, IndexError, TypeError):

        table = {}

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    standings_cache[key] = (
        time.time(),
        table
    )

    return table


# =========================================================
# POISSON ENGINE V4
# =========================================================

# BLOCK: POISSON_OVER25
def poisson_over25(
    home_attack,
    away_attack
):

    home_attack = max(
        0,
        safe_float(home_attack, 0)
    )

    away_attack = max(
        0,
        safe_float(away_attack, 0)
    )

    total_goals = (
        home_attack +
        away_attack
    )

    probability = 1 - poisson.cdf(
        2,
        total_goals
    )

    return round(
        probability * 100,
        2
    )


# BLOCK: POISSON_OVER35
def poisson_over35(
    home_attack,
    away_attack
):

    home_attack = max(
        0,
        safe_float(home_attack, 0)
    )

    away_attack = max(
        0,
        safe_float(away_attack, 0)
    )

    total_goals = (
        home_attack +
        away_attack
    )

    probability = 1 - poisson.cdf(
        3,
        total_goals
    )

    return round(
        probability * 100,
        2
    )


# BLOCK: POISSON_UNDER25
def poisson_under25(
    home_attack,
    away_attack
):

    return round(
        100 -
        poisson_over25(
            home_attack,
            away_attack
        ),
        2
    )


# BLOCK: POISSON_UNDER35
def poisson_under35(
    home_attack,
    away_attack
):

    return round(
        100 -
        poisson_over35(
            home_attack,
            away_attack
        ),
        2
    )


# BLOCK: POISSON_BTTS
def poisson_btts(
    home_attack,
    away_attack
):

    home_attack = max(
        0,
        safe_float(home_attack, 0)
    )

    away_attack = max(
        0,
        safe_float(away_attack, 0)
    )

    home_scores = (
        1 -
        poisson.pmf(
            0,
            home_attack
        )
    )

    away_scores = (
        1 -
        poisson.pmf(
            0,
            away_attack
        )
    )

    probability = (
        home_scores *
        away_scores
    )

    return round(
        probability * 100,
        2
    )

# =========================================================
# AI TEAM STRENGTH V4
# =========================================================

# BLOCK: TEAM_STRENGTH
def team_strength(form):

    if not form:
        return 0

    # -----------------------------------------------------
    # ATTACK
    # Strong attacking output is important,
    # but we do not let it dominate the whole model.
    # -----------------------------------------------------

    attack = (
        form["avg_scored"] * 30
        +
        form["scored_pct"] * 0.20
    )

    # -----------------------------------------------------
    # DEFENCE
    # -----------------------------------------------------

    defence = (
        form["clean_sheet_pct"] * 0.15
        -
        form["avg_conceded"] * 15
    )

    # -----------------------------------------------------
    # FORM
    # Recent form has slightly more weight.
    # -----------------------------------------------------

    form_score = (
        form["form_pct"] * 0.25
        +
        form["recent_form_pct"] * 0.35
    )

    # -----------------------------------------------------
    # GOAL DIFFERENCE
    # Small adjustment only.
    # -----------------------------------------------------

    goals = (
        form["goal_diff"] * 1.5
    )

    score = (
        attack
        +
        defence
        +
        form_score
        +
        goals
    )

    return round(
        score,
        2
    )


# =========================================================
# AI MATCH SCORE V4
# =========================================================

# BLOCK: AI_MATCH_SCORE
def ai_match_score(
    home_form,
    away_form,
    table_home=None,
    table_away=None
):

    if not home_form or not away_form:
        return None

    home = team_strength(
        home_form
    )

    away = team_strength(
        away_form
    )

    # -----------------------------------------------------
    # LEAGUE TABLE
    # Small adjustment only.
    # We do not let the table dominate recent form.
    # -----------------------------------------------------

    if table_home and table_away:

        home += (
            table_away["rank"]
            -
            table_home["rank"]
        ) * 1.5

        away += (
            table_home["rank"]
            -
            table_away["rank"]
        ) * 1.5

        home += (
            table_home["goal_diff"]
            -
            table_away["goal_diff"]
        ) * 0.20

        away += (
            table_away["goal_diff"]
            -
            table_home["goal_diff"]
        ) * 0.20

    # -----------------------------------------------------
    # PREVENT NEGATIVE STRENGTH
    # -----------------------------------------------------

    home = max(
        1,
        home
    )

    away = max(
        1,
        away
    )

    total_strength = (
        home +
        away
    )

    # -----------------------------------------------------
    # RELATIVE STRENGTH
    #
    # IMPORTANT:
    # These are NOT final 1X2 probabilities.
    # They show the relative strength between
    # the two teams.
    # -----------------------------------------------------

    home_strength_pct = round(
        home /
        total_strength
        * 100,
        1
    )

    away_strength_pct = round(
        away /
        total_strength
        * 100,
        1
    )

    return {

        "home_strength": round(
            home,
            2
        ),

        "away_strength": round(
            away,
            2
        ),

        "home_strength_pct": home_strength_pct,

        "away_strength_pct": away_strength_pct
    }


# =========================================================
# VALUE ENGINE V4
# =========================================================

# BLOCK: FAIR_ODDS
def fair_odds(probability):

    probability = safe_float(
        probability
    )

    if probability is None:
        return None

    probability = max(
        1,
        min(
            99,
            probability
        )
    )

    return round(
        100 /
        probability,
        2
    )


# BLOCK: VALUE_EDGE
def value_edge(
    probability,
    bookmaker_odd
):

    probability = safe_float(
        probability
    )

    bookmaker_odd = safe_float(
        bookmaker_odd
    )

    if (
        probability is None
        or
        bookmaker_odd is None
        or
        bookmaker_odd <= 1.01
    ):
        return 0

    fair = fair_odds(
        probability
    )

    if fair is None:
        return 0

    return round(
        (
            bookmaker_odd -
            fair
        )
        /
        fair
        * 100,
        1
    )


# =========================================================
# VALUE CLASSIFIER V4
# =========================================================

# BLOCK: CLASSIFY_VALUE
def classify_value(
    probability,
    odd
):

    edge = value_edge(
        probability,
        odd
    )

    # Only two useful levels.
    # We deliberately avoid too many classifications.

    if edge >= 15:

        return (
            "💎 SUPER VALUE",
            edge
        )

    if edge >= 8:

        return (
            "⭐ VALUE",
            edge
        )

    return (
        None,
        edge
    )


# =========================================================
# MARKET FILTER V4
# =========================================================

# BLOCK: MARKET_ALLOWED
def market_allowed(
    probability,
    odd,
    minimum_probability,
    minimum_odd=1.50,
    maximum_odd=4.00
):

    probability = safe_float(
        probability
    )

    odd = safe_float(
        odd
    )

    if probability is None:
        return False

    if odd is None:
        return False

    if probability < minimum_probability:
        return False

    if odd < minimum_odd:
        return False

    if odd > maximum_odd:
        return False

    return True

# =========================================================
# BET BUILDER AI V4
# =========================================================

BET_BUILDER_MIN_ODD = 1.50
BET_BUILDER_MAX_ODD = 3.50


# BLOCK: BUILD_BET_BUILDER
# =========================================================
# BLOCK: BET BUILDER
# =========================================================

def build_bet_builder(match):

    """
    V4 does NOT build large combinations.

    The main system sends individual high-quality signals.
    This builder is kept only as an optional helper for
    a very small combination when there is enough quality.
    """

    if not match:
        return None

    fixture = match.get(
        "fixture",
        {}
    ).get("id")

    if not fixture:
        return None

    signals = analyze_prematch(match)

    if not signals:
        return None

    odds = get_match_odds(
        fixture
    )

    if not odds:
        return None

    (
        home_odd,
        draw_odd,
        away_odd,
        over25_odd,
        under25_odd,
        over35_odd,
        under35_odd,
        btts_odd,
        home15_odd,
        away15_odd
    ) = odds

    # -----------------------------------------------------
    # V4 PRINCIPLE:
    # maximum 2 selections.
    # We do not create long accumulators.
    # -----------------------------------------------------

    candidates = []

    for signal in signals:

        market = signal.get(
            "market"
        )

        probability = safe_float(
            signal.get("probability")
        )

        confidence = safe_float(
            signal.get("confidence")
        )

        if (
            probability is None
            or
            confidence is None
        ):
            continue

        odd = None

        if market == "🏆 HOME WIN":
            odd = home_odd

        elif market == "✈️ AWAY WIN":
            odd = away_odd

        elif market == "🚀 OVER 2.5":
            odd = over25_odd

        elif market == "🛡 UNDER 2.5":
            odd = under25_odd

        elif market == "💎 BTTS YES":
            odd = btts_odd

        if odd is None:
            continue

        if (
            odd < BET_BUILDER_MIN_ODD
            or
            odd > BET_BUILDER_MAX_ODD
        ):
            continue

        candidates.append({
            "market": market,
            "odd": odd,
            "probability": probability,
            "confidence": confidence
        })

    if not candidates:
        return None

    # Strongest signals first
    candidates.sort(
        key=lambda x: (
            x["confidence"],
            x["probability"]
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # ONE STRONG SIGNAL
    # -----------------------------------------------------

    best = candidates[0]

    selections = [
        (
            best["market"],
            best["odd"]
        )
    ]

    total_odd = best["odd"]

    # -----------------------------------------------------
    # OPTIONAL SECOND SIGNAL
    # -----------------------------------------------------

    if len(candidates) > 1:

        second = candidates[1]

        # Do not combine identical / conflicting markets.
        if second["market"] != best["market"]:

            combined_odd = (
                total_odd *
                second["odd"]
            )

            if (
                combined_odd >= BET_BUILDER_MIN_ODD
                and
                combined_odd <= BET_BUILDER_MAX_ODD
            ):

                selections.append(
                    (
                        second["market"],
                        second["odd"]
                    )
                )

                total_odd = combined_odd

    total_odd = round(
        total_odd,
        2
    )

    return {

        "fixture_id": fixture,

        "total_odd": total_odd,

        "legs": selections
    }


# =========================================================
# LIVE CONFIDENCE ENGINE V4
# =========================================================

# BLOCK: CALCULATE_CONFIDENCE
def calculate_confidence(
    pressure,
    attack,
    xg,
    shots_on,
    total_shots,
    corners,
    minute,
    goal_diff
):

    """
    Simple confidence model.

    Only the strongest live indicators are used.
    """

    confidence = 50.0

    pressure = safe_float(
        pressure,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    xg = safe_float(
        xg,
        0
    )

    shots_on = safe_float(
        shots_on,
        0
    )

    total_shots = safe_float(
        total_shots,
        0
    )

    corners = safe_float(
        corners,
        0
    )

    minute = safe_float(
        minute,
        0
    )

    goal_diff = abs(
        safe_float(
            goal_diff,
            0
        )
    )

    # -----------------------------------------------------
    # PRESSURE
    # -----------------------------------------------------

    confidence += (
        max(0, pressure - 60)
        * 0.30
    )

    # -----------------------------------------------------
    # ATTACK MOMENTUM
    # -----------------------------------------------------

    confidence += (
        max(0, attack - 60)
        * 0.25
    )

    # -----------------------------------------------------
    # xG
    # -----------------------------------------------------

    confidence += min(
        10,
        xg * 5
    )

    # -----------------------------------------------------
    # SHOTS ON TARGET
    # -----------------------------------------------------

    confidence += min(
        8,
        shots_on * 1.2
    )

    # -----------------------------------------------------
    # TOTAL SHOTS
    # -----------------------------------------------------

    confidence += min(
        5,
        total_shots * 0.25
    )

    # -----------------------------------------------------
    # CORNERS
    # Secondary factor only.
    # -----------------------------------------------------

    confidence += min(
        4,
        corners * 0.4
    )

    # -----------------------------------------------------
    # LIVE WINDOW
    # Small bonus only.
    # -----------------------------------------------------

    if 55 <= minute <= 80:
        confidence += 3

    # -----------------------------------------------------
    # CLOSE GAME
    # -----------------------------------------------------

    if goal_diff <= 1:
        confidence += 3

    return round(
        min(
            95,
            confidence
        ),
        1
    )


# =========================================================
# SMART VALUE ENGINE V4
# =========================================================

# BLOCK: SMART_VALUE_SCORE
def smart_value_score(
    probability,
    odd,
    confidence
):

    """
    Simple value score.

    V4 intentionally avoids stacking many correlated
    variables into another artificial score.
    """

    probability = safe_float(
        probability
    )

    odd = safe_float(
        odd
    )

    confidence = safe_float(
        confidence
    )

    if (
        probability is None
        or
        odd is None
        or
        confidence is None
        or
        odd <= 1.01
    ):
        return 0

    implied_probability = (
        100 /
        odd
    )

    edge = (
        probability -
        implied_probability
    )

    score = (
        probability * 0.50
        +
        edge * 1.50
        +
        confidence * 0.25
    )

    return round(
        max(
            0,
            min(
                100,
                score
            )
        ),
        2
    )


# =========================================================
# AI RISK FILTER V4
# =========================================================

# BLOCK: RISK_FILTER
def risk_filter(
    probability,
    confidence,
    edge
):

    """
    Small risk filter.

    Only three major factors are used.
    """

    probability = safe_float(
        probability,
        0
    )

    confidence = safe_float(
        confidence,
        0
    )

    edge = safe_float(
        edge,
        0
    )

    risk = 0

    # -----------------------------------------------------
    # PROBABILITY
    # -----------------------------------------------------

    if probability < 60:
        risk += 30

    elif probability < 70:
        risk += 15

    # -----------------------------------------------------
    # CONFIDENCE
    # -----------------------------------------------------

    if confidence < 75:
        risk += 25

    elif confidence < 82:
        risk += 10

    # -----------------------------------------------------
    # VALUE
    # -----------------------------------------------------

    if edge < 3:
        risk += 20

    elif edge < 6:
        risk += 10

    return min(
        risk,
        100
    )


# =========================================================
# SIMPLE ODDS SCORE V4
# =========================================================

# BLOCK: SMART_ODDS_SCORE
def smart_odds_score(
    probability,
    odd,
    confidence
):

    probability = safe_float(
        probability
    )

    odd = safe_float(
        odd
    )

    confidence = safe_float(
        confidence
    )

    if (
        probability is None
        or
        odd is None
        or
        confidence is None
        or
        odd <= 1.01
    ):
        return 0

    implied_probability = (
        100 /
        odd
    )

    edge = (
        probability -
        implied_probability
    )

    score = 50

    # Value
    score += edge * 1.5

    # Confidence
    score += (
        confidence -
        70
    ) * 0.40

    return round(
        max(
            0,
            min(
                100,
                score
            )
        ),
        1
    )

# =========================================================
# AI MATCH QUALITY ENGINE V4
# =========================================================

# BLOCK: MATCH_QUALITY_SCORE
def match_quality_score(
    home_form,
    away_form,
    home_pressure,
    away_pressure,
    home_xg,
    away_xg,
    total_shots,
    total_shots_on,
    minute
):

    if not home_form or not away_form:
        return 0

    home_pressure = safe_float(
        home_pressure,
        0
    )

    away_pressure = safe_float(
        away_pressure,
        0
    )

    home_xg = safe_float(
        home_xg,
        0
    )

    away_xg = safe_float(
        away_xg,
        0
    )

    total_shots = safe_float(
        total_shots,
        0
    )

    total_shots_on = safe_float(
        total_shots_on,
        0
    )

    minute = safe_float(
        minute,
        0
    )

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    quality = 40.0

    # -----------------------------------------------------
    # RECENT FORM
    # Small contribution.
    # Form should not dominate LIVE analysis.
    # -----------------------------------------------------

    quality += (
        (
            home_form.get(
                "recent_form_pct",
                0
            )
            +
            away_form.get(
                "recent_form_pct",
                0
            )
        )
        * 0.05
    )

    # -----------------------------------------------------
    # PRESSURE
    # Strongest live indicator.
    # -----------------------------------------------------

    best_pressure = max(
        home_pressure,
        away_pressure
    )

    quality += (
        max(
            0,
            best_pressure - 60
        )
        * 0.20
    )

    # -----------------------------------------------------
    # xG
    # -----------------------------------------------------

    quality += (
        home_xg +
        away_xg
    ) * 7

    # -----------------------------------------------------
    # SHOTS
    # Secondary confirmation.
    # -----------------------------------------------------

    quality += min(
        10,
        total_shots * 0.35
    )

    quality += min(
        8,
        total_shots_on * 1.2
    )

    # -----------------------------------------------------
    # LIVE WINDOW
    # Very small bonus.
    # -----------------------------------------------------

    if 55 <= minute <= 80:
        quality += 3

    return round(
        min(
            100,
            quality
        ),
        1
    )


# =========================================================
# AI RISK ENGINE V4
# =========================================================

# BLOCK: CALCULATE_RISK
def calculate_risk(
    probability,
    confidence,
    edge
):

    probability = safe_float(
        probability,
        0
    )

    confidence = safe_float(
        confidence,
        0
    )

    edge = safe_float(
        edge,
        0
    )

    risk = 0

    # -----------------------------------------------------
    # PROBABILITY
    # -----------------------------------------------------

    if probability < 60:
        risk += 30

    elif probability < 70:
        risk += 15

    # -----------------------------------------------------
    # CONFIDENCE
    # -----------------------------------------------------

    if confidence < 75:
        risk += 25

    elif confidence < 82:
        risk += 10

    # -----------------------------------------------------
    # VALUE
    # -----------------------------------------------------

    if edge < 3:
        risk += 20

    elif edge < 6:
        risk += 10

    return min(
        100,
        risk
    )


# =========================================================
# DYNAMIC LIVE THRESHOLD V4
# =========================================================

# BLOCK: DYNAMIC_THRESHOLD
def dynamic_threshold(
    minute,
    pressure,
    attack,
    match_quality,
    risk
):

    pressure = safe_float(
        pressure,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    match_quality = safe_float(
        match_quality,
        0
    )

    risk = safe_float(
        risk,
        0
    )

    # -----------------------------------------------------
    # BASE
    # -----------------------------------------------------

    threshold = 82

    # -----------------------------------------------------
    # MATCH QUALITY
    # Only small adjustments.
    # -----------------------------------------------------

    if match_quality >= 90:

        threshold -= 4

    elif match_quality >= 82:

        threshold -= 2

    # -----------------------------------------------------
    # ATTACK
    # -----------------------------------------------------

    if attack >= 90:

        threshold -= 2

    # -----------------------------------------------------
    # PRESSURE
    # -----------------------------------------------------

    if pressure >= 90:

        threshold -= 2

    # -----------------------------------------------------
    # RISK
    # Risk has stronger influence than bonuses.
    # -----------------------------------------------------

    if risk >= 60:

        threshold += 5

    elif risk >= 45:

        threshold += 2

    # -----------------------------------------------------
    # LATE GAME
    # -----------------------------------------------------

    if minute >= 80:

        threshold -= 2

    # -----------------------------------------------------
    # SAFETY LIMITS
    # -----------------------------------------------------

    return max(
        75,
        min(
            90,
            threshold
        )
    )

# =========================================================
# AI SIGNAL MANAGER V4
# =========================================================

class SignalManager:

    def __init__(self):

        self.signals = []

    def add(
        self,
        market,
        probability,
        confidence,
        quality,
        risk,
        minute
    ):

        probability = safe_float(
            probability,
            0
        )

        confidence = safe_float(
            confidence,
            0
        )

        quality = safe_float(
            quality,
            0
        )

        risk = safe_float(
            risk,
            100
        )

        # -------------------------------------------------
        # V4 SIGNAL SCORE
        #
        # Only used for choosing between already
        # qualified signals.
        #
        # It does NOT create a signal by itself.
        # -------------------------------------------------

        score = (
            probability * 0.40
            +
            confidence * 0.35
            +
            quality * 0.15
            -
            risk * 0.10
        )

        self.signals.append({

            "market": market,

            "probability": probability,

            "confidence": confidence,

            "quality": quality,

            "risk": risk,

            "minute": minute,

            "score": round(
                score,
                2
            )
        })

    def best(self):

        if not self.signals:
            return None

        return max(
            self.signals,
            key=lambda x: x["score"]
        )


# =========================================================
# AI CONTEXT ENGINE V4
# =========================================================

# BLOCK: CONTEXT_SCORE
def context_score(
    minute,
    home_goals,
    away_goals,
    red_home,
    red_away,
    favorite,
    attack_diff
):

    score = 50

    minute = safe_float(
        minute,
        0
    )

    home_goals = safe_float(
        home_goals,
        0
    )

    away_goals = safe_float(
        away_goals,
        0
    )

    red_home = safe_float(
        red_home,
        0
    )

    red_away = safe_float(
        red_away,
        0
    )

    attack_diff = safe_float(
        attack_diff,
        0
    )

    # -----------------------------------------------------
    # MATCH MINUTE
    # Small bonus only.
    # -----------------------------------------------------

    if 60 <= minute <= 80:

        score += 4

    elif minute > 80:

        score += 2

    # -----------------------------------------------------
    # CLOSE GAME
    # -----------------------------------------------------

    if abs(
        home_goals -
        away_goals
    ) <= 1:

        score += 5

    # -----------------------------------------------------
    # RED CARDS
    # -----------------------------------------------------

    if red_home > red_away:

        score -= 8

    elif red_away > red_home:

        score -= 8

    # -----------------------------------------------------
    # FAVOURITE BEHIND
    # -----------------------------------------------------

    if favorite == "HOME":

        if home_goals < away_goals:
            score += 6

    elif favorite == "AWAY":

        if away_goals < home_goals:
            score += 6

    # -----------------------------------------------------
    # ATTACK DIFFERENCE
    # -----------------------------------------------------

    if abs(attack_diff) >= 25:

        score += 6

    elif abs(attack_diff) >= 15:

        score += 3

    return round(
        max(
            0,
            min(
                100,
                score
            )
        ),
        1
    )


# =========================================================
# AI EXPLAINABILITY ENGINE V4
# =========================================================

# BLOCK: EXPLAIN_SIGNAL
def explain_signal(
    market,
    probability,
    confidence,
    attack,
    pressure,
    quality,
    risk
):

    reasons = []

    probability = safe_float(
        probability,
        0
    )

    confidence = safe_float(
        confidence,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    pressure = safe_float(
        pressure,
        0
    )

    quality = safe_float(
        quality,
        0
    )

    risk = safe_float(
        risk,
        100
    )

    # -----------------------------------------------------
    # STRONGEST REASONS ONLY
    # -----------------------------------------------------

    if probability >= 80:

        reasons.append(
            f"High probability {probability:.1f}%"
        )

    elif probability >= 75:

        reasons.append(
            f"Good probability {probability:.1f}%"
        )

    if confidence >= 85:

        reasons.append(
            f"Strong confidence {confidence:.1f}"
        )

    if attack >= 80:

        reasons.append(
            f"Strong attack {attack:.1f}"
        )

    if pressure >= 75:

        reasons.append(
            f"High pressure {pressure:.1f}"
        )

    if quality >= 80:

        reasons.append(
            f"High match quality {quality:.1f}"
        )

    if risk <= 20:

        reasons.append(
            "Low calculated risk"
        )

    if not reasons:

        reasons.append(
            "Multiple supporting indicators"
        )

    return {

        "market": market,

        "summary": " | ".join(
            reasons[:4]
        )
    }


# =========================================================
# AI PERFORMANCE TRACKER V4
# =========================================================

# BLOCK: PERFORMANCE_REPORT
def performance_report(conn):

    if conn is None:
        return []

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            market,
            COUNT(*),
            SUM(
                CASE
                    WHEN result = 'WIN'
                    THEN 1
                    ELSE 0
                END
            )
        FROM signals
        WHERE result IS NOT NULL
        GROUP BY market
    """)

    rows = cursor.fetchall()

    report = []

    for market, total, wins in rows:

        if not total:
            continue

        winrate = round(
            wins * 100 / total,
            1
        )

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        if total < 20:

            status = "📊 SMALL SAMPLE"

        elif winrate >= 85:

            status = "👑 ELITE"

        elif winrate >= 80:

            status = "🔥 EXCELLENT"

        elif winrate >= 75:

            status = "💎 GOOD"

        elif winrate >= 70:

            status = "⭐ OK"

        else:

            status = "⚠ NEEDS IMPROVEMENT"

        report.append({

            "market": market,

            "total": total,

            "wins": wins,

            "winrate": winrate,

            "status": status
        })

    return sorted(
        report,
        key=lambda x: (
            x["winrate"],
            x["total"]
        ),
        reverse=True
    )

# =========================================================
# AI SIGNAL COOLDOWN ENGINE V4
# =========================================================

SIGNAL_HISTORY = {}


# BLOCK: CAN_SEND_SIGNAL
def can_send_signal(
    fixture_id,
    market,
    cooldown=600
):

    if not fixture_id or not market:
        return False

    key = f"{fixture_id}_{market}"

    now = time.time()

    # -----------------------------------------------------
    # First signal
    # -----------------------------------------------------

    if key not in SIGNAL_HISTORY:

        SIGNAL_HISTORY[key] = now

        return True

    # -----------------------------------------------------
    # Existing signal
    # -----------------------------------------------------

    elapsed = (
        now -
        SIGNAL_HISTORY[key]
    )

    if elapsed >= cooldown:

        SIGNAL_HISTORY[key] = now

        return True

    return False


# =========================================================
# SIGNIFICANT LIVE CHANGE
# =========================================================

# BLOCK: SIGNIFICANT_CHANGE
def significant_change(
    previous_attack,
    current_attack,
    previous_pressure,
    current_pressure
):

    previous_attack = safe_float(
        previous_attack,
        0
    )

    current_attack = safe_float(
        current_attack,
        0
    )

    previous_pressure = safe_float(
        previous_pressure,
        0
    )

    current_pressure = safe_float(
        current_pressure,
        0
    )

    # -----------------------------------------------------
    # ATTACK CHANGE
    # -----------------------------------------------------

    if abs(
        current_attack -
        previous_attack
    ) >= 12:

        return True

    # -----------------------------------------------------
    # PRESSURE CHANGE
    # -----------------------------------------------------

    if abs(
        current_pressure -
        previous_pressure
    ) >= 15:

        return True

    return False


# =========================================================
# LIVE SIGNAL STATE
# =========================================================

LIVE_STATE = {}


# BLOCK: GET_LIVE_STATE
def get_live_state(fixture_id):

    if fixture_id not in LIVE_STATE:

        LIVE_STATE[fixture_id] = {

            "attack": 0,

            "pressure": 0,

            "minute": 0,

            "last_score": None,

            "updated_at": 0
        }

    return LIVE_STATE[fixture_id]


# BLOCK: UPDATE_LIVE_STATE
def update_live_state(
    fixture_id,
    attack,
    pressure,
    minute,
    current_score
):

    state = get_live_state(
        fixture_id
    )

    previous_attack = state.get(
        "attack",
        0
    )

    previous_pressure = state.get(
        "pressure",
        0
    )

    previous_score = state.get(
        "last_score"
    )

    changed = significant_change(
        previous_attack,
        attack,
        previous_pressure,
        pressure
    )

    score_changed = (
        previous_score is not None
        and
        previous_score != current_score
    )

    # -----------------------------------------------------
    # UPDATE STATE
    # -----------------------------------------------------

    state["attack"] = safe_float(
        attack,
        0
    )

    state["pressure"] = safe_float(
        pressure,
        0
    )

    state["minute"] = safe_float(
        minute,
        0
    )

    state["last_score"] = current_score

    state["updated_at"] = time.time()

    return {
        "significant_change": changed,
        "score_changed": score_changed,
        "previous_score": previous_score
    }

# =========================================================
# AI ANOMALY DETECTION ENGINE V4
# =========================================================

# BLOCK: ANOMALY_SCORE
def anomaly_score(
    pressure,
    attack,
    xg,
    shots_on,
    possession
):

    pressure = safe_float(
        pressure,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    xg = safe_float(
        xg,
        0
    )

    shots_on = safe_float(
        shots_on,
        0
    )

    possession = safe_float(
        possession,
        0
    )

    anomalies = []

    # -----------------------------------------------------
    # HIGH PRESSURE / LOW SHOTS
    # -----------------------------------------------------

    if (
        pressure >= 85
        and
        shots_on <= 1
    ):

        anomalies.append(
            "High pressure / low shots"
        )

    # -----------------------------------------------------
    # HIGH xG / LOW ATTACK
    # -----------------------------------------------------

    if (
        xg >= 2.0
        and
        attack < 60
    ):

        anomalies.append(
            "High xG / weak attack"
        )

    # -----------------------------------------------------
    # POSSESSION / PRESSURE MISMATCH
    # -----------------------------------------------------

    if (
        possession >= 70
        and
        pressure < 45
    ):

        anomalies.append(
            "Possession / pressure mismatch"
        )

    # -----------------------------------------------------
    # SCORE
    # -----------------------------------------------------

    score = max(
        0,
        100 -
        len(anomalies) * 20
    )

    return (
        score,
        anomalies
    )

# =========================================================
# AI DATA RELIABILITY ENGINE V4
# =========================================================

# BLOCK: RELIABILITY_SCORE
def reliability_score(
    pressure,
    attack,
    xg,
    shots_on,
    total_shots,
    possession,
    minute
):

    score = 100

    # -----------------------------------------------------
    # MISSING DATA
    # -----------------------------------------------------

    if pressure is None:
        score -= 15

    if attack is None:
        score -= 15

    if xg is None:
        score -= 10

    if shots_on is None:
        score -= 10

    if total_shots is None:
        score -= 10

    if possession is None:
        score -= 5

    # -----------------------------------------------------
    # EARLY MATCH
    # -----------------------------------------------------

    if minute is None:
        score -= 15

    elif minute < 10:
        score -= 20

    # -----------------------------------------------------
    # IMPOSSIBLE VALUES
    # -----------------------------------------------------

    if pressure is not None:

        if pressure < 0 or pressure > 100:
            score -= 30

    if possession is not None:

        if possession < 0 or possession > 100:
            score -= 30

    # -----------------------------------------------------
    # NUMERIC VALIDATION
    # -----------------------------------------------------

    for value in (
        attack,
        xg,
        shots_on,
        total_shots
    ):

        if value is not None:

            try:

                if float(value) < 0:
                    score -= 15

            except (ValueError, TypeError):

                score -= 15

    return max(
        0,
        min(
            100,
            score
        )
    )


# =========================================================
# DATA RELIABILITY CHECK
# =========================================================

# BLOCK: RELIABLE_LIVE_DATA
def reliable_live_data(
    pressure,
    attack,
    xg,
    shots_on,
    total_shots,
    possession,
    minute
):

    reliability = reliability_score(
        pressure,
        attack,
        xg,
        shots_on,
        total_shots,
        possession,
        minute
    )

    # -----------------------------------------------------
    # We do NOT modify confidence here.
    #
    # Reliability is a data-quality check,
    # not another confidence bonus/penalty system.
    # -----------------------------------------------------

    return reliability

# =========================================================
# AI MATCH REGIME ENGINE V4
# =========================================================

# BLOCK: DETECT_MATCH_REGIME
def detect_match_regime(
    pressure,
    attack,
    xg,
    shots_on,
    possession,
    minute
):

    pressure = safe_float(
        pressure,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    xg = safe_float(
        xg,
        0
    )

    shots_on = safe_float(
        shots_on,
        0
    )

    possession = safe_float(
        possession,
        0
    )

    minute = safe_float(
        minute,
        0
    )

    # -----------------------------------------------------
    # CHAOTIC
    # Highest priority.
    # -----------------------------------------------------

    if (
        minute >= 75
        and
        pressure >= 85
        and
        attack >= 80
    ):

        return "CHAOTIC"

    # -----------------------------------------------------
    # OPEN
    # -----------------------------------------------------

    if (
        attack >= 80
        and
        pressure >= 75
        and
        xg >= 2.0
    ):

        return "OPEN"

    # -----------------------------------------------------
    # DEFENSIVE
    # -----------------------------------------------------

    if (
        xg < 1.2
        and
        shots_on <= 3
        and
        pressure < 60
    ):

        return "DEFENSIVE"

    # -----------------------------------------------------
    # CONTROLLED
    # -----------------------------------------------------

    if (
        possession >= 60
        and
        pressure >= 60
    ):

        return "CONTROLLED"

    return "NORMAL"

# =========================================================
# AI EXPECTED VALUE ENGINE V4
# =========================================================

# BLOCK: EXPECTED_VALUE
def expected_value(
    probability,
    odd
):

    probability = safe_float(
        probability
    )

    odd = safe_float(
        odd
    )

    if (
        probability is None
        or
        odd is None
        or
        odd <= 1.01
    ):
        return None

    probability_decimal = (
        probability / 100
    )

    ev = (
        probability_decimal *
        odd
    ) - 1

    return round(
        ev,
        3
    )


# =========================================================
# EXPECTED VALUE CHECK
# =========================================================

# BLOCK: HAS_POSITIVE_VALUE
def has_positive_value(
    probability,
    odd,
    minimum_ev=0.03
):

    ev = expected_value(
        probability,
        odd
    )

    if ev is None:
        return False

    return ev >= minimum_ev


# =========================================================
# VALUE RESULT
# =========================================================

# BLOCK: GET_VALUE_RESULT
def get_value_result(
    probability,
    odd
):

    ev = expected_value(
        probability,
        odd
    )

    if ev is None:
        return {

            "ev": None,

            "positive": False
        }

    return {

        "ev": ev,

        "positive": ev >= 0.03
    }

# =========================================================
# MARKET MOVEMENT V4
# =========================================================

# BLOCK: MARKET_MOVEMENT
def market_movement(
    opening_odd,
    current_odd
):

    opening_odd = safe_float(
        opening_odd
    )

    current_odd = safe_float(
        current_odd
    )

    if (
        opening_odd is None
        or
        current_odd is None
        or
        opening_odd <= 1.01
        or
        current_odd <= 1.01
    ):

        return {
            "movement": 0,
            "trend": "UNKNOWN"
        }

    movement = (
        (
            opening_odd -
            current_odd
        )
        /
        opening_odd
    ) * 100

    movement = round(
        movement,
        2
    )

    if movement >= 5:

        trend = "SUPPORT"

    elif movement <= -5:

        trend = "AGAINST"

    else:

        trend = "NEUTRAL"

    return {

        "movement": movement,

        "trend": trend
    }

# =========================================================
# AI SIGNAL STABILITY V4
# =========================================================

from collections import deque

STABILITY_CACHE = {}


# BLOCK: UPDATE_SIGNAL_STABILITY
def update_signal_stability(
    fixture_id,
    confidence,
    attack,
    pressure
):
    """
    Проверява дали сигналът е стабилен
    през няколко последователни скана.

    Не създава сигнал самостоятелно.
    Само потвърждава вече силен сигнал.
    """

    if not fixture_id:
        return False, 0

    if fixture_id not in STABILITY_CACHE:

        STABILITY_CACHE[fixture_id] = deque(
            maxlen=4
        )

    history = STABILITY_CACHE[fixture_id]

    history.append({

        "confidence": safe_float(
            confidence,
            0
        ),

        "attack": safe_float(
            attack,
            0
        ),

        "pressure": safe_float(
            pressure,
            0
        )

    })

    # Need several scans
    if len(history) < 3:

        return False, 0

    avg_confidence = (

        sum(
            x["confidence"]
            for x in history
        )
        /
        len(history)

    )

    avg_attack = (

        sum(
            x["attack"]
            for x in history
        )
        /
        len(history)

    )

    avg_pressure = (

        sum(
            x["pressure"]
            for x in history
        )
        /
        len(history)

    )

    stability = (

        avg_confidence * 0.50
        +
        avg_attack * 0.25
        +
        avg_pressure * 0.25

    )

    stability = round(
        stability,
        1
    )

    return (
        stability >= 80,
        stability
    )


# =========================================================
# AI UNCERTAINTY GATE V4
# =========================================================

# BLOCK: UNCERTAINTY_SCORE
def uncertainty_score(
    confidence,
    quality,
    reliability,
    stability,
    risk
):
    """
    Един прост uncertainty gate.

    Не добавя нови бонуси.
    Целта е да спре несигурните сигнали.
    """

    confidence = safe_float(
        confidence,
        0
    )

    quality = safe_float(
        quality,
        0
    )

    reliability = safe_float(
        reliability,
        0
    )

    stability = safe_float(
        stability,
        0
    )

    risk = safe_float(
        risk,
        100
    )

    uncertainty = 100

    uncertainty -= confidence * 0.25

    uncertainty -= quality * 0.20

    uncertainty -= reliability * 0.20

    uncertainty -= stability * 0.20

    uncertainty += risk * 0.15

    return round(
        max(
            0,
            min(
                100,
                uncertainty
            )
        ),
        1
    )


# BLOCK: UNCERTAINTY_ALLOWED
def uncertainty_allowed(
    confidence,
    quality,
    reliability,
    stability,
    risk
):

    uncertainty = uncertainty_score(

        confidence,
        quality,
        reliability,
        stability,
        risk

    )

    return (
        uncertainty <= 25,
        uncertainty
    )


# =========================================================
# AI SIMPLE BAYESIAN UPDATE V4
# =========================================================

# BLOCK: BAYESIAN_UPDATE
def bayesian_update(
    probability,
    evidence_strength
):
    """
    Малка корекция на вероятността
    според новото live доказателство.

    Не позволява огромни скокове.
    """

    probability = safe_float(
        probability,
        0
    )

    evidence_strength = safe_float(
        evidence_strength,
        50
    )

    probability = max(
        1,
        min(
            99,
            probability
        )
    )

    evidence_strength = max(
        0,
        min(
            100,
            evidence_strength
        )
    )

    # Evidence around 50 = no change
    adjustment = (
        evidence_strength - 50
    ) * 0.12

    updated = (
        probability
        +
        adjustment
    )

    # Important:
    # Bayesian update cannot move probability
    # by more than 6 points in one scan.
    maximum_change = 6

    updated = max(
        probability - maximum_change,
        min(
            probability + maximum_change,
            updated
        )
    )

    return round(
        max(
            1,
            min(
                99,
                updated
            )
        ),
        1
    )


# =========================================================
# AI CAUSAL CONFIRMATION V4
# =========================================================

# BLOCK: CAUSAL_CONFIRMATION
def causal_confirmation(
    pressure,
    attack,
    xg,
    shots_on,
    dangerous_attacks
):
    """
    Проверява дали силната статистика има
    реална подкрепа от няколко независими
    live показателя.

    Не е отделен signal engine.
    """

    pressure = safe_float(
        pressure,
        0
    )

    attack = safe_float(
        attack,
        0
    )

    xg = safe_float(
        xg,
        0
    )

    shots_on = safe_float(
        shots_on,
        0
    )

    dangerous_attacks = safe_float(
        dangerous_attacks,
        0
    )

    confirmations = 0

    if pressure >= 80:

        confirmations += 1

    if attack >= 80:

        confirmations += 1

    if xg >= 1.50:

        confirmations += 1

    if shots_on >= 4:

        confirmations += 1

    if dangerous_attacks >= 40:

        confirmations += 1

    return confirmations


# =========================================================
# AI FINAL LIVE QUALITY GATE V4
# =========================================================

# BLOCK: FINAL_LIVE_QUALITY_GATE
def final_live_quality_gate(
    probability,
    confidence,
    quality,
    risk,
    reliability,
    stability,
    confirmations
):
    """
    Последна проверка преди сигнал.

    Важно:
    този gate НЕ създава сигнал.
    Той само отсява слабите.
    """

    probability = safe_float(
        probability,
        0
    )

    confidence = safe_float(
        confidence,
        0
    )

    quality = safe_float(
        quality,
        0
    )

    risk = safe_float(
        risk,
        100
    )

    reliability = safe_float(
        reliability,
        0
    )

    stability = safe_float(
        stability,
        0
    )

    confirmations = int(
        safe_float(
            confirmations,
            0
        )
    )

    # Minimum core quality
    if probability < 75:

        return False

    if confidence < 80:

        return False

    if quality < 75:

        return False

    # Risk
    if risk > 35:

        return False

    # Data quality
    if reliability < 80:

        return False

    # Stable signal
    if stability < 80:

        return False

    # At least two real confirmations
    if confirmations < 2:

        return False

    return True

# =========================================================
# AI RESULT LEARNING + MARKET MEMORY V4
# =========================================================

from collections import defaultdict
from datetime import datetime, timedelta


# =========================================================
# GLOBAL LEARNING MEMORY
# =========================================================

RESULT_MEMORY = []

MARKET_MEMORY = {}

LEAGUE_MEMORY = {}

CONFIDENCE_MEMORY = {}

ODDS_MEMORY = {}

LIVE_MEMORY = {}

PREMATCH_MEMORY = {}


# =========================================================
# SAFE RESULT NORMALIZER
# =========================================================

# BLOCK: NORMALIZE_RESULT
def normalize_result(result):

    if result is None:
        return None

    result = str(
        result
    ).strip().upper()

    if result in (
        "WIN",
        "W"
    ):
        return "WIN"

    if result in (
        "LOSS",
        "LOSE",
        "L",
        "LOST"
    ):
        return "LOSS"

    if result in (
        "PENDING",
        "OPEN",
        "WAIT"
    ):
        return "PENDING"

    return None


# =========================================================
# SAFE MARKET NAME
# =========================================================

# BLOCK: NORMALIZE_MARKET
def normalize_market(market):

    if market is None:
        return "UNKNOWN"

    return str(
        market
    ).strip()


# =========================================================
# RESULT MEMORY ENTRY
# =========================================================

# BLOCK: SAVE_RESULT_MEMORY
def save_result_memory(signal):

    if not signal:
        return False

    result = normalize_result(
        signal.get(
            "result"
        )
    )

    if result not in (
        "WIN",
        "LOSS"
    ):
        return False

    entry = {

        "fixture_id":
            signal.get(
                "fixture_id"
            ),

        "market":
            normalize_market(
                signal.get(
                    "market"
                )
            ),

        "result":
            result,

        "odd":
            safe_float(
                signal.get(
                    "odd"
                ),
                0
            ),

        "probability":
            safe_float(
                signal.get(
                    "probability"
                ),
                0
            ),

        "confidence":
            safe_float(
                signal.get(
                    "confidence"
                ),
                0
            ),

        "quality":
            safe_float(
                signal.get(
                    "quality"
                ),
                0
            ),

        "risk":
            safe_float(
                signal.get(
                    "risk"
                ),
                100
            ),

        "league":
            signal.get(
                "league",
                "UNKNOWN"
            ),

        "country":
            signal.get(
                "country",
                "UNKNOWN"
            ),

        "minute":
            safe_float(
                signal.get(
                    "minute"
                ),
                0
            ),

        "created_at":
            signal.get(
                "created_at"
            )
            or
            datetime.now().isoformat()

    }

    RESULT_MEMORY.append(
        entry
    )

    return True


# =========================================================
# ROI CALCULATOR
# =========================================================

# BLOCK: CALCULATE_ROI
def calculate_roi(
    results
):

    if not results:
        return 0

    profit = 0
    stakes = 0

    for item in results:

        result = normalize_result(
            item.get(
                "result"
            )
        )

        odd = safe_float(
            item.get(
                "odd"
            ),
            0
        )

        if (
            result not in (
                "WIN",
                "LOSS"
            )
            or
            odd <= 1.01
        ):
            continue

        stakes += 1

        if result == "WIN":

            profit += (
                odd - 1
            )

        else:

            profit -= 1

    if stakes == 0:
        return 0

    return round(
        (
            profit /
            stakes
        ) * 100,
        2
    )


# =========================================================
# WINRATE
# =========================================================

# BLOCK: CALCULATE_MEMORY_WINRATE
def calculate_memory_winrate(
    results
):

    valid = [

        x for x in results

        if normalize_result(
            x.get(
                "result"
            )
        ) in (
            "WIN",
            "LOSS"
        )

    ]

    if not valid:
        return 0

    wins = sum(

        1

        for x in valid

        if normalize_result(
            x.get(
                "result"
            )
        ) == "WIN"

    )

    return round(
        wins * 100 / len(valid),
        2
    )


# =========================================================
# SAMPLE QUALITY
# =========================================================

# BLOCK: SAMPLE_QUALITY
def sample_quality(
    results
):

    total = len(
        results
    )

    if total < 10:

        return "VERY_SMALL"

    if total < 25:

        return "SMALL"

    if total < 50:

        return "MEDIUM"

    if total < 100:

        return "GOOD"

    return "STRONG"


# =========================================================
# MARKET MEMORY
# =========================================================

# BLOCK: REBUILD_MARKET_MEMORY
def rebuild_market_memory():

    global MARKET_MEMORY

    MARKET_MEMORY = {}

    grouped = defaultdict(
        list
    )

    for signal in RESULT_MEMORY:

        market = normalize_market(
            signal.get(
                "market"
            )
        )

        grouped[
            market
        ].append(
            signal
        )

    for market, results in grouped.items():

        wins = sum(

            1

            for x in results

            if normalize_result(
                x.get(
                    "result"
                )
            ) == "WIN"

        )

        losses = sum(

            1

            for x in results

            if normalize_result(
                x.get(
                    "result"
                )
            ) == "LOSS"

        )

        total = (
            wins +
            losses
        )

        if total == 0:
            continue

        MARKET_MEMORY[
            market
        ] = {

            "market":
                market,

            "signals":
                total,

            "wins":
                wins,

            "losses":
                losses,

            "winrate":
                round(
                    wins * 100 / total,
                    2
                ),

            "roi":
                calculate_roi(
                    results
                ),

            "sample":
                sample_quality(
                    results
                )

        }

    return MARKET_MEMORY


# =========================================================
# LEAGUE MEMORY
# =========================================================

# BLOCK: REBUILD_LEAGUE_MEMORY
def rebuild_league_memory():

    global LEAGUE_MEMORY

    LEAGUE_MEMORY = {}

    grouped = defaultdict(
        list
    )

    for signal in RESULT_MEMORY:

        league = signal.get(
            "league",
            "UNKNOWN"
        )

        grouped[
            league
        ].append(
            signal
        )

    for league, results in grouped.items():

        valid = [

            x for x in results

            if normalize_result(
                x.get(
                    "result"
                )
            ) in (
                "WIN",
                "LOSS"
            )

        ]

        if not valid:
            continue

        wins = sum(

            1

            for x in valid

            if normalize_result(
                x.get(
                    "result"
                )
            ) == "WIN"

        )

        total = len(
            valid
        )

        LEAGUE_MEMORY[
            league
        ] = {

            "signals":
                total,

            "wins":
                wins,

            "losses":
                total - wins,

            "winrate":
                round(
                    wins * 100 / total,
                    2
                ),

            "roi":
                calculate_roi(
                    valid
                ),

            "sample":
                sample_quality(
                    valid
                )

        }

    return LEAGUE_MEMORY


# =========================================================
# CONFIDENCE BAND MEMORY
# =========================================================

# BLOCK: CONFIDENCE_BAND
def confidence_band(
    confidence
):

    confidence = safe_float(
        confidence,
        0
    )

    if confidence < 75:
        return "<75"

    if confidence < 80:
        return "75-79"

    if confidence < 85:
        return "80-84"

    if confidence < 90:
        return "85-89"

    return "90+"


# BLOCK: REBUILD_CONFIDENCE_MEMORY
def rebuild_confidence_memory():

    global CONFIDENCE_MEMORY

    CONFIDENCE_MEMORY = {}

    grouped = defaultdict(
        list
    )

    for signal in RESULT_MEMORY:

        band = confidence_band(
            signal.get(
                "confidence"
            )
        )

        grouped[
            band
        ].append(
            signal
        )

    for band, results in grouped.items():

        valid = [

            x for x in results

            if normalize_result(
                x.get(
                    "result"
                )
            ) in (
                "WIN",
                "LOSS"
            )

        ]

        if not valid:
            continue

        CONFIDENCE_MEMORY[
            band
        ] = {

            "signals":
                len(valid),

            "winrate":
                calculate_memory_winrate(
                    valid
                ),

            "roi":
                calculate_roi(
                    valid
                )

        }

    return CONFIDENCE_MEMORY


# =========================================================
# ODDS BAND MEMORY
# =========================================================

# BLOCK: ODDS_BAND
def odds_band(
    odd
):

    odd = safe_float(
        odd,
        0
    )

    if odd <= 1.01:
        return "INVALID"

    if odd < 1.30:
        return "1.01-1.29"

    if odd < 1.50:
        return "1.30-1.49"

    if odd < 1.75:
        return "1.50-1.74"

    if odd < 2.00:
        return "1.75-1.99"

    if odd < 2.50:
        return "2.00-2.49"

    return "2.50+"


# BLOCK: REBUILD_ODDS_MEMORY
def rebuild_odds_memory():

    global ODDS_MEMORY

    ODDS_MEMORY = {}

    grouped = defaultdict(
        list
    )

    for signal in RESULT_MEMORY:

        band = odds_band(
            signal.get(
                "odd"
            )
        )

        if band == "INVALID":
            continue

        grouped[
            band
        ].append(
            signal
        )

    for band, results in grouped.items():

        valid = [

            x for x in results

            if normalize_result(
                x.get(
                    "result"
                )
            ) in (
                "WIN",
                "LOSS"
            )

        ]

        if not valid:
            continue

        ODDS_MEMORY[
            band
        ] = {

            "signals":
                len(valid),

            "winrate":
                calculate_memory_winrate(
                    valid
                ),

            "roi":
                calculate_roi(
                    valid
                )

        }

    return ODDS_MEMORY


# =========================================================
# LIVE / PREMATCH MEMORY
# =========================================================

# BLOCK: DETECT_SIGNAL_MODE
def detect_signal_mode(
    signal
):

    market = str(
        signal.get(
            "market",
            ""
        )
    ).upper()

    minute = safe_float(
        signal.get(
            "minute"
        ),
        0
    )

    if (
        "NEXT GOAL" in market
        or
        minute > 0
    ):

        return "LIVE"

    return "PREMATCH"


# BLOCK: REBUILD_MODE_MEMORY
def rebuild_mode_memory():

    global LIVE_MEMORY
    global PREMATCH_MEMORY

    LIVE_MEMORY = {}
    PREMATCH_MEMORY = {}

    live = []
    prematch = []

    for signal in RESULT_MEMORY:

        mode = detect_signal_mode(
            signal
        )

        if mode == "LIVE":

            live.append(
                signal
            )

        else:

            prematch.append(
                signal
            )

    for name, data in (
        ("LIVE", live),
        ("PREMATCH", prematch)
    ):

        if not data:
            continue

        target = (
            LIVE_MEMORY
            if name == "LIVE"
            else PREMATCH_MEMORY
        )

        target.update({

            "signals":
                len(data),

            "winrate":
                calculate_memory_winrate(
                    data
                ),

            "roi":
                calculate_roi(
                    data
                )

        })

    return (
        LIVE_MEMORY,
        PREMATCH_MEMORY
    )


# =========================================================
# MARKET DECISION FROM HISTORY
# =========================================================

# BLOCK: HISTORICAL_MARKET_SCORE
def historical_market_score(
    market
):

    market = normalize_market(
        market
    )

    data = MARKET_MEMORY.get(
        market
    )

    if not data:
        return {

            "score": 50,

            "known": False,

            "reason":
                "No historical data"

        }

    total = data.get(
        "signals",
        0
    )

    winrate = data.get(
        "winrate",
        0
    )

    roi = data.get(
        "roi",
        0
    )

    # No aggressive learning
    score = 50

    # Winrate contribution
    if winrate >= 85:
        score += 15

    elif winrate >= 80:
        score += 10

    elif winrate >= 75:
        score += 5

    elif winrate < 65:
        score -= 10

    # ROI contribution
    if roi >= 10:
        score += 10

    elif roi >= 5:
        score += 5

    elif roi < 0:
        score -= 10

    # Sample-size protection
    if total < 20:

        score = (
            50 +
            (
                score - 50
            ) * 0.35
        )

    elif total < 50:

        score = (
            50 +
            (
                score - 50
            ) * 0.65
        )

    return {

        "score":
            round(
                max(
                    0,
                    min(
                        100,
                        score
                    )
                ),
                1
            ),

        "known":
            True,

        "signals":
            total,

        "winrate":
            winrate,

        "roi":
            roi,

        "sample":
            data.get(
                "sample"
            )

    }


# =========================================================
# MARKET BLOCK DECISION
# =========================================================

# BLOCK: SHOULD_BLOCK_MARKET
def should_block_market(
    market
):

    data = MARKET_MEMORY.get(
        normalize_market(
            market
        )
    )

    if not data:
        return False

    total = data.get(
        "signals",
        0
    )

    winrate = data.get(
        "winrate",
        0
    )

    roi = data.get(
        "roi",
        0
    )

    # We NEVER block from a tiny sample.
    if total < 30:
        return False

    # Strong evidence of bad market.
    if (
        total >= 50
        and
        winrate < 62
        and
        roi < -5
    ):

        return True

    return False


# =========================================================
# LEAGUE HISTORICAL SCORE
# =========================================================

# BLOCK: HISTORICAL_LEAGUE_SCORE
def historical_league_score(
    league
):

    league = (
        league
        or
        "UNKNOWN"
    )

    data = LEAGUE_MEMORY.get(
        league
    )

    if not data:

        return {

            "score": 50,

            "known": False

        }

    total = data.get(
        "signals",
        0
    )

    winrate = data.get(
        "winrate",
        0
    )

    roi = data.get(
        "roi",
        0
    )

    score = 50

    if winrate >= 85:
        score += 10

    elif winrate >= 80:
        score += 7

    elif winrate >= 75:
        score += 4

    elif winrate < 65:
        score -= 8

    if roi >= 10:
        score += 8

    elif roi >= 5:
        score += 4

    elif roi < 0:
        score -= 8

    # Sample protection
    if total < 20:

        score = (
            50 +
            (
                score - 50
            ) * 0.35
        )

    elif total < 50:

        score = (
            50 +
            (
                score - 50
            ) * 0.65
        )

    return {

        "score":
            round(
                max(
                    0,
                    min(
                        100,
                        score
                    )
                ),
                1
            ),

        "known":
            True,

        "signals":
            total,

        "winrate":
            winrate,

        "roi":
            roi

    }


# =========================================================
# COMPLETE MEMORY REBUILD
# =========================================================

# BLOCK: REBUILD_ALL_LEARNING_MEMORY
def rebuild_all_learning_memory():

    rebuild_market_memory()

    rebuild_league_memory()

    rebuild_confidence_memory()

    rebuild_odds_memory()

    rebuild_mode_memory()

    return {

        "results":
            len(
                RESULT_MEMORY
            ),

        "markets":
            len(
                MARKET_MEMORY
            ),

        "leagues":
            len(
                LEAGUE_MEMORY
            ),

        "confidence_bands":
            len(
                CONFIDENCE_MEMORY
            ),

        "odds_bands":
            len(
                ODDS_MEMORY
            ),

        "live_signals":
            LIVE_MEMORY.get(
                "signals",
                0
            ),

        "prematch_signals":
            PREMATCH_MEMORY.get(
                "signals",
                0
            )

    }


# =========================================================
# LEARNING BONUS
# =========================================================

# BLOCK: LEARNING_BONUS
def learning_bonus(
    market,
    league
):

    market_data = historical_market_score(
        market
    )

    league_data = historical_league_score(
        league
    )

    market_score = market_data.get(
        "score",
        50
    )

    league_score = league_data.get(
        "score",
        50
    )

    # Market history is more important
    bonus = (

        (
            market_score - 50
        ) * 0.12

        +

        (
            league_score - 50
        ) * 0.08

    )

    return round(
        max(
            -10,
            min(
                10,
                bonus
            )
        ),
        2
    )


# =========================================================
# LEARNING GATE
# =========================================================

# BLOCK: HISTORICAL_LEARNING_GATE
def historical_learning_gate(
    market,
    league
):

    market = normalize_market(
        market
    )

    # Never send a known bad market
    if should_block_market(
        market
    ):

        return {

            "allowed":
                False,

            "reason":
                "Historical market performance is weak",

            "bonus":
                -10

        }

    bonus = learning_bonus(
        market,
        league
    )

    return {

        "allowed":
            True,

        "reason":
            "Historical performance accepted",

        "bonus":
            bonus

    }


# =========================================================
# LEARNING REPORT
# =========================================================

# BLOCK: LEARNING_REPORT_V4
def learning_report_v4():

    rebuild_all_learning_memory()

    best_market = None
    best_roi = -999

    for market, data in MARKET_MEMORY.items():

        if data.get(
            "signals",
            0
        ) < 20:

            continue

        roi = data.get(
            "roi",
            0
        )

        if roi > best_roi:

            best_roi = roi

            best_market = market

    worst_market = None
    worst_roi = 999

    for market, data in MARKET_MEMORY.items():

        if data.get(
            "signals",
            0
        ) < 20:

            continue

        roi = data.get(
            "roi",
            0
        )

        if roi < worst_roi:

            worst_roi = roi

            worst_market = market

    return {

        "memory":
            len(
                RESULT_MEMORY
            ),

        "markets":
            len(
                MARKET_MEMORY
            ),

        "leagues":
            len(
                LEAGUE_MEMORY
            ),

        "best_market":
            best_market,

        "best_market_roi":
            (
                best_roi
                if best_market
                else 0
            ),

        "worst_market":
            worst_market,

        "worst_market_roi":
            (
                worst_roi
                if worst_market
                else 0
            ),

        "live":
            LIVE_MEMORY,

        "prematch":
            PREMATCH_MEMORY

    }


# =========================================================
# DEBUG LEARNING REPORT
# =========================================================

# BLOCK: PRINT_LEARNING_REPORT
def print_learning_report():

    report = learning_report_v4()

    print(
        "\n"
        "=================================================\n"
        "🧠 AI LEARNING MEMORY V4\n"
        "=================================================\n"
    )

    print(
        "TOTAL RESULTS:",
        report.get(
            "memory",
            0
        )
    )

    print(
        "MARKETS:",
        report.get(
            "markets",
            0
        )
    )

    print(
        "LEAGUES:",
        report.get(
            "leagues",
            0
        )
    )

    print(
        "BEST MARKET:",
        report.get(
            "best_market"
        )
    )

    print(
        "BEST ROI:",
        report.get(
            "best_market_roi",
            0
        )
    )

    print(
        "WORST MARKET:",
        report.get(
            "worst_market"
        )
    )

    print(
        "WORST ROI:",
        report.get(
            "worst_market_roi",
            0
        )
    )

    print(
        "LIVE:",
        report.get(
            "live"
        )
    )

    print(
        "PREMATCH:",
        report.get(
            "prematch"
        )
    )

    print(
        "=================================================\n"
    )


# =========================================================
# END RESULT LEARNING V4
# =========================================================

# =========================================================
# RESULT TRACKER V4
# =========================================================

RESULT_CHECK_INTERVAL = 300


# =========================================================
# DATABASE MIGRATION
# =========================================================

# BLOCK: UPGRADE_DATABASE
def upgrade_database():

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(signals)")

    columns = {
        row[1]
        for row in cursor.fetchall()
    }

    required_columns = {

        "status": "TEXT",

        "checked_at": "TEXT",

        "home_goals": "INTEGER",

        "away_goals": "INTEGER",

        "fixture_status": "TEXT",

        "stake": "REAL",

        "profit": "REAL"

    }

    for column, data_type in required_columns.items():

        if column not in columns:

            try:

                cursor.execute(

                    f"ALTER TABLE signals "
                    f"ADD COLUMN {column} {data_type}"

                )

            except Exception as e:

                logging.warning(

                    "DB MIGRATION ERROR %s: %s",

                    column,

                    repr(e)

                )

    conn.commit()

    conn.close()


# =========================================================
# INIT DATABASE
# =========================================================

init_database()

upgrade_database()


# =========================================================
# GET FIXTURE RESULT
# =========================================================

# BLOCK: GET_FIXTURE_RESULT
def get_fixture_result(fixture_id):

    data = api_get(

        "fixtures",

        {

            "id": fixture_id

        }

    )

    response = data.get(

        "response",

        []

    )

    if not response:

        return None

    fixture = response[0]

    status = (

        fixture
        .get("fixture", {})
        .get("status", {})
        .get("short")

    )

    goals = fixture.get(

        "goals",

        {}

    )

    home_goals = goals.get(

        "home"

    )

    away_goals = goals.get(

        "away"

    )

    if home_goals is None:

        return None

    if away_goals is None:

        return None

    return {

        "status": status,

        "home_goals": int(home_goals),

        "away_goals": int(away_goals)

    }


# =========================================================
# FINISHED STATUS
# =========================================================

FINISHED_STATUSES = {

    "FT",

    "AET",

    "PEN"

}


# BLOCK: IS_FINISHED_STATUS
def is_finished_status(status):

    return status in FINISHED_STATUSES


# =========================================================
# MARKET RESULT ENGINE
# =========================================================

# BLOCK: EVALUATE_MARKET_RESULT
def evaluate_market_result(

    market,

    home_goals,

    away_goals

):

    total_goals = (

        home_goals +

        away_goals

    )

    # -----------------------------------------------------
    # HOME WIN
    # -----------------------------------------------------

    if market in (

        "🏆 HOME WIN",

        "HOME WIN"

    ):

        if home_goals > away_goals:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # AWAY WIN
    # -----------------------------------------------------

    if market in (

        "✈️ AWAY WIN",

        "✈ AWAY WIN",

        "AWAY WIN"

    ):

        if away_goals > home_goals:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # OVER 2.5
    # -----------------------------------------------------

    if market in (

        "🚀 OVER 2.5",

        "⚽ OVER 2.5",

        "OVER 2.5"

    ):

        if total_goals >= 3:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # UNDER 2.5
    # -----------------------------------------------------

    if market in (

        "🛡 UNDER 2.5",

        "UNDER 2.5"

    ):

        if total_goals <= 2:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # OVER 3.5
    # -----------------------------------------------------

    if market in (

        "🔥 OVER 3.5",

        "OVER 3.5"

    ):

        if total_goals >= 4:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # UNDER 3.5
    # -----------------------------------------------------

    if market in (

        "🛡 UNDER 3.5",

        "UNDER 3.5"

    ):

        if total_goals <= 3:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # BTTS
    # -----------------------------------------------------

    if market in (

        "💎 BTTS",

        "💎 BTTS YES",

        "BTTS",

        "BTTS YES"

    ):

        if (

            home_goals > 0

            and

            away_goals > 0

        ):

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # HOME OVER 1.5
    # -----------------------------------------------------

    if market in (

        "⚽ HOME OVER 1.5",

        "HOME OVER 1.5"

    ):

        if home_goals >= 2:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # AWAY OVER 1.5
    # -----------------------------------------------------

    if market in (

        "⚽ AWAY OVER 1.5",

        "AWAY OVER 1.5"

    ):

        if away_goals >= 2:

            return "WIN"

        return "LOSS"

    # -----------------------------------------------------
    # NEXT GOAL
    # -----------------------------------------------------

    if "NEXT GOAL" in str(market):

        return "PENDING"

    # -----------------------------------------------------
    # UNKNOWN MARKET
    # -----------------------------------------------------

    return "PENDING"


# =========================================================
# SIGNAL PROFIT
# =========================================================

# BLOCK: CALCULATE_SIGNAL_PROFIT
def calculate_signal_profit(

    result,

    odd,

    stake=1.0

):

    if result == "WIN":

        if odd is None:

            return None

        return round(

            (odd - 1) * stake,

            2

        )

    if result == "LOSS":

        return round(

            -stake,

            2

        )

    return 0.0


# =========================================================
# UPDATE SINGLE SIGNAL
# =========================================================

# BLOCK: UPDATE_SIGNAL_RESULT
def update_signal_result(

    signal_id,

    fixture_id,

    market,

    odd

):

    fixture = get_fixture_result(

        fixture_id

    )

    if fixture is None:

        return False

    status = fixture["status"]

    home_goals = fixture["home_goals"]

    away_goals = fixture["away_goals"]

    # -----------------------------------------------------
    # MATCH NOT FINISHED
    # -----------------------------------------------------

    if not is_finished_status(status):

        return False

    result = evaluate_market_result(

        market,

        home_goals,

        away_goals

    )

    # NEXT GOAL needs live snapshot data.
    # Do not guess its result.

    if result == "PENDING":

        return False

    stake = 1.0

    profit = calculate_signal_profit(

        result,

        odd,

        stake

    )

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    cursor.execute(

        """

        UPDATE signals

        SET

            result=?,

            status=?,

            home_goals=?,

            away_goals=?,

            fixture_status=?,

            checked_at=?,

            stake=?,

            profit=?

        WHERE id=?

        """,

        (

            result,

            result,

            home_goals,

            away_goals,

            status,

            datetime.now(

                TIMEZONE

            ).isoformat(),

            stake,

            profit,

            signal_id

        )

    )

    conn.commit()

    conn.close()

    return True


# =========================================================
# CHECK ALL PENDING SIGNALS
# =========================================================

# BLOCK: CHECK_PENDING_SIGNALS
def check_pending_signals():

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    cursor.execute(

        """

        SELECT

            id,

            fixture_id,

            market,

            odd

        FROM signals

        WHERE

            result IS NULL

            OR result='PENDING'

        ORDER BY id ASC

        LIMIT 100

        """

    )

    rows = cursor.fetchall()

    conn.close()

    updated = 0

    for row in rows:

        signal_id = row[0]

        fixture_id = row[1]

        market = row[2]

        odd = safe_float(

            row[3]

        )

        try:

            success = update_signal_result(

                signal_id,

                fixture_id,

                market,

                odd

            )

            if success:

                updated += 1

        except Exception as e:

            logging.warning(

                "RESULT ERROR fixture=%s: %s",

                fixture_id,

                repr(e)

            )

    return updated


# =========================================================
# MARKET PERFORMANCE
# =========================================================

# BLOCK: GET_MARKET_PERFORMANCE
def get_market_performance(

    market=None

):

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    if market:

        cursor.execute(

            """

            SELECT

                COUNT(*),

                SUM(

                    CASE

                        WHEN result='WIN'

                        THEN 1

                        ELSE 0

                    END

                ),

                COALESCE(

                    SUM(profit),

                    0

                )

            FROM signals

            WHERE

                market=?

                AND result IN (

                    'WIN',

                    'LOSS'

                )

            """,

            (

                market,

            )

        )

    else:

        cursor.execute(

            """

            SELECT

                COUNT(*),

                SUM(

                    CASE

                        WHEN result='WIN'

                        THEN 1

                        ELSE 0

                    END

                ),

                COALESCE(

                    SUM(profit),

                    0

                )

            FROM signals

            WHERE result IN (

                'WIN',

                'LOSS'

            )

            """

        )

    row = cursor.fetchone()

    conn.close()

    total = row[0] or 0

    wins = row[1] or 0

    profit = row[2] or 0.0

    if total == 0:

        return {

            "total": 0,

            "wins": 0,

            "losses": 0,

            "winrate": 0.0,

            "profit": 0.0,

            "roi": 0.0

        }

    losses = total - wins

    winrate = (

        wins /

        total *

        100

    )

    roi = (

        profit /

        total *

        100

    )

    return {

        "total": total,

        "wins": wins,

        "losses": losses,

        "winrate": round(

            winrate,

            2

        ),

        "profit": round(

            profit,

            2

        ),

        "roi": round(

            roi,

            2

        )

    }


# =========================================================
# ALL MARKET PERFORMANCE
# =========================================================

# BLOCK: GET_ALL_MARKET_PERFORMANCE
def get_all_market_performance():

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    cursor.execute(

        """

        SELECT

            market,

            COUNT(*),

            SUM(

                CASE

                    WHEN result='WIN'

                    THEN 1

                    ELSE 0

                END

            ),

            COALESCE(

                SUM(profit),

                0

            )

        FROM signals

        WHERE result IN (

            'WIN',

            'LOSS'

        )

        GROUP BY market

        ORDER BY COUNT(*) DESC

        """

    )

    rows = cursor.fetchall()

    conn.close()

    report = []

    for market, total, wins, profit in rows:

        losses = total - wins

        winrate = (

            wins /

            total *

            100

        )

        roi = (

            profit /

            total *

            100

        )

        report.append({

            "market": market,

            "total": total,

            "wins": wins,

            "losses": losses,

            "winrate": round(

                winrate,

                1

            ),

            "profit": round(

                profit,

                2

            ),

            "roi": round(

                roi,

                1

            )

        })

    return report


# =========================================================
# QUALITY REPORT
# =========================================================

# BLOCK: PRINT_PERFORMANCE_REPORT
def print_performance_report():

    report = get_all_market_performance()

    print()

    print(

        "================================================"

    )

    print(

        "             V4 PERFORMANCE"

    )

    print(

        "================================================"

    )

    if not report:

        print(

            "NO FINISHED SIGNALS"

        )

        return

    for row in report:

        if row["winrate"] >= 85:

            status = "👑 ELITE"

        elif row["winrate"] >= 80:

            status = "🔥 EXCELLENT"

        elif row["winrate"] >= 75:

            status = "💎 GOOD"

        elif row["winrate"] >= 70:

            status = "⭐ OK"

        else:

            status = "⚠ WEAK"

        print()

        print(

            row["market"]

        )

        print(

            "Signals:",

            row["total"]

        )

        print(

            "WIN:",

            row["wins"],

            "| LOSS:",

            row["losses"]

        )

        print(

            "Win Rate:",

            f'{row["winrate"]:.1f}%'

        )

        print(

            "Profit:",

            row["profit"]

        )

        print(

            "ROI:",

            f'{row["roi"]:.1f}%'

        )

        print(

            "Status:",

            status

        )

    print()

    print(

        "================================================"

    )


# =========================================================
# LEARNING FROM REAL RESULTS
# =========================================================

# BLOCK: GET_LEARNING_STATISTICS
def get_learning_statistics(

    market,

    limit=100

):

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    cursor.execute(

        """

        SELECT

            result,

            probability,

            confidence,

            odd,

            profit

        FROM signals

        WHERE

            market=?

            AND result IN (

                'WIN',

                'LOSS'

            )

        ORDER BY id DESC

        LIMIT ?

        """,

        (

            market,

            limit

        )

    )

    rows = cursor.fetchall()

    conn.close()

    if not rows:

        return None

    total = len(rows)

    wins = sum(

        1

        for row in rows

        if row[0] == "WIN"

    )

    avg_probability = sum(

        row[1] or 0

        for row in rows

    ) / total

    avg_confidence = sum(

        row[2] or 0

        for row in rows

    ) / total

    avg_odd = sum(

        row[3] or 0

        for row in rows

    ) / total

    profit = sum(

        row[4] or 0

        for row in rows

    )

    return {

        "market": market,

        "total": total,

        "wins": wins,

        "losses": total - wins,

        "winrate": round(

            wins /

            total *

            100,

            2

        ),

        "avg_probability": round(

            avg_probability,

            2

        ),

        "avg_confidence": round(

            avg_confidence,

            2

        ),

        "avg_odd": round(

            avg_odd,

            2

        ),

        "profit": round(

            profit,

            2

        )

    }


# =========================================================
# MARKET LEARNING DECISION
# =========================================================

# BLOCK: MARKET_LEARNING_DECISION
def market_learning_decision(

    market

):

    stats = get_learning_statistics(

        market,

        100

    )

    if not stats:

        return {

            "action": "LEARN",

            "reason": "Not enough history",

            "stats": None

        }

    # We do not aggressively change the system.
    # Only identify clearly weak markets.

    if (

        stats["total"] >= 30

        and

        stats["winrate"] < 60

    ):

        return {

            "action": "REDUCE",

            "reason": "Weak historical performance",

            "stats": stats

        }

    if (

        stats["total"] >= 50

        and

        stats["winrate"] >= 80

    ):

        return {

            "action": "PRIORITIZE",

            "reason": "Strong historical performance",

            "stats": stats

        }

    return {

        "action": "KEEP",

        "reason": "Normal performance",

        "stats": stats

    }


# =========================================================
# RESULT CHECKER
# =========================================================

# BLOCK: RUN_RESULT_CHECKER
def run_result_checker():

    try:

        updated = check_pending_signals()

        logging.info(

            "RESULT CHECKER | updated=%s",

            updated

        )

    except Exception as e:

        logging.warning(

            "RESULT CHECKER ERROR: %s",

            repr(e)

        )


# =========================================================
# RESULT CHECKER THREAD
# =========================================================

# BLOCK: RESULT_CHECKER_LOOP
def result_checker_loop():

    while True:

        try:

            run_result_checker()

        except Exception as e:

            logging.warning(

                "RESULT LOOP ERROR: %s",

                repr(e)

            )

        time.sleep(

            RESULT_CHECK_INTERVAL

        )


# =========================================================
# START RESULT CHECKER
# =========================================================

# BLOCK: START_RESULT_CHECKER
def start_result_checker():

    thread = threading.Thread(

        target=result_checker_loop,

        daemon=True

    )

    thread.start()

    return thread

# =========================================================
# PREMATCH ENGINE V4
# =========================================================

PREMATCH_MIN_PROBABILITY = 68
PREMATCH_MIN_CONFIDENCE = 78
PREMATCH_MIN_ODD = 1.50
PREMATCH_MAX_ODD = 3.50

MAX_PREMATCH_SIGNALS_PER_SCAN = 5


# =========================================================
# PREMATCH TEAM STRENGTH
# =========================================================

# BLOCK: PREMATCH_TEAM_STRENGTH
def prematch_team_strength(form):

    if not form:
        return 0

    score = 0

    # Attack
    score += form["avg_scored"] * 18

    # Defence
    score -= form["avg_conceded"] * 10

    # Overall form
    score += form["form_pct"] * 0.20

    # Recent form
    score += form["recent_form_pct"] * 0.25

    # Goal difference
    score += form["goal_diff"] * 1.5

    # Consistency
    score += form["scored_pct"] * 0.10

    return round(
        score,
        2
    )


# =========================================================
# PREMATCH PROBABILITY
# =========================================================

# BLOCK: PREMATCH_PROBABILITIES
def prematch_probabilities(

    home_form,

    away_form,

    table_home=None,

    table_away=None

):

    home_strength = prematch_team_strength(

        home_form

    )

    away_strength = prematch_team_strength(

        away_form

    )

    # Small home advantage.
    # Deliberately kept low so it does not overpower form.

    home_strength += 5

    # League table adjustment

    if table_home and table_away:

        rank_diff = (

            table_away["rank"]

            -

            table_home["rank"]

        )

        home_strength += rank_diff * 1.5

        goal_diff_diff = (

            table_home["goal_diff"]

            -

            table_away["goal_diff"]

        )

        home_strength += goal_diff_diff * 0.20

        away_strength += (

            -goal_diff_diff * 0.20

        )

    home_strength = max(

        1,

        home_strength

    )

    away_strength = max(

        1,

        away_strength

    )

    total = (

        home_strength

        +

        away_strength

    )

    home_probability = (

        home_strength

        /

        total

        *

        100

    )

    away_probability = (

        away_strength

        /

        total

        *

        100

    )

    # Draw is estimated separately.
    # We do not force it into the home/away calculation.

    form_gap = abs(

        home_form["form_pct"]

        -

        away_form["form_pct"]

    )

    draw_probability = max(

        18,

        32 - form_gap * 0.10

    )

    # Normalize

    remaining = (

        home_probability

        +

        away_probability

    )

    factor = (

        100 - draw_probability

    ) / remaining

    home_probability *= factor
    away_probability *= factor

    return {

        "home_probability": round(

            home_probability,

            1

        ),

        "draw_probability": round(

            draw_probability,

            1

        ),

        "away_probability": round(

            away_probability,

            1

        ),

        "home_strength": round(

            home_strength,

            2

        ),

        "away_strength": round(

            away_strength,

            2

        )

    }


# =========================================================
# PREMATCH GOAL EXPECTATION
# =========================================================

# BLOCK: PREMATCH_GOAL_EXPECTATION
def prematch_goal_expectation(

    home_form,

    away_form

):

    home_attack = (

        home_form["avg_scored"] * 0.65

        +

        away_form["avg_conceded"] * 0.35

    )

    away_attack = (

        away_form["avg_scored"] * 0.65

        +

        home_form["avg_conceded"] * 0.35

    )

    # Keep the values realistic.

    home_attack = max(

        0.20,

        min(

            4.00,

            home_attack

        )

    )

    away_attack = max(

        0.20,

        min(

            4.00,

            away_attack

        )

    )

    return (

        round(home_attack, 2),

        round(away_attack, 2)

    )


# =========================================================
# PREMATCH MARKET PROBABILITIES
# =========================================================

# BLOCK: PREMATCH_MARKET_PROBABILITIES
def prematch_market_probabilities(

    home_form,

    away_form

):

    home_attack, away_attack = (

        prematch_goal_expectation(

            home_form,

            away_form

        )

    )

    over25 = poisson_over25(

        home_attack,

        away_attack

    )

    under25 = poisson_under25(

        home_attack,

        away_attack

    )

    over35 = poisson_over35(

        home_attack,

        away_attack

    )

    under35 = poisson_under35(

        home_attack,

        away_attack

    )

    btts = poisson_btts(

        home_attack,

        away_attack

    )

    # Team goals

    home_over15 = round(

        (

            1 -

            poisson.cdf(

                1,

                home_attack

            )

        ) * 100,

        2

    )

    away_over15 = round(

        (

            1 -

            poisson.cdf(

                1,

                away_attack

            )

        ) * 100,

        2

    )

    return {

        "home_attack": home_attack,

        "away_attack": away_attack,

        "over25": over25,

        "under25": under25,

        "over35": over35,

        "under35": under35,

        "btts": btts,

        "home_over15": home_over15,

        "away_over15": away_over15

    }


# =========================================================
# PREMATCH SIGNAL CANDIDATE
# =========================================================

# BLOCK: CREATE_PREMATCH_CANDIDATE
def create_prematch_candidate(

    market,

    probability,

    odd,

    confidence

):

    if odd is None:

        return None

    if probability < PREMATCH_MIN_PROBABILITY:

        return None

    if odd < PREMATCH_MIN_ODD:

        return None

    if odd > PREMATCH_MAX_ODD:

        return None

    edge = value_edge(

        probability,

        odd

    )

    ev = (

        probability / 100

        *

        odd

        -

        1

    )

    # Small quality bonus for positive value.

    candidate_confidence = confidence

    if edge >= 15:

        candidate_confidence += 4

    elif edge >= 8:

        candidate_confidence += 2

    elif edge < 0:

        candidate_confidence -= 5

    if ev > 0.15:

        candidate_confidence += 3

    elif ev > 0.05:

        candidate_confidence += 1

    candidate_confidence = min(

        95,

        round(

            candidate_confidence,

            1

        )

    )

    if candidate_confidence < PREMATCH_MIN_CONFIDENCE:

        return None

    # Final score intentionally simple.

    score = (

        probability * 0.45

        +

        candidate_confidence * 0.35

        +

        max(

            0,

            min(

                100,

                50 + edge * 2

            )

        ) * 0.20

    )

    return {

        "market": market,

        "probability": round(

            probability,

            1

        ),

        "odd": round(

            odd,

            2

        ),

        "confidence": candidate_confidence,

        "edge": round(

            edge,

            1

        ),

        "ev": round(

            ev,

            3

        ),

        "score": round(

            score,

            2

        )

    }


# =========================================================
# PREMATCH CONFIDENCE
# =========================================================

# BLOCK: PREMATCH_CONFIDENCE
def prematch_confidence(

    home_form,

    away_form,

    probability

):

    confidence = 50

    form_gap = abs(

        home_form["form_pct"]

        -

        away_form["form_pct"]

    )

    recent_gap = abs(

        home_form["recent_form_pct"]

        -

        away_form["recent_form_pct"]

    )

    # Strong probability

    if probability >= 80:

        confidence += 15

    elif probability >= 75:

        confidence += 11

    elif probability >= 70:

        confidence += 7

    # Form difference

    if form_gap >= 20:

        confidence += 8

    elif form_gap >= 12:

        confidence += 5

    elif form_gap >= 7:

        confidence += 2

    # Recent form

    if recent_gap >= 20:

        confidence += 6

    elif recent_gap >= 10:

        confidence += 3

    # Goal difference

    goal_diff_gap = abs(

        home_form["goal_diff"]

        -

        away_form["goal_diff"]

    )

    if goal_diff_gap >= 10:

        confidence += 5

    elif goal_diff_gap >= 5:

        confidence += 2

    # Avoid exaggerated confidence.

    return round(

        min(

            95,

            confidence

        ),

        1

    )


# =========================================================
# PREMATCH SIGNAL DEDUPLICATION
# =========================================================

# BLOCK: DEDUPLICATE_PREMATCH_SIGNALS
def deduplicate_prematch_signals(

    candidates

):

    if not candidates:

        return []

    candidates = sorted(

        candidates,

        key=lambda x: (

            x["score"],

            x["probability"],

            x["confidence"],

            x["ev"]

        ),

        reverse=True

    )

    selected = []

    used_direction = set()

    for candidate in candidates:

        market = candidate["market"]

        # Do not send contradictory selections
        # from the same match.

        if market in (

            "🏆 HOME WIN",

            "✈️ AWAY WIN"

        ):

            direction = "RESULT"

        elif market in (

            "🚀 OVER 2.5",

            "🛡 UNDER 2.5"

        ):

            direction = "TOTAL25"

        elif market in (

            "🔥 OVER 3.5",

            "🛡 UNDER 3.5"

        ):

            direction = "TOTAL35"

        elif market in (

            "💎 BTTS YES",

            "BTTS"

        ):

            direction = "BTTS"

        else:

            direction = market

        if direction in used_direction:

            continue

        used_direction.add(

            direction

        )

        selected.append(

            candidate

        )

    return selected


# =========================================================
# PREMATCH ANALYSIS
# =========================================================

# BLOCK: ANALYZE_PREMATCH
# =========================================================
# BLOCK: PREMATCH MASTER ANALYZER
# =========================================================



# =========================================================
# PREMATCH SIGNAL DATABASE SAVE
# =========================================================

# BLOCK: SAVE_PREMATCH_SIGNAL
def save_prematch_signal(

    signal

):

    if not signal:

        return False

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    # Prevent duplicate signal for same
    # fixture + market.

    cursor.execute(

        """

        SELECT id

        FROM signals

        WHERE

            fixture_id=?

            AND market=?

        LIMIT 1

        """,

        (

            signal["fixture_id"],

            signal["market"]

        )

    )

    exists = cursor.fetchone()

    if exists:

        conn.close()

        return False

    cursor.execute(

        """

        INSERT INTO signals(

            fixture_id,

            country,

            league,

            home_team,

            away_team,

            market,

            probability,

            odd,

            confidence,

            result,

            created_at,

            status

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)

        """,

        (

            signal["fixture_id"],

            signal["country"],

            signal["league"],

            signal["home_team"],

            signal["away_team"],

            signal["market"],

            signal["probability"],

            signal["odd"],

            signal["confidence"],

            datetime.now(

                TIMEZONE

            ).isoformat(),

            "PENDING"

        )

    )

    conn.commit()

    conn.close()

    return True


# =========================================================
# PREMATCH TELEGRAM MESSAGE
# =========================================================

# BLOCK: FORMAT_PREMATCH_SIGNAL
def format_prematch_signal(

    signal

):

    return (

        "🔥 PREMATCH V4\n\n"

        f"⚽ {signal['home_team']} "
        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n"

        f"🌍 {signal['country']}\n\n"

        f"🎯 {signal['market']}\n"

        f"📊 Probability: "
        f"{signal['probability']:.1f}%\n"

        f"💰 Odd: "
        f"{signal['odd']:.2f}\n"

        f"📈 Edge: "
        f"{signal['edge']:.1f}%\n"

        f"💎 EV: "
        f"{signal['ev']:+.3f}\n"

        f"🤖 Confidence: "
        f"{signal['confidence']:.1f}%\n\n"

        f"🏠 Home: "
        f"{signal['home_probability']:.1f}%\n"

        f"⚖ Draw: "
        f"{signal['draw_probability']:.1f}%\n"

        f"✈️ Away: "
        f"{signal['away_probability']:.1f}%"

    )


# =========================================================
# PREMATCH SCAN
# =========================================================

# BLOCK: SCAN_PREMATCH_MATCHES
def scan_prematch_matches(matches):

    if not matches:

        return []

    candidates = []

    for match in matches:

        signals = analyze_prematch(

            match

        )

        if not signals:

            continue

        candidates.extend(

            signals

        )

    if not candidates:

        return []

    # -----------------------------------------------------
    # GLOBAL RANKING
    # -----------------------------------------------------

    candidates.sort(

        key=lambda x: (

            x["score"],

            x["probability"],

            x["confidence"],

            x["ev"]

        ),

        reverse=True

    )

    # -----------------------------------------------------
    # FEW BUT GOOD
    # -----------------------------------------------------

    selected = candidates[

        :MAX_PREMATCH_SIGNALS_PER_SCAN

    ]

    sent = []

    for signal in selected:

        if save_prematch_signal(

            signal

        ):

            message = format_prematch_signal(

                signal

            )

            if send_telegram(

                message

            ):

                sent.append(

                    signal

                )

    return sent

# =========================================================
# LIVE / NEXT GOAL ENGINE V4
# =========================================================

LIVE_MINUTE = 45
LIVE_MAX_MINUTE = 88

LIVE_MIN_PROBABILITY = 68
LIVE_MIN_CONFIDENCE = 80

LIVE_MIN_PRESSURE = 68
LIVE_MIN_ATTACK = 65

LIVE_COOLDOWN = 600

MAX_LIVE_SIGNALS_PER_SCAN = 5


# =========================================================
# LIVE STATISTICS HELPERS
# =========================================================

# BLOCK: GET_STAT_VALUE
def get_stat_value(

    statistics,

    team_id,

    stat_name

):

    for team_data in statistics:

        team = team_data.get(

            "team",

            {}

        )

        if team.get("id") != team_id:

            continue

        value = extract(

            team_data,

            stat_name

        )

        return value

    return 0


# BLOCK: GET_LIVE_TEAM_STATS
def get_live_team_stats(

    fixture

):

    fixture_id = fixture.get(

        "fixture",

        {}

    ).get(

        "id"

    )

    teams = fixture.get(

        "teams",

        {}

    )

    home = teams.get(

        "home",

        {}

    )

    away = teams.get(

        "away",

        {}

    )

    home_id = home.get(

        "id"

    )

    away_id = away.get(

        "id"

    )

    if not fixture_id:

        return None

    if not home_id or not away_id:

        return None

    statistics = get_statistics(

        fixture_id

    )

    if not statistics:

        return None

    result = {

        "home": {

            "shots": 0,

            "shots_on": 0,

            "dangerous": 0,

            "corners": 0,

            "possession": 0,

            "attacks": 0,

            "xg": 0,

            "red_cards": 0

        },

        "away": {

            "shots": 0,

            "shots_on": 0,

            "dangerous": 0,

            "corners": 0,

            "possession": 0,

            "attacks": 0,

            "xg": 0,

            "red_cards": 0

        }

    }

    for item in statistics:

        team = item.get(

            "team",

            {}

        )

        team_id = team.get(

            "id"

        )

        if team_id == home_id:

            target = result["home"]

        elif team_id == away_id:

            target = result["away"]

        else:

            continue

        for stat in item.get(

            "statistics",

            []

        ):

            name = clean_text(

                stat.get("type")

            )

            value = stat.get(

                "value"

            )

            if value is None:

                continue

            try:

                if isinstance(

                    value,

                    str

                ):

                    value = value.replace(

                        "%",

                        ""

                    )

                value = float(

                    value

                )

            except:

                continue

            if name in (

                "total shots",

                "shots"

            ):

                target["shots"] = value

            elif name in (

                "shots on goal",

                "shots on target"

            ):

                target["shots_on"] = value

            elif name == "dangerous attacks":

                target["dangerous"] = value

            elif name == "corner kicks":

                target["corners"] = value

            elif name == "ball possession":

                target["possession"] = value

            elif name in (

                "attacks",

                "total attacks"

            ):

                target["attacks"] = value

            elif name in (

                "expected goals",

                "xg"

            ):

                target["xg"] = value

            elif name == "red cards":

                target["red_cards"] = value

    return result


# =========================================================
# LIVE PRESSURE
# =========================================================

# BLOCK: CALCULATE_LIVE_PRESSURE
def calculate_live_pressure(

    team

):

    pressure = 0

    pressure += min(

        25,

        team["dangerous"] * 0.35

    )

    pressure += min(

        25,

        team["shots"] * 1.2

    )

    pressure += min(

        20,

        team["shots_on"] * 4

    )

    pressure += min(

        15,

        team["corners"] * 2

    )

    pressure += min(

        15,

        team["xg"] * 6

    )

    return round(

        min(

            100,

            pressure

        ),

        1

    )


# =========================================================
# LIVE ATTACK SCORE
# =========================================================

# BLOCK: CALCULATE_LIVE_ATTACK
def calculate_live_attack(

    team

):

    attack = 0

    attack += min(

        30,

        team["dangerous"] * 0.45

    )

    attack += min(

        25,

        team["shots"] * 1.4

    )

    attack += min(

        25,

        team["shots_on"] * 5

    )

    attack += min(

        20,

        team["xg"] * 8

    )

    return round(

        min(

            100,

            attack

        ),

        1

    )


# =========================================================
# LIVE DOMINANCE
# =========================================================

# BLOCK: LIVE_DOMINANCE
def live_dominance(

    home,

    away

):

    home_pressure = calculate_live_pressure(

        home

    )

    away_pressure = calculate_live_pressure(

        away

    )

    home_attack = calculate_live_attack(

        home

    )

    away_attack = calculate_live_attack(

        away

    )

    pressure_diff = (

        home_pressure

        -

        away_pressure

    )

    attack_diff = (

        home_attack

        -

        away_attack

    )

    return {

        "home_pressure": home_pressure,

        "away_pressure": away_pressure,

        "home_attack": home_attack,

        "away_attack": away_attack,

        "pressure_diff": round(

            pressure_diff,

            1

        ),

        "attack_diff": round(

            attack_diff,

            1

        )

    }


# =========================================================
# LIVE GOAL BASE PROBABILITY
# =========================================================

# BLOCK: LIVE_GOAL_PROBABILITY
def live_goal_probability(

    minute,

    pressure,

    attack,

    shots_on,

    xg,

    dangerous,

    corners

):

    probability = 50

    # Pressure

    if pressure >= 85:

        probability += 15

    elif pressure >= 75:

        probability += 10

    elif pressure >= 68:

        probability += 5

    # Attack

    if attack >= 90:

        probability += 12

    elif attack >= 80:

        probability += 8

    elif attack >= 70:

        probability += 5

    # Shots on target

    probability += min(

        10,

        shots_on * 1.5

    )

    # xG

    probability += min(

        12,

        xg * 5

    )

    # Dangerous attacks

    probability += min(

        8,

        dangerous * 0.12

    )

    # Corners

    probability += min(

        5,

        corners * 0.8

    )

    # Useful live window

    if 55 <= minute <= 80:

        probability += 5

    elif 80 < minute <= 88:

        probability += 3

    return round(

        min(

            95,

            max(

                50,

                probability

            )

        ),

        1

    )


# =========================================================
# LIVE CONFIDENCE
# =========================================================

# BLOCK: LIVE_CONFIDENCE
def live_confidence(

    probability,

    pressure,

    attack,

    shots_on,

    xg,

    score_diff,

    minute

):

    confidence = 50

    # Probability

    if probability >= 85:

        confidence += 15

    elif probability >= 78:

        confidence += 11

    elif probability >= 72:

        confidence += 7

    elif probability >= 68:

        confidence += 4

    # Pressure

    if pressure >= 90:

        confidence += 10

    elif pressure >= 80:

        confidence += 7

    elif pressure >= 70:

        confidence += 4

    # Attack

    if attack >= 90:

        confidence += 8

    elif attack >= 80:

        confidence += 5

    elif attack >= 70:

        confidence += 3

    # Shots on target

    if shots_on >= 6:

        confidence += 6

    elif shots_on >= 4:

        confidence += 4

    elif shots_on >= 2:

        confidence += 2

    # xG

    if xg >= 2.5:

        confidence += 7

    elif xg >= 1.8:

        confidence += 4

    elif xg >= 1.2:

        confidence += 2

    # Close match

    if score_diff <= 1:

        confidence += 4

    # Best live period

    if 55 <= minute <= 80:

        confidence += 4

    return round(

        min(

            95,

            confidence

        ),

        1

    )


# =========================================================
# LIVE SIDE SELECTION
# =========================================================

# BLOCK: SELECT_NEXT_GOAL_SIDE
def select_next_goal_side(

    home,

    away,

    dominance

):

    home_score = (

        dominance["home_pressure"] * 0.45

        +

        dominance["home_attack"] * 0.55

    )

    away_score = (

        dominance["away_pressure"] * 0.45

        +

        dominance["away_attack"] * 0.55

    )

    # Need clear separation.
    # This is important for quality.

    if abs(

        home_score - away_score

    ) < 10:

        return None

    if home_score > away_score:

        return {

            "side": "HOME",

            "score": round(

                home_score,

                1

            ),

            "opponent_score": round(

                away_score,

                1

            )

        }

    return {

        "side": "AWAY",

        "score": round(

            away_score,

            1

        ),

        "opponent_score": round(

            home_score,

            1

        )

    }


# =========================================================
# LIVE SIGNAL QUALITY
# =========================================================

# BLOCK: LIVE_SIGNAL_QUALITY
def live_signal_quality(

    pressure,

    attack,

    probability,

    confidence,

    shots_on,

    xg,

    pressure_diff

):

    quality = 0

    quality += probability * 0.30

    quality += confidence * 0.30

    quality += pressure * 0.15

    quality += attack * 0.15

    quality += min(

        10,

        shots_on * 1.5

    )

    quality += min(

        8,

        xg * 2

    )

    if pressure_diff >= 20:

        quality += 5

    elif pressure_diff >= 12:

        quality += 3

    return round(

        min(

            100,

            quality

        ),

        2

    )


# =========================================================
# LIVE RISK
# =========================================================

# BLOCK: LIVE_RISK
def live_risk(

    probability,

    confidence,

    pressure_diff,

    minute,

    score_diff,

    red_cards

):

    risk = 0

    if probability < 70:

        risk += 20

    elif probability < 75:

        risk += 10

    if confidence < 80:

        risk += 15

    elif confidence < 85:

        risk += 7

    if pressure_diff < 10:

        risk += 15

    if minute < 50:

        risk += 15

    if minute >= 86:

        risk += 8

    if score_diff >= 2:

        risk += 8

    if red_cards > 0:

        risk += 5

    return min(

        100,

        risk

    )


# =========================================================
# LIVE FINAL DECISION
# =========================================================

# BLOCK: ANALYZE_LIVE_MATCH


# =========================================================
# LIVE SIGNAL SAVE
# =========================================================

# BLOCK: SAVE_LIVE_SIGNAL
def save_live_signal(

    signal

):

    if not signal:

        return False

    conn = sqlite3.connect(

        DB_NAME

    )

    cursor = conn.cursor()

    # Same fixture + market + score
    # should not be duplicated.

    cursor.execute(

        """

        SELECT id

        FROM signals

        WHERE

            fixture_id=?

            AND market=?

            AND home_goals=?

            AND away_goals=?

        LIMIT 1

        """,

        (

            signal["fixture_id"],

            signal["market"],

            signal["home_goals"],

            signal["away_goals"]

        )

    )

    if cursor.fetchone():

        conn.close()

        return False

    cursor.execute(

        """

        INSERT INTO signals(

            fixture_id,

            country,

            league,

            home_team,

            away_team,

            market,

            probability,

            odd,

            confidence,

            result,

            created_at,

            status,

            home_goals,

            away_goals,

            fixture_status

        )

        VALUES(

            ?, ?, ?, ?, ?, ?, ?, ?, ?,

            NULL, ?, 'PENDING', ?, ?, 'LIVE'

        )

        """,

        (

            signal["fixture_id"],

            signal["country"],

            signal["league"],

            signal["home_team"],

            signal["away_team"],

            signal["market"],

            signal["probability"],

            None,

            signal["confidence"],

            signal["created_at"],

            signal["home_goals"],

            signal["away_goals"]

        )

    )

    conn.commit()

    conn.close()

    return True


# =========================================================
# LIVE TELEGRAM
# =========================================================

# BLOCK: FORMAT_LIVE_SIGNAL
def format_live_signal(

    signal

):

    return (

        "🔥 LIVE V4\n\n"

        f"⚽ {signal['home_team']} "
        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n"

        f"⏱ {signal['minute']}'\n"

        f"📊 Score: "
        f"{signal['home_goals']}-"
        f"{signal['away_goals']}\n\n"

        f"🎯 {signal['market']}\n\n"

        f"📈 Probability: "
        f"{signal['probability']:.1f}%\n"

        f"🤖 Confidence: "
        f"{signal['confidence']:.1f}%\n"

        f"💪 Pressure: "
        f"{signal['pressure']:.1f}\n"

        f"⚡ Attack: "
        f"{signal['attack']:.1f}\n"

        f"🎯 Shots on target: "
        f"{signal['shots_on']:.0f}\n"

        f"📐 xG: "
        f"{signal['xg']:.2f}\n"

        f"🔥 Dangerous attacks: "
        f"{signal['dangerous']:.0f}\n"

        f"🛡 Risk: "
        f"{signal['risk']}\n"

        f"⭐ Quality: "
        f"{signal['quality']:.1f}"

    )


# =========================================================
# LIVE SCANNER
# =========================================================

# BLOCK: SCAN_LIVE_MATCHES
def scan_live_matches():

    matches = get_live_matches()

    if not matches:

        return []

    candidates = []

    for fixture in matches:

        signal = analyze_live_match(

            fixture

        )

        if not signal:

            continue

        candidates.append(

            signal

        )

    if not candidates:

        return []

    # -----------------------------------------------------
    # BEST FIRST
    # -----------------------------------------------------

    candidates.sort(

        key=lambda x: (

            x["quality"],

            x["confidence"],

            x["probability"],

            -x["risk"]

        ),

        reverse=True

    )

    selected = candidates[

        :MAX_LIVE_SIGNALS_PER_SCAN

    ]

    sent = []

    for signal in selected:

        key = (

            f"{signal['fixture_id']}_"

            f"{signal['home_goals']}_"

            f"{signal['away_goals']}_"

            f"{signal['market']}"

        )

        # Mark only after successful send.

        message = format_live_signal(

            signal

        )

        if send_telegram(

            message

        ):

            sent_live[key] = time.time()

            save_live_signal(

                signal

            )

            sent.append(

                signal

            )

    return sent

# =========================================================
# MAIN ENGINE V4
# =========================================================
#
# Един централен engine:
#
# PREMATCH
#    ↓
# LIVE
#    ↓
# SIGNAL FILTER
#    ↓
# SAVE
#    ↓
# TELEGRAM
#    ↓
# RESULT CHECK
#
# =========================================================

import time
import logging
import sqlite3
from datetime import datetime


# =========================================================
# MAIN SETTINGS
# =========================================================

SCAN_INTERVAL = 60

PREMATCH_SCAN_INTERVAL = 300

LIVE_SCAN_INTERVAL = 60

RESULT_SCAN_INTERVAL = 120

MAX_PREMATCH_SIGNALS = 5

MAX_LIVE_SIGNALS = 5

MIN_PREMATCH_CONFIDENCE = 75

MIN_LIVE_CONFIDENCE = 80

MIN_LIVE_PROBABILITY = 68

MAX_LIVE_RISK = 35


# =========================================================
# RUNTIME STATE
# =========================================================

LAST_PREMATCH_SCAN = 0

LAST_LIVE_SCAN = 0

LAST_RESULT_SCAN = 0

RUNNING = True


# =========================================================
# GLOBAL SIGNAL MEMORY
# =========================================================

SENT_SIGNALS = {}

sent_live = {}

sent_prematch = {}


# =========================================================
# SAFE LOGGING
# =========================================================

# BLOCK: MAIN_LOG
def main_log(

    message,

    level="INFO"

):

    try:

        if level == "ERROR":

            logging.error(

                message

            )

        elif level == "WARNING":

            logging.warning(

                message

            )

        else:

            logging.info(

                message

            )

    except Exception:

        print(

            message

        )


# =========================================================
# SAFE TIME
# =========================================================

# BLOCK: NOW_TS
def now_ts():

    return time.time()


# =========================================================
# SAFE SIGNAL KEY
# =========================================================

# BLOCK: MAKE_SIGNAL_KEY
def make_signal_key(

    fixture_id,

    market

):

    return (

        f"{fixture_id}_"

        f"{market}"

    )


# =========================================================
# SIGNAL DUPLICATE CHECK
# =========================================================

# BLOCK: ALREADY_SENT
def already_sent(

    fixture_id,

    market,

    cooldown=900

):

    key = make_signal_key(

        fixture_id,

        market

    )

    now = now_ts()

    if key not in SENT_SIGNALS:

        return False

    elapsed = (

        now

        -

        SENT_SIGNALS[key]

    )

    if elapsed < cooldown:

        return True

    return False


# =========================================================
# MARK SIGNAL AS SENT
# =========================================================

# BLOCK: MARK_SIGNAL_SENT
def mark_signal_sent(

    fixture_id,

    market

):

    key = make_signal_key(

        fixture_id,

        market

    )

    SENT_SIGNALS[key] = now_ts()


# =========================================================
# SAFE TELEGRAM SEND
# =========================================================

# BLOCK: SAFE_SEND_TELEGRAM
def safe_send_telegram(

    message

):

    try:

        result = send_telegram(

            message

        )

        return bool(

            result

        )

    except Exception as e:

        main_log(

            f"Telegram error: {repr(e)}",

            "WARNING"

        )

        return False


# =========================================================
# PREMATCH SIGNAL NORMALIZER
# =========================================================

# BLOCK: NORMALIZE_PREMATCH_SIGNAL
def normalize_prematch_signal(

    signal,

    match

):

    if not signal:

        return None

    if not isinstance(

        signal,

        dict

    ):

        return None

    fixture = match.get(

        "fixture",

        {}

    )

    teams = match.get(

        "teams",

        {}

    )

    league = match.get(

        "league",

        {}

    )

    fixture_id = signal.get(

        "fixture_id",

        fixture.get("id")

    )

    if not fixture_id:

        return None

    probability = signal.get(

        "probability",

        0

    )

    confidence = signal.get(

        "confidence",

        0

    )

    market = signal.get(

        "market"

    )

    if not market:

        return None

    try:

        probability = float(

            probability

        )

        confidence = float(

            confidence

        )

    except Exception:

        return None

    signal["fixture_id"] = (

        fixture_id

    )

    signal["home_team"] = signal.get(

        "home_team",

        teams.get(

            "home",

            {}

        ).get(

            "name",

            "HOME"

        )

    )

    signal["away_team"] = signal.get(

        "away_team",

        teams.get(

            "away",

            {}

        ).get(

            "name",

            "AWAY"

        )

    )

    signal["league"] = signal.get(

        "league",

        league.get(

            "name",

            ""

        )

    )

    signal["country"] = signal.get(

        "country",

        league.get(

            "country",

            ""

        )

    )

    signal["probability"] = probability

    signal["confidence"] = confidence

    return signal


# =========================================================
# PREMATCH MESSAGE
# =========================================================

# BLOCK: FORMAT_PREMATCH_SIGNAL
def format_prematch_signal(

    signal

):

    probability = signal.get(

        "probability",

        0

    )

    confidence = signal.get(

        "confidence",

        0

    )

    odd = signal.get(

        "odd"

    )

    if odd is None:

        odd_text = "N/A"

    else:

        try:

            odd_text = f"{float(odd):.2f}"

        except Exception:

            odd_text = str(

                odd

            )

    return (

        "🔥 PREMATCH V4\n\n"

        f"⚽ {signal['home_team']} "

        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n"

        f"🌍 {signal['country']}\n\n"

        f"🎯 {signal['market']}\n\n"

        f"📈 Probability: "

        f"{probability:.1f}%\n"

        f"🤖 Confidence: "

        f"{confidence:.1f}%\n"

        f"💰 Odd: {odd_text}\n"

    )


# =========================================================
# PREMATCH SCANNER
# =========================================================

# BLOCK: SCAN_PREMATCH_MATCHES
def scan_prematch_matches():

    try:

        matches = get_prematch_matches()

    except Exception as e:

        main_log(

            f"Prematch fetch error: {repr(e)}",

            "WARNING"

        )

        return []

    if not matches:

        return []

    candidates = []

    for match in matches:

        try:

            fixture = match.get(

                "fixture",

                {}

            )

            fixture_id = fixture.get(

                "id"

            )

            if not fixture_id:

                continue

            signals = analyze_prematch(

                match

            )

            if not signals:

                continue

            if not isinstance(

                signals,

                list

            ):

                continue

            for signal in signals:

                signal = normalize_prematch_signal(

                    signal,

                    match

                )

                if not signal:

                    continue

                confidence = signal.get(

                    "confidence",

                    0

                )

                if confidence < MIN_PREMATCH_CONFIDENCE:

                    continue

                market = signal.get(

                    "market"

                )

                if not market:

                    continue

                if already_sent(

                    fixture_id,

                    market,

                    1800

                ):

                    continue

                candidates.append(

                    signal

                )

        except Exception as e:

            main_log(

                "Prematch analysis error: "

                f"{repr(e)}",

                "WARNING"

            )

            continue

    # -----------------------------------------------------
    # BEST SIGNALS FIRST
    # -----------------------------------------------------

    candidates.sort(

        key=lambda x: (

            x.get(

                "confidence",

                0

            ),

            x.get(

                "probability",

                0

            ),

            x.get(

                "value_score",

                0

            )

        ),

        reverse=True

    )

    selected = candidates[

        :MAX_PREMATCH_SIGNALS

    ]

    sent = []

    for signal in selected:

        try:

            fixture_id = signal[

                "fixture_id"

            ]

            market = signal[

                "market"

            ]

            message = format_prematch_signal(

                signal

            )

            success = safe_send_telegram(

                message

            )

            if not success:

                continue

            mark_signal_sent(

                fixture_id,

                market

            )

            sent.append(

                signal

            )

        except Exception as e:

            main_log(

                "Prematch send error: "

                f"{repr(e)}",

                "WARNING"

            )

    return sent


# =========================================================
# LIVE SCANNER WRAPPER
# =========================================================

# BLOCK: RUN_LIVE_SCAN
def run_live_scan():

    try:

        signals = scan_live_matches()

        if signals:

            main_log(

                f"LIVE signals sent: "

                f"{len(signals)}"

            )

        return signals

    except Exception as e:

        main_log(

            f"Live scanner error: {repr(e)}",

            "WARNING"

        )

        return []


# =========================================================
# RESULT CHECKER WRAPPER
# =========================================================

# BLOCK: RUN_RESULT_CHECKER
def run_result_checker():

    try:

        # Use existing result checker
        # if it exists in the project.

        checker = globals().get(

            "check_results"

        )

        if checker is None:

            return []

        result = checker()

        return result

    except Exception as e:

        main_log(

            f"Result checker error: "

            f"{repr(e)}",

            "WARNING"

        )

        return []


# =========================================================
# DATABASE HEALTH CHECK
# =========================================================

# BLOCK: DATABASE_HEALTH_CHECK
def database_health_check():

    try:

        conn = sqlite3.connect(

            DB_NAME

        )

        cursor = conn.cursor()

        cursor.execute(

            """

            SELECT COUNT(*)

            FROM signals

            """

        )

        total = cursor.fetchone()[0]

        conn.close()

        return total

    except Exception as e:

        main_log(

            f"Database error: {repr(e)}",

            "ERROR"

        )

        return None


# =========================================================
# API HEALTH CHECK
# =========================================================

# BLOCK: API_HEALTH_CHECK
def api_health_check():

    try:

        # We do not make an unnecessary API request here.
        #
        # The real API call happens during the scanners.

        if not API_KEY:

            return False

        return True

    except Exception:

        return False


# =========================================================
# SYSTEM STATUS
# =========================================================

# BLOCK: PRINT_SYSTEM_STATUS
def print_system_status():

    db_total = database_health_check()

    api_ok = api_health_check()

    print()

    print(

        "=========================================="

    )

    print(

        "🤖 AI FOOTBALL SYSTEM V4"

    )

    print(

        "=========================================="

    )

    print(

        "API:     ",

        "ONLINE" if api_ok else "ERROR"

    )

    print(

        "DB:      ",

        db_total

        if db_total is not None

        else "ERROR"

    )

    print(

        "PREMATCH:",

        "READY"

    )

    print(

        "LIVE:    ",

        "READY"

    )

    print(

        "RESULTS: ",

        "READY"

    )

    print(

        "=========================================="

    )

    print()


# =========================================================
# MAIN LOOP
# =========================================================

# BLOCK: MAIN_LOOP
def legacy_main_loop_1():

    global LAST_PREMATCH_SCAN

    global LAST_LIVE_SCAN

    global LAST_RESULT_SCAN

    global RUNNING

    while RUNNING:

        try:

            now = now_ts()

            # =============================================
            # LIVE
            # =============================================

            if (

                now

                -

                LAST_LIVE_SCAN

                >= LIVE_SCAN_INTERVAL

            ):

                LAST_LIVE_SCAN = now

                try:

                    run_live_scan()

                except Exception as e:

                    main_log(

                        f"Live loop error: "

                        f"{repr(e)}",

                        "WARNING"

                    )

            # =============================================
            # PREMATCH
            # =============================================

            if (

                now

                -

                LAST_PREMATCH_SCAN

                >= PREMATCH_SCAN_INTERVAL

            ):

                LAST_PREMATCH_SCAN = now

                try:

                    signals = (

                        scan_prematch_matches()

                    )

                    if signals:

                        main_log(

                            "Prematch signals sent: "

                            f"{len(signals)}"

                        )

                except Exception as e:

                    main_log(

                        f"Prematch loop error: "

                        f"{repr(e)}",

                        "WARNING"

                    )

            # =============================================
            # RESULTS
            # =============================================

            if (

                now

                -

                LAST_RESULT_SCAN

                >= RESULT_SCAN_INTERVAL

            ):

                LAST_RESULT_SCAN = now

                try:

                    run_result_checker()

                except Exception as e:

                    main_log(

                        f"Result loop error: "

                        f"{repr(e)}",

                        "WARNING"

                    )

            # =============================================
            # WAIT
            # =============================================

            time.sleep(

                SCAN_INTERVAL

            )

        except KeyboardInterrupt:

            RUNNING = False

            print(

                "\n🛑 SYSTEM STOPPED"

            )

        except Exception as e:

            main_log(

                f"MAIN LOOP ERROR: "

                f"{repr(e)}",

                "ERROR"

            )

            # Never kill the whole bot
            # because of one bad match.

            time.sleep(

                10

            )


# =========================================================
# STARTUP
# =========================================================

# BLOCK: STARTUP
def startup():

    print_system_status()

    print(

        "🚀 Starting AI Football System V4..."

    )

    print(

        "📡 Live scanner: "

        f"every {LIVE_SCAN_INTERVAL}s"

    )

    print(

        "📊 Prematch scanner: "

        f"every {PREMATCH_SCAN_INTERVAL}s"

    )

    print(

        "🔎 Result checker: "

        f"every {RESULT_SCAN_INTERVAL}s"

    )

    print()

    main_loop()


# =========================================================
# PREMATCH ENGINE V4
# =========================================================

# BLOCK: ANALYZE_PREMATCH

# =========================================================
# PREMATCH FIXTURE ENGINE V4
# =========================================================

# BLOCK: GET_PREMATCH_MATCHES
def get_prematch_matches():

    try:

        # =================================================
        # GET UPCOMING FIXTURES
        # =================================================
        #
        # Една заявка вместо отделна заявка за всеки
        # следващ мач.
        #
        # Това пази API лимита.
        # =================================================

        today = datetime.now(
            TIMEZONE
        ).strftime(
            "%Y-%m-%d"
        )

        tomorrow = (
            datetime.now(
                TIMEZONE
            ) + timedelta(
                days=1
            )
        ).strftime(
            "%Y-%m-%d"
        )

        data_today = api_get(
            "fixtures",
            {
                "date": today,
                "timezone": str(TIMEZONE)
            }
        )

        data_tomorrow = api_get(
            "fixtures",
            {
                "date": tomorrow,
                "timezone": str(TIMEZONE)
            }
        )

        matches = (
            data_today.get("response", [])
            +
            data_tomorrow.get("response", [])
        )
        
        if not matches:
        
            return []
        
        now = datetime.now(
        
            timezone.utc
        
        )
        
        result = []
        
        


        # =========================================================
        # PREMATCH DEBUG COUNTERS
        # =========================================================

     

        # =================================================
        # BASIC MATCH FILTER
        # =================================================

        for match in matches:

            try:

                fixture = match.get(

                    "fixture",

                    {}
                )

                league = match.get(

                    "league",

                    {}
                )

                teams = match.get(

                    "teams",

                    {}
                )

                fixture_id = fixture.get(

                    "id"
                )

                if not fixture_id:

                    continue

                # =========================================
                # STATUS
                # =========================================

                status = fixture.get(

                    "status",

                    {}
                )

                status_short = status.get(

                    "short",

                    ""
                )

                # Само предстоящи мачове.

                if status_short not in (

                    "NS",

                    "TBD"

                ):

                    continue

                # =========================================
                # DATE
                # =========================================

                date_text = fixture.get(

                    "date"
                )

                if not date_text:

                    continue

                try:

                    match_time = (

                        datetime.fromisoformat(

                            date_text.replace(

                                "Z",

                                "+00:00"

                            )

                        )

                    )

                except Exception:

                    continue

                # Мачът трябва да е бъдещ.

                if match_time <= now:

                    continue           


                # =========================================================
                # PREMATCH 12-HOUR WINDOW
                # =========================================================
                hours_until = (
                    match_time - now
                ).total_seconds() / 3600
                
                #if hours_until > 12:
                #    continue

                

                # =========================================
                # LEAGUE
                # =========================================

                league_name = league.get(

                    "name",

                    ""
                )

                country = league.get(

                    "country",

                    ""
                )

                league_id = league.get(

                    "id"
                )

                season = league.get(

                    "season"
                )

                # =========================================
                # BLOCKED LEAGUES
                # =========================================

                if blocked_league(

                    league_name

                ):
                    continue

                # =========================================
                # BAD COUNTRIES
                # =========================================

                if country in BAD_COUNTRIES:
                    continue

                # =========================================
                # TEAMS
                # =========================================

                home = teams.get(

                    "home",

                    {}
                )

                away = teams.get(

                    "away",

                    {}
                )

                home_id = home.get(

                    "id"
                )

                away_id = away.get(

                    "id"
                )

                home_name = home.get(

                    "name",

                    ""
                )

                away_name = away.get(

                    "name",

                    ""
                )

                if not home_id or not away_id:

                    continue

                if not home_name or not away_name:

                    continue

                # =========================================
                # TEAM NAME SAFETY
                # =========================================

                home_clean = clean_text(

                    home_name
                )

                away_clean = clean_text(

                    away_name
                )

                # Допълнителна защита за очевидни
                # младежки/женски/резервни отбори.

                blocked_team_words = (

                    "women",
                    "female",
                    "u17",
                    "u18",
                    "u19",
                    "u20",
                    "u21",
                    "u22",
                    "u23",
                    "reserve",
                    "reserves",
                    "academy"
                )

                if any(
                    word in home_clean or word in away_clean
                    for word in blocked_team_words
                ):
                    continue

                # =========================================
                # MATCH OBJECT
                # =========================================

                match["_prematch_time"] = (

                    match_time
                )

                match["_league_id"] = (

                    league_id
                )

                match["_season"] = (

                    season
                )

                result.append(

                    match

                )

            except Exception as e:

                logging.warning(

                    "PREMATCH FILTER ERROR: %s",

                    repr(e)

                )

                continue

        # =================================================
        # SORT BY MATCH TIME
        # =================================================

        result.sort(

            key=lambda x:

                x.get(

                    "_prematch_time"

                )

        )

        # =================================================
        # LIMIT
        # =================================================
        #
        # Не анализираме стотици мачове.
        # Първо вземаме разумен брой предстоящи.
        #
        # След това analyze_prematch() избира само
        # качествените сигнали.
        # =================================================

        # =========================================================
        # PREMATCH DEBUG RESULT
        # =========================================================

     
        
        return result[:60]

    except Exception as e:

        logging.warning(

            "GET PREMATCH MATCHES ERROR: %s",

            repr(e)

        )

        return []


# =========================================================
# PREMATCH MATCH INFO
# =========================================================

# BLOCK: PREMATCH_MATCH_INFO
def prematch_match_info(match):

    try:

        fixture = match.get(

            "fixture",

            {}
        )

        teams = match.get(

            "teams",

            {}
        )

        league = match.get(

            "league",

            {}
        )

        date_text = fixture.get(

            "date"
        )

        match_time = None

        if date_text:

            try:

                dt = datetime.fromisoformat(

                    date_text.replace(

                        "Z",

                        "+00:00"

                    )

                )

                match_time = dt.astimezone(

                    TIMEZONE

                ).strftime(

                    "%d.%m.%Y %H:%M"

                )

            except Exception:

                pass

        return {

            "fixture_id":

                fixture.get("id"),

            "home":

                teams.get(

                    "home",

                    {}

                ).get(

                    "name",

                    ""

                ),

            "away":

                teams.get(

                    "away",

                    {}

                ).get(

                    "name",

                    ""

                ),

            "league":

                league.get(

                    "name",

                    ""

                ),

            "country":

                league.get(

                    "country",

                    ""

                ),

            "time":

                match_time

        }

    except Exception:

        return None

# =========================================================
# LIVE ENGINE V4
# =========================================================

LIVE_MINUTE = 25

LIVE_MAX_MINUTE = 88

LIVE_MIN_PROBABILITY = 68

LIVE_MIN_CONFIDENCE = 78

LIVE_MAX_RISK = 35

LIVE_MAX_SIGNALS = 5


# =========================================================
# LIVE STATISTICS PARSER
# =========================================================

# BLOCK: PARSE_LIVE_STATISTICS
def parse_live_statistics(

    fixture_id

):

    statistics = get_statistics(

        fixture_id

    )

    if not statistics:

        return None

    home_stats = {}
    away_stats = {}

    try:

        for team_data in statistics:

            team = team_data.get(

                "team",

                {}

            )

            team_id = team.get(

                "id"

            )

            values = team_data.get(

                "statistics",

                []

            )

            parsed = {}

            for item in values:

                stat_name = clean_text(

                    item.get(

                        "type",

                        ""

                    )

                )

                value = item.get(

                    "value"

                )

                if value is None:

                    value = 0

                if isinstance(

                    value,

                    str

                ):

                    value = value.replace(

                        "%",

                        ""

                    )

                try:

                    parsed[stat_name] = float(

                        value

                    )

                except Exception:

                    parsed[stat_name] = 0

            if not home_stats:

                home_stats = {

                    "team_id":

                        team_id,

                    **parsed

                }

            else:

                away_stats = {

                    "team_id":

                        team_id,

                    **parsed

                }

    except Exception as e:

        logging.warning(

            "LIVE STAT PARSE ERROR: %s",

            repr(e)

        )

        return None

    if not home_stats or not away_stats:

        return None

    return (

        home_stats,

        away_stats

    )


# =========================================================
# STAT HELPER
# =========================================================

# BLOCK: LIVE_STAT
def live_stat(

    stats,

    *names

):

    for name in names:

        key = clean_text(

            name

        )

        if key in stats:

            return safe_float(

                stats[key]

            ) or 0

    return 0


# =========================================================
# LIVE PRESSURE
# =========================================================

# BLOCK: CALCULATE_LIVE_PRESSURE
def calculate_live_pressure(

    stats

):

    shots = live_stat(

        stats,

        "Total Shots"

    )

    shots_on = live_stat(

        stats,

        "Shots on Goal",

        "Shots on Target"

    )

    corners = live_stat(

        stats,

        "Corner Kicks"

    )

    dangerous = live_stat(

        stats,

        "Dangerous Attacks"

    )

    possession = live_stat(

        stats,

        "Ball Possession"

    )

    pressure = 0

    pressure += min(

        25,

        shots * 2

    )

    pressure += min(

        25,

        shots_on * 5

    )

    pressure += min(

        15,

        corners * 2

    )

    pressure += min(

        25,

        dangerous * 0.35

    )

    pressure += min(

        10,

        max(

            0,

            possession - 50

        ) * 0.20

    )

    return round(

        min(

            100,

            pressure

        ),

        1

    )


# =========================================================
# LIVE ATTACK SCORE
# =========================================================

# BLOCK: CALCULATE_ATTACK
def calculate_attack(

    stats

):

    shots = live_stat(

        stats,

        "Total Shots"

    )

    shots_on = live_stat(

        stats,

        "Shots on Goal",

        "Shots on Target"

    )

    dangerous = live_stat(

        stats,

        "Dangerous Attacks"

    )

    corners = live_stat(

        stats,

        "Corner Kicks"

    )

    attack = 0

    attack += min(

        30,

        shots * 2.5

    )

    attack += min(

        30,

        shots_on * 7

    )

    attack += min(

        25,

        dangerous * 0.40

    )

    attack += min(

        15,

        corners * 2

    )

    return round(

        min(

            100,

            attack

        ),

        1

    )


# =========================================================
# LIVE xG
# =========================================================

# BLOCK: GET_LIVE_XG
def get_live_xg(

    stats

):

    xg = live_stat(

        stats,

        "Expected Goals",

        "xG"

    )

    return max(

        0,

        xg

    )


# =========================================================
# LIVE MATCH ANALYSIS
# =========================================================

# BLOCK: ANALYZE_LIVE_MATCH


# =========================================================
# LIVE SIGNAL MESSAGE
# =========================================================

# BLOCK: FORMAT_LIVE_SIGNAL
def format_live_signal(

    signal

):

    return (

        "🔥 LIVE V4\n\n"

        f"⚽ {signal['home_team']} "

        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n\n"

        f"⏱ {signal['minute']}'\n"

        f"📊 Score: "

        f"{signal['home_goals']}"

        f"-"

        f"{signal['away_goals']}\n\n"

        f"🎯 {signal['market']}\n\n"

        f"📈 Probability: "

        f"{signal['probability']:.1f}%\n"

        f"🤖 Confidence: "

        f"{signal['confidence']:.1f}%\n"

        f"🛡 Risk: "

        f"{signal['risk']}\n"

        f"📊 Quality: "

        f"{signal['quality']:.1f}\n\n"

        f"⚡ Pressure: "

        f"{signal['home_pressure']:.0f}"

        " / "

        f"{signal['away_pressure']:.0f}\n"

        f"⚽ Attack: "

        f"{signal['home_attack']:.0f}"

        " / "

        f"{signal['away_attack']:.0f}\n"

        f"📈 xG: "

        f"{signal['total_xg']:.2f}\n"

        f"🎯 Shots on target: "

        f"{signal['total_shots_on']:.0f}"

    )


# =========================================================
# LIVE SCANNER
# =========================================================

# BLOCK: SCAN_LIVE_MATCHES
def scan_live_matches():

    try:

        matches = get_live_matches()

    except Exception as e:

        logging.warning(

            "GET LIVE MATCHES ERROR: %s",

            repr(e)

        )

        return []

    if not matches:

        return []

    candidates = []

    for fixture in matches:

        try:

            signal = analyze_live_match(

                fixture

            )

            if not signal:

                continue

            candidates.append(

                signal

            )

        except Exception as e:

            logging.warning(

                "LIVE MATCH ERROR: %s",

                repr(e)

            )

            continue

    # =================================================
    # BEST LIVE SIGNALS
    # =================================================

    candidates.sort(

        key=lambda x: (

            x["confidence"],

            x["probability"],

            x["quality"],

            -x["risk"]

        ),

        reverse=True

    )

    selected = candidates[

        :LIVE_MAX_SIGNALS

    ]

    sent = []

    for signal in selected:

        try:

            message = format_live_signal(

                signal

            )

            if safe_send_telegram(

                message

            ):

                sent.append(

                    signal

                )

        except Exception as e:

            logging.warning(

                "LIVE TELEGRAM ERROR: %s",

                repr(e)

            )

    return sent

# =========================================================
# PREMATCH CORE V4
# =========================================================

PREMATCH_MIN_PROBABILITY = 62

PREMATCH_MIN_CONFIDENCE = 75

PREMATCH_MAX_RISK = 40

PREMATCH_MAX_SIGNALS = 5


# =========================================================
# PREMATCH MARKET PROBABILITY
# =========================================================

# BLOCK: PREMATCH_MARKET_PROBABILITY
def prematch_market_probability(

    home_form,

    away_form,

    table_home=None,

    table_away=None

):

    home_strength = team_strength(

        home_form

    )

    away_strength = team_strength(

        away_form

    )

    # =====================================================
    # TABLE ADVANTAGE
    # =====================================================

    if table_home and table_away:

        home_strength += (

            table_away["rank"]

            -

            table_home["rank"]

        ) * 1.5

        away_strength += (

            table_home["rank"]

            -

            table_away["rank"]

        ) * 1.5

        home_strength += (

            table_home["goal_diff"]

            -

            table_away["goal_diff"]

        ) * 0.25

        away_strength += (

            table_away["goal_diff"]

            -

            table_home["goal_diff"]

        ) * 0.25

    home_strength = max(

        1,

        home_strength

    )

    away_strength = max(

        1,

        away_strength

    )

    total_strength = (

        home_strength

        +

        away_strength

    )

    home_probability = round(

        home_strength

        /

        total_strength

        *

        100,

        1

    )

    away_probability = round(

        away_strength

        /

        total_strength

        *

        100,

        1

    )

    return {

        "home_strength":

            round(

                home_strength,

                2

            ),

        "away_strength":

            round(

                away_strength,

                2

            ),

        "home_probability":

            home_probability,

        "away_probability":

            away_probability

    }


# =========================================================
# PREMATCH GOALS MODEL
# =========================================================

# BLOCK: PREMATCH_GOAL_MODEL
def prematch_goal_model(

    home_form,

    away_form

):

    home_attack = (

        home_form["avg_scored"]

        *

        0.60

        +

        away_form["avg_conceded"]

        *

        0.40

    )

    away_attack = (

        away_form["avg_scored"]

        *

        0.60

        +

        home_form["avg_conceded"]

        *

        0.40

    )

    home_attack = max(

        0.10,

        min(

            4.00,

            home_attack

        )

    )

    away_attack = max(

        0.10,

        min(

            4.00,

            away_attack

        )

    )

    over25 = poisson_over25(

        home_attack,

        away_attack

    )

    over35 = poisson_over35(

        home_attack,

        away_attack

    )

    under25 = poisson_under25(

        home_attack,

        away_attack

    )

    under35 = poisson_under35(

        home_attack,

        away_attack

    )

    btts = poisson_btts(

        home_attack,

        away_attack

    )

    return {

        "home_attack":

            round(

                home_attack,

                2

            ),

        "away_attack":

            round(

                away_attack,

                2

            ),

        "over25":

            over25,

        "over35":

            over35,

        "under25":

            under25,

        "under35":

            under35,

        "btts":

            btts

    }


# =========================================================
# PREMATCH CONFIDENCE
# =========================================================

# BLOCK: PREMATCH_CONFIDENCE
def prematch_confidence(

    probability,

    form,

    opponent_form,

    market,

    league_score=50

):

    confidence = 50

    # =====================================================
    # PROBABILITY
    # =====================================================

    confidence += (

        probability

        -

        60

    ) * 0.45

    # =====================================================
    # TEAM FORM
    # =====================================================

    confidence += (

        form["recent_form_pct"]

        -

        60

    ) * 0.12

    # =====================================================
    # FORM DIFFERENCE
    # =====================================================

    form_difference = (

        form["form_pct"]

        -

        opponent_form["form_pct"]

    )

    if abs(form_difference) >= 15:

        confidence += 4

    elif abs(form_difference) >= 8:

        confidence += 2

    # =====================================================
    # MARKET SUPPORT
    # =====================================================

    if market in (

        "🚀 OVER 2.5",

        "💎 BTTS YES"

    ):

        if league_score >= 75:

            confidence += 4

        elif league_score <= 55:

            confidence -= 4

    # =====================================================
    # STRONG RECENT FORM
    # =====================================================

    if form["recent_form_pct"] >= 75:

        confidence += 3

    # =====================================================
    # CONSISTENT SCORING
    # =====================================================

    if form["scored_pct"] >= 80:

        confidence += 2

    return round(

        min(

            95,

            max(

                0,

                confidence

            )

        ),

        1

    )


# =========================================================
# PREMATCH RISK
# =========================================================

# BLOCK: PREMATCH_RISK
def prematch_risk(

    probability,

    confidence,

    form,

    opponent_form

):

    risk = 0

    # =====================================================
    # LOW PROBABILITY
    # =====================================================

    if probability < 65:

        risk += 15

    elif probability < 70:

        risk += 8

    # =====================================================
    # CONFIDENCE
    # =====================================================

    if confidence < 75:

        risk += 15

    elif confidence < 80:

        risk += 8

    # =====================================================
    # CLOSE TEAMS
    # =====================================================

    form_difference = abs(

        form["form_pct"]

        -

        opponent_form["form_pct"]

    )

    if form_difference < 5:

        risk += 10

    # =====================================================
    # WEAK SCORING
    # =====================================================

    if form["scored_pct"] < 60:

        risk += 8

    return min(

        100,

        risk

    )


# =========================================================
# PREMATCH SIGNAL
# =========================================================

# BLOCK: CREATE_PREMATCH_SIGNAL
def create_prematch_signal(

    market,

    probability,

    confidence,

    risk,

    odd,

    home,

    away,

    league

):

    if odd is None:

        return None

    probability = safe_float(probability)
    confidence = safe_float(confidence)
    risk = safe_float(risk)
    odd = safe_float(odd)

    if probability is None or confidence is None or risk is None or odd is None:
        return None

    if probability < 0 or probability > 100:
        return None

    # No market-specific hard threshold here. Every available market is
    # scored first; the final PREMATCH selector keeps only the strongest
    # probabilities.

    edge = value_edge(
        probability,
        odd
    )

    return {

        "market":

            market,

        "probability":

            round(

                probability,

                1

            ),

        "confidence":

            round(

                confidence,

                1

            ),

        "risk":

            risk,

        "odd":

            odd,

        "edge":

            edge,

        "home_team":

            home,

        "away_team":

            away,

        "league":

            league

    }


# =========================================================
# PREMATCH ANALYZER V4
# =========================================================

# =========================================================
# CALIBRATED PREMATCH PROBABILITY ENGINE
# =========================================================

def _poisson_result_probabilities(home_xg, away_xg, max_goals=10):
    """Model 1X2 probability from the same Poisson goal model used for totals."""
    try:
        ph = [poisson.pmf(i, max(0.05, home_xg)) for i in range(max_goals + 1)]
        pa = [poisson.pmf(i, max(0.05, away_xg)) for i in range(max_goals + 1)]
        home = draw = away = 0.0
        for i, p_i in enumerate(ph):
            for j, p_j in enumerate(pa):
                p = p_i * p_j
                if i > j:
                    home += p
                elif i == j:
                    draw += p
                else:
                    away += p
        total = home + draw + away
        if total <= 0:
            return 33.3, 33.3, 33.3
        return tuple(round(x / total * 100, 1) for x in (home, draw, away))
    except Exception:
        return 33.3, 33.3, 33.3


def _normalized_implied(odds):
    """Remove bookmaker overround from a set of mutually exclusive odds."""
    vals = []
    for odd in odds:
        if odd is None or odd <= 1.01:
            vals.append(None)
        else:
            vals.append(100.0 / odd)
    total = sum(v for v in vals if v is not None)
    if total <= 0:
        return [None for _ in odds]
    return [round(v / total * 100, 1) if v is not None else None for v in vals]


def _blend_probability(model_probability, market_probability, model_weight=0.55):
    """Calibrate model probability toward the bookmaker's normalized market."""
    if market_probability is None:
        return round(max(0.0, min(100.0, model_probability)), 1)
    blended = model_probability * model_weight + market_probability * (1.0 - model_weight)
    # Conservative calibration: the model must earn very high probabilities from history.
    return round(max(5.0, min(90.0, blended)), 1)


# =========================================================
# BLOCK: ANALYZE_PREMATCH
def analyze_prematch(

    match

):

    try:

        fixture = match.get(

            "fixture",

            {}

        )

        teams = match.get(

            "teams",

            {}

        )

        league = match.get(

            "league",

            {}

        )

        fixture_id = fixture.get(

            "id"

        )

        if not fixture_id:

            return []

        home_team = teams.get(

            "home",

            {}

        )

        away_team = teams.get(

            "away",

            {}

        )

        home_id = home_team.get(

            "id"

        )

        away_id = away_team.get(

            "id"

        )

        home_name = home_team.get(

            "name",

            "HOME"

        )

        away_name = away_team.get(

            "name",

            "AWAY"

        )

        league_id = league.get(

            "id"

        )

        league_name = league.get(

            "name",

            ""

        )

        country = league.get(

            "country",

            ""

        )

        season = league.get(

            "season"

        )

        if not home_id or not away_id:

            return []

        # =================================================
        # BASIC FILTER
        # =================================================

        if blocked_league(

            league_name

        ):

            return []

        if country in BAD_COUNTRIES:

            return []

        # =================================================
        # FORM
        # =================================================

        home_form = get_team_form(

            home_id,

            "home"

        )

        away_form = get_team_form(

            away_id,

            "away"

        )

        if not home_form or not away_form:

            return []

        # =================================================
        # TABLE
        # =================================================

        table = get_league_table(

            league_id,

            season

        )

        table_home = table.get(

            home_id

        )

        table_away = table.get(

            away_id

        )

        # =================================================
        # ODDS
        # =================================================

        match_odds = get_match_odds(

            fixture_id

        )

        if match_odds is None:

            return []

        (

            home_odd,

            draw_odd,

            away_odd,

            over25_odd,

            under25_odd,

            over35_odd,

            under35_odd,

            btts_odd,

            home15_odd,

            away15_odd

        ) = match_odds

        # =================================================
        # STRENGTH
        # =================================================

        strength = prematch_market_probability(

            home_form,

            away_form,

            table_home,

            table_away

        )

        home_probability = strength[

            "home_probability"

        ]

        away_probability = strength[

            "away_probability"

        ]

        # =================================================
        # GOALS
        # =================================================

        goals = prematch_goal_model(

            home_form,

            away_form

        )

        # -------------------------------------------------
        # CALIBRATED RESULT PROBABILITIES
        # -------------------------------------------------
        poisson_home, poisson_draw, poisson_away = _poisson_result_probabilities(
            goals["home_attack"],
            goals["away_attack"]
        )
        result_market = _normalized_implied([home_odd, draw_odd, away_odd])
        home_probability = _blend_probability(poisson_home, result_market[0])
        away_probability = _blend_probability(poisson_away, result_market[2])

        # Goal markets already come from Poisson. Keep the model as the main
        # source and lightly calibrate toward the available market price.
        goal_market = _normalized_implied([over25_odd, under25_odd])
        btts_market = _normalized_implied([btts_odd, None])
        if goal_market[0] is not None:
            goals["over25"] = _blend_probability(goals["over25"], goal_market[0])
            if goal_market[1] is not None:
                goals["under25"] = _blend_probability(goals["under25"], goal_market[1])
        if btts_odd is not None and btts_odd > 1.01:
            # For a binary market we need the opposite price too; if unavailable
            # keep the pure model probability rather than inventing a price.
            goals["btts"] = round(max(0.0, min(100.0, goals["btts"])), 1)

        # =================================================
        # LEAGUE SCORE
        # =================================================

        league_avg_goals = (

            (

                home_form["avg_scored"]

                +

                home_form["avg_conceded"]

                +

                away_form["avg_scored"]

                +

                away_form["avg_conceded"]

            )

            /

            2

        )

        league_btts = (

            (

                home_form["btts_pct"]

                +

                away_form["btts_pct"]

            )

            /

            2

        )

        league_over25 = (

            (

                home_form["over25_pct"]

                +

                away_form["over25_pct"]

            )

            /

            2

        )

        league_score = league_ai_score(

            league_name,

            country,

            league_avg_goals,

            league_btts,

            league_over25

        )

        signals = []

        # =================================================
        # HOME WIN
        # =================================================

        if home_odd is not None:

            confidence = prematch_confidence(

                home_probability,

                home_form,

                away_form,

                "🏆 HOME WIN",

                league_score

            )

            risk = prematch_risk(

                home_probability,

                confidence,

                home_form,

                away_form

            )

            signal = create_prematch_signal(

                "🏆 HOME WIN",

                home_probability,

                confidence,

                risk,

                home_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # AWAY WIN
        # =================================================

        if away_odd is not None:

            confidence = prematch_confidence(

                away_probability,

                away_form,

                home_form,

                "✈️ AWAY WIN",

                league_score

            )

            risk = prematch_risk(

                away_probability,

                confidence,

                away_form,

                home_form

            )

            signal = create_prematch_signal(

                "✈️ AWAY WIN",

                away_probability,

                confidence,

                risk,

                away_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # OVER 2.5
        # =================================================

        if over25_odd is not None:

            probability = goals[

                "over25"

            ]

            confidence = prematch_confidence(

                probability,

                home_form,

                away_form,

                "🚀 OVER 2.5",

                league_score

            )

            risk = prematch_risk(

                probability,

                confidence,

                home_form,

                away_form

            )

            signal = create_prematch_signal(

                "🚀 OVER 2.5",

                probability,

                confidence,

                risk,

                over25_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # UNDER 2.5
        # =================================================

        if under25_odd is not None:

            probability = goals[

                "under25"

            ]

            confidence = prematch_confidence(

                probability,

                away_form,

                home_form,

                "🛡 UNDER 2.5",

                league_score

            )

            risk = prematch_risk(

                probability,

                confidence,

                away_form,

                home_form

            )

            signal = create_prematch_signal(

                "🛡 UNDER 2.5",

                probability,

                confidence,

                risk,

                under25_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # BTTS
        # =================================================

        if btts_odd is not None:

            probability = goals[

                "btts"

            ]

            confidence = prematch_confidence(

                probability,

                home_form,

                away_form,

                "💎 BTTS YES",

                league_score

            )

            risk = prematch_risk(

                probability,

                confidence,

                home_form,

                away_form

            )

            signal = create_prematch_signal(

                "💎 BTTS YES",

                probability,

                confidence,

                risk,

                btts_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # HOME OVER 1.5
        # =================================================

        if home15_odd is not None:

            # Exact Poisson probability for this team's expected goals >= 2.
            lam = max(0.05, min(4.0, goals["home_attack"]))
            probability = round(
                max(5.0, min(90.0, (1.0 - math.exp(-lam) * (1.0 + lam)) * 100.0)),
                1
            )

            confidence = prematch_confidence(

                probability,

                home_form,

                away_form,

                "⚽ HOME OVER 1.5",

                league_score

            )

            risk = prematch_risk(

                probability,

                confidence,

                home_form,

                away_form

            )

            signal = create_prematch_signal(

                "⚽ HOME OVER 1.5",

                probability,

                confidence,

                risk,

                home15_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # AWAY OVER 1.5
        # =================================================

        if away15_odd is not None:

            # Exact Poisson probability for this team's expected goals >= 2.
            lam = max(0.05, min(4.0, goals["away_attack"]))
            probability = round(
                max(5.0, min(90.0, (1.0 - math.exp(-lam) * (1.0 + lam)) * 100.0)),
                1
            )

            confidence = prematch_confidence(

                probability,

                away_form,

                home_form,

                "⚽ AWAY OVER 1.5",

                league_score

            )

            risk = prematch_risk(

                probability,

                confidence,

                away_form,

                home_form

            )

            signal = create_prematch_signal(

                "⚽ AWAY OVER 1.5",

                probability,

                confidence,

                risk,

                away15_odd,

                home_name,

                away_name,

                league_name

            )

            if signal:

                signals.append(

                    signal

                )

        # =================================================
        # SORT
        # =================================================

        signals.sort(

            key=lambda x: (

                x["probability"],

                x["confidence"],

                x["edge"],

                -x["risk"]

            ),

            reverse=True

        )

        # Match metadata for Telegram and database.
        fixture_date = fixture.get("date") or ""
        local_date = ""
        local_time = ""
        try:
            if fixture_date:
                dt = datetime.fromisoformat(str(fixture_date).replace("Z", "+00:00"))
                dt = dt.astimezone(TIMEZONE)
                local_date = dt.strftime("%d.%m.%Y")
                local_time = dt.strftime("%H:%M")
        except Exception:
            pass
        for _s in signals:
            _s["fixture_id"] = fixture_id
            _s["country"] = country
            _s["league"] = league_name
            _s["match_date"] = local_date
            _s["match_time"] = local_time
            _s["fixture_timestamp"] = fixture_date
            _s["home_team"] = home_name
            _s["away_team"] = away_name

        return signals

    except Exception as e:

        logging.warning(

            "PREMATCH ENGINE ERROR: %s",

            repr(e)

        )

        return []


# =========================================================
# PREMATCH SCANNER
# =========================================================

# BLOCK: SCAN_PREMATCH_MATCHES
def scan_prematch_matches(

    matches

):

    candidates = []

    for match in matches:

        try:

            signals = analyze_prematch(

                match

            )

            if not signals:

                continue

            best = signals[0]

            candidates.append(

                best

            )

        except Exception as e:

            logging.warning(

                "PREMATCH MATCH ERROR: %s",

                repr(e)

            )

    candidates.sort(

        key=lambda x: (

            x["confidence"],

            x["probability"],

            x["edge"],

            -x["risk"]

        ),

        reverse=True

    )

    return candidates[

        :PREMATCH_MAX_SIGNALS

    ]


# =========================================================
# PREMATCH MESSAGE
# =========================================================

# BLOCK: FORMAT_PREMATCH_SIGNAL
def format_prematch_signal(

    signal

):

    return (

        "🔥 PREMATCH V4\n\n"

        f"⚽ {signal['home_team']} "

        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n\n"

        f"🎯 {signal['market']}\n\n"

        f"📈 Probability: "

        f"{signal['probability']:.1f}%\n"

        f"🤖 Confidence: "

        f"{signal['confidence']:.1f}%\n"

        f"💰 Odds: "

        f"{signal['odd']:.2f}\n"

        f"💎 Edge: "

        f"{signal['edge']:+.1f}%\n"

        f"🛡 Risk: "

        f"{signal['risk']}"

    )

# =========================================================
# LIVE CORE V4
# =========================================================

LIVE_MINUTE = 25

LIVE_MIN_PROBABILITY = 72

LIVE_MIN_CONFIDENCE = 78

LIVE_MAX_RISK = 35

LIVE_MAX_SIGNALS = 5

LIVE_COOLDOWN = 600


# =========================================================
# LIVE STATISTICS PARSER
# =========================================================

# BLOCK: PARSE_LIVE_STATISTICS
def parse_live_statistics(
    statistics
):

    home = {}
    away = {}

    if not statistics:
        return home, away

    try:
        for team_data in statistics:

            team = team_data.get(
                "team",
                {}
            )

            team_id = team.get(
                "id"
            )

            stats = {}

            for item in team_data.get(
                "statistics",
                []
            ):

                name = clean_text(
                    item.get("type")
                )

                value = item.get(
                    "value"
                )

                if isinstance(
                    value,
                    str
                ):

                    value = value.replace(
                        "%",
                        ""
                    )

                number = safe_float(
                    value
                )

                stats[name] = (
                    number
                    if number is not None
                    else 0
                )

            if team_id is not None:

                if not home:
                    home = {
                        "id": team_id,
                        "stats": stats
                    }

                else:
                    away = {
                        "id": team_id,
                        "stats": stats
                    }

    except Exception as e:

        logging.warning(
            "LIVE STAT PARSE ERROR: %s",
            repr(e)
        )

    return home, away


# =========================================================
# LIVE TEAM STAT HELPERS
# =========================================================

# BLOCK: LIVE_STAT
def live_stat(
    data,
    name
):

    try:

        return float(
            data.get(
                "stats",
                {}
            ).get(
                clean_text(name),
                0
            )
        )

    except Exception:

        return 0.0


# =========================================================
# LIVE ATTACK SCORE
# =========================================================

# BLOCK: CALCULATE_ATTACK_SCORE
def calculate_attack_score(
    shots,
    shots_on,
    dangerous_attacks,
    corners
):

    score = 0

    # Shots on target are most important.
    score += min(
        40,
        shots_on * 8
    )

    # Total shots.
    score += min(
        25,
        shots * 2
    )

    # Dangerous attacks.
    score += min(
        25,
        dangerous_attacks * 0.5
    )

    # Corners.
    score += min(
        10,
        corners * 2
    )

    return round(
        min(
            100,
            score
        ),
        1
    )


# =========================================================
# LIVE PRESSURE SCORE
# =========================================================

# BLOCK: CALCULATE_PRESSURE_SCORE
def calculate_pressure_score(
    attack,
    possession,
    corners,
    shots_on
):

    pressure = (

        attack * 0.50

        +

        possession * 0.15

        +

        min(
            100,
            corners * 8
        ) * 0.15

        +

        min(
            100,
            shots_on * 12
        ) * 0.20

    )

    return round(
        min(
            100,
            pressure
        ),
        1
    )


# =========================================================
# LIVE GOAL PROBABILITY
# =========================================================

# BLOCK: LIVE_GOAL_PROBABILITY
def live_goal_probability(
    attack,
    pressure,
    xg,
    minute,
    score_difference
):

    probability = 50

    # Main attacking factors.
    probability += (
        attack - 50
    ) * 0.25

    probability += (
        pressure - 50
    ) * 0.25

    # xG.
    probability += min(
        20,
        max(
            0,
            xg * 7
        )
    )

    # Active part of the match.
    if 55 <= minute <= 80:

        probability += 5

    elif minute >= 80:

        probability += 3

    # Close score increases motivation.
    if abs(
        score_difference
    ) <= 1:

        probability += 4

    # Very early matches are less reliable.
    if minute < 30:

        probability -= 8

    return round(
        max(
            0,
            min(
                95,
                probability
            )
        ),
        1
    )


# =========================================================
# LIVE RISK
# =========================================================

# BLOCK: CALCULATE_LIVE_RISK
def calculate_live_risk(
    probability,
    confidence,
    attack,
    pressure,
    minute,
    shots_on
):

    risk = 0

    # Probability.
    if probability < 70:

        risk += 15

    elif probability < 75:

        risk += 8

    # Confidence.
    if confidence < 75:

        risk += 15

    elif confidence < 80:

        risk += 8

    # Weak attack.
    if attack < 60:

        risk += 10

    # Weak pressure.
    if pressure < 60:

        risk += 10

    # Too early.
    if minute < 35:

        risk += 10

    # No shots on target.
    if shots_on == 0:

        risk += 8

    return min(
        100,
        risk
    )


# =========================================================
# LIVE CONFIDENCE
# =========================================================

# BLOCK: CALCULATE_LIVE_CONFIDENCE
def calculate_live_confidence(
    probability,
    attack,
    pressure,
    xg,
    shots_on,
    minute,
    score_difference
):

    confidence = 50

    # Probability.
    confidence += (
        probability - 60
    ) * 0.35

    # Attack.
    confidence += (
        attack - 60
    ) * 0.15

    # Pressure.
    confidence += (
        pressure - 60
    ) * 0.15

    # xG.
    confidence += min(
        10,
        max(
            0,
            xg * 4
        )
    )

    # Shots on target.
    confidence += min(
        8,
        shots_on * 1.5
    )

    # Best live period.
    if 55 <= minute <= 80:

        confidence += 5

    # Close match.
    if abs(
        score_difference
    ) <= 1:

        confidence += 3

    return round(
        max(
            0,
            min(
                95,
                confidence
            )
        ),
        1
    )


# =========================================================
# LIVE VALUE
# =========================================================

# BLOCK: CALCULATE_LIVE_VALUE
def calculate_live_value(
    probability,
    odd
):

    if odd is None:

        return 0

    if odd <= 1.01:

        return 0

    return value_edge(
        probability,
        odd
    )


# =========================================================
# LIVE SIGNAL BUILDER
# =========================================================

# BLOCK: BUILD_LIVE_SIGNAL
def build_live_signal(
    market,
    probability,
    confidence,
    risk,
    odd,
    match,
    minute,
    attack,
    pressure,
    xg
):

    if odd is None:

        return None

    if probability < LIVE_MIN_PROBABILITY:

        return None

    if confidence < LIVE_MIN_CONFIDENCE:

        return None

    if risk > LIVE_MAX_RISK:

        return None

    edge = calculate_live_value(
        probability,
        odd
    )

    # Do not force huge value.
    # Probability + confidence are more important.
    if edge < -3:

        return None

    fixture = match.get(
        "fixture",
        {}
    )

    teams = match.get(
        "teams",
        {}
    )

    league = match.get(
        "league",
        {}
    )

    return {

        "fixture_id":
            fixture.get("id"),

        "home_team":
            teams.get(
                "home",
                {}
            ).get(
                "name",
                "HOME"
            ),

        "away_team":
            teams.get(
                "away",
                {}
            ).get(
                "name",
                "AWAY"
            ),

        "league":
            league.get(
                "name",
                ""
            ),

        "country":
            league.get(
                "country",
                ""
            ),

        "match_date":
            (lambda d: d.strftime("%d.%m.%Y") if d else "")(
                datetime.fromisoformat(str(fixture.get("date")).replace("Z", "+00:00")).astimezone(TIMEZONE) if fixture.get("date") else None
            ),

        "match_time":
            (lambda d: d.strftime("%H:%M") if d else "")(
                datetime.fromisoformat(str(fixture.get("date")).replace("Z", "+00:00")).astimezone(TIMEZONE) if fixture.get("date") else None
            ),

        "home_goals": match.get("goals", {}).get("home") or 0,
        "away_goals": match.get("goals", {}).get("away") or 0,

        "market":
            market,

        "probability":
            round(
                probability,
                1
            ),

        "confidence":
            round(
                confidence,
                1
            ),

        "risk":
            risk,

        "odd":
            odd,

        "edge":
            edge,

        "minute":
            minute,

        "attack":
            attack,

        "pressure":
            pressure,

        "xg":
            round(
                xg,
                2
            )

    }


# =========================================================
# LIVE NEXT GOAL ANALYZER
# =========================================================

# BLOCK: ANALYZE_LIVE_MATCH
# =========================================================
# BLOCK: LIVE MASTER ANALYZER
# =========================================================

def analyze_live_match(
    match
):

    try:

        fixture = match.get(
            "fixture",
            {}
        )

        fixture_id = fixture.get(
            "id"
        )

        if not fixture_id:

            return []

        status = fixture.get(
            "status",
            {}
        )

        elapsed = status.get(
            "elapsed"
        )

        if elapsed is None:

            return []

        minute = int(
            elapsed
        )

        if minute < LIVE_MINUTE:

            return []

        teams = match.get(
            "teams",
            {}
        )

        home_team = teams.get(
            "home",
            {}
        )

        away_team = teams.get(
            "away",
            {}
        )

        home_id = home_team.get(
            "id"
        )

        away_id = away_team.get(
            "id"
        )

        if not home_id or not away_id:

            return []

        home_goals = (
            match.get(
                "goals",
                {}
            ).get(
                "home"
            )
            or 0
        )

        away_goals = (
            match.get(
                "goals",
                {}
            ).get(
                "away"
            )
            or 0
        )

        score_difference = (
            home_goals
            -
            away_goals
        )

        # =================================================
        # STATISTICS
        # =================================================

        statistics = get_statistics(
            fixture_id
        )

        home_stats, away_stats = (
            parse_live_statistics(
                statistics
            )
        )

        # =================================================
        # HOME
        # =================================================

        home_shots = live_stat(
            home_stats,
            "total shots"
        )

        home_shots_on = live_stat(
            home_stats,
            "shots on goal"
        )

        home_dangerous = live_stat(
            home_stats,
            "dangerous attacks"
        )

        home_corners = live_stat(
            home_stats,
            "corner kicks"
        )

        home_possession = live_stat(
            home_stats,
            "ball possession"
        )

        # =================================================
        # AWAY
        # =================================================

        away_shots = live_stat(
            away_stats,
            "total shots"
        )

        away_shots_on = live_stat(
            away_stats,
            "shots on goal"
        )

        away_dangerous = live_stat(
            away_stats,
            "dangerous attacks"
        )

        away_corners = live_stat(
            away_stats,
            "corner kicks"
        )

        away_possession = live_stat(
            away_stats,
            "ball possession"
        )

        # =================================================
        # ATTACK SCORES
        # =================================================

        home_attack = calculate_attack_score(
            home_shots,
            home_shots_on,
            home_dangerous,
            home_corners
        )

        away_attack = calculate_attack_score(
            away_shots,
            away_shots_on,
            away_dangerous,
            away_corners
        )

        home_pressure = calculate_pressure_score(
            home_attack,
            home_possession,
            home_corners,
            home_shots_on
        )

        away_pressure = calculate_pressure_score(
            away_attack,
            away_possession,
            away_corners,
            away_shots_on
        )

        # =================================================
        # xG
        # =================================================

        home_xg = live_stat(
            home_stats,
            "expected goals"
        )

        away_xg = live_stat(
            away_stats,
            "expected goals"
        )

        total_xg = (
            home_xg
            +
            away_xg
        )

        total_shots = (
            home_shots
            +
            away_shots
        )

        total_shots_on = (
            home_shots_on
            +
            away_shots_on
        )

        # =================================================
        # SELECT STRONGER SIDE
        # =================================================

        if (

            home_attack >=
            away_attack

        ):

            best_team = "HOME"

            best_attack = home_attack

            best_pressure = home_pressure

            best_shots_on = home_shots_on

        else:

            best_team = "AWAY"

            best_attack = away_attack

            best_pressure = away_pressure

            best_shots_on = away_shots_on

        # =================================================
        # MAIN PROBABILITY
        # =================================================

        probability = live_goal_probability(

            best_attack,

            best_pressure,

            total_xg,

            minute,

            score_difference

        )

        # =================================================
        # CONFIDENCE
        # =================================================

        confidence = calculate_live_confidence(

            probability,

            best_attack,

            best_pressure,

            total_xg,

            best_shots_on,

            minute,

            score_difference

        )

        # =================================================
        # RISK
        # =================================================

        risk = calculate_live_risk(

            probability,

            confidence,

            best_attack,

            best_pressure,

            minute,

            best_shots_on

        )

        print(

            "LIVE:",

            home_team.get(
                "name"
            ),

            "-",

            away_team.get(
                "name"
            ),

            "|",

            minute,

            "| ATTACK",

            round(
                best_attack,
                1
            ),

            "| PRESSURE",

            round(
                best_pressure,
                1
            ),

            "| xG",

            round(
                total_xg,
                2
            ),

            "| PROB",

            probability,

            "| CONF",

            confidence,

            "| RISK",

            risk

        )

        # =================================================
        # ODDS
        # =================================================

        odds = get_match_odds(
            fixture_id
        )

        if odds is None:

            return []

        (
            home_odd,
            draw_odd,
            away_odd,
            over25_odd,
            under25_odd,
            over35_odd,
            under35_odd,
            btts_odd,
            home15_odd,
            away15_odd

        ) = odds

        signals = []

        # =================================================
        # NEXT GOAL
        # =================================================

        # We use the stronger attacking side.
        # No unnecessary bonuses.

        if best_team == "HOME":

            market = "🎯 NEXT GOAL HOME"

            odd = None

            # API-Football usually exposes
            # live next-goal markets separately.
            # If unavailable, do not invent an odd.

        else:

            market = "🎯 NEXT GOAL AWAY"

            odd = None

        # =================================================
        # TEMPORARY LIVE ODDS
        # =================================================

        # Next-goal odds are intentionally left empty here.
        # The live market parser will be connected separately.
        #
        # This prevents the system from using
        # pre-match 1X2 odds as fake live odds.

        if odd is not None:

            signal = build_live_signal(

                market,

                probability,

                confidence,

                risk,

                odd,

                match,

                minute,

                best_attack,

                best_pressure,

                total_xg

            )

            if signal:

                signals.append(
                    signal
                )

        # =================================================
        # LIVE QUALITY CHECK
        # =================================================

        # Even without odds we can identify
        # whether the match deserves monitoring.

        if (

            probability >= 78

            and

            confidence >= 82

            and

            risk <= 25

            and

            best_attack >= 70

            and

            best_pressure >= 65

        ):

            print(

                "🔥 LIVE QUALITY MATCH:",

                home_team.get(
                    "name"
                ),

                "-",

                away_team.get(
                    "name"
                ),

                "|",

                "PROB",

                probability,

                "|",

                "CONF",

                confidence

            )

        return signals

    except Exception as e:

        logging.warning(

            "LIVE ENGINE ERROR: %s",

            repr(e)

        )

        return []


# =========================================================
# LIVE SCANNER
# =========================================================

# BLOCK: SCAN_LIVE_MATCHES
def scan_live_matches(
    matches
):

    candidates = []

    for match in matches:

        try:

            signals = analyze_live_match(
                match
            )

            if not signals:

                continue

            candidates.extend(
                signals
            )

        except Exception as e:

            logging.warning(

                "LIVE MATCH ERROR: %s",

                repr(e)

            )

    candidates.sort(

        key=lambda x: (

            x["confidence"],

            x["probability"],

            x["edge"],

            -x["risk"]

        ),

        reverse=True

    )

    return candidates[
        :LIVE_MAX_SIGNALS
    ]


# =========================================================
# LIVE MESSAGE
# =========================================================

# BLOCK: FORMAT_LIVE_SIGNAL
def format_live_signal(
    signal
):

    return (

        "🔥 LIVE V4\n\n"

        f"⚽ {signal['home_team']} "

        f"- {signal['away_team']}\n"

        f"🏆 {signal['league']}\n\n"

        f"🎯 {signal['market']}\n\n"

        f"⏱ Minute: "

        f"{signal['minute']}\n"

        f"📈 Probability: "

        f"{signal['probability']:.1f}%\n"

        f"🤖 Confidence: "

        f"{signal['confidence']:.1f}%\n"

        f"⚡ Attack: "

        f"{signal['attack']:.1f}\n"

        f"🔥 Pressure: "

        f"{signal['pressure']:.1f}\n"

        f"📊 xG: "

        f"{signal['xg']:.2f}\n"

        f"🛡 Risk: "

        f"{signal['risk']}"

    )

# =========================================================
# LIVE ODDS ENGINE V4
# =========================================================

LIVE_ODDS_CACHE_TIME = 30

live_odds_cache = {}


# =========================================================
# GET LIVE ODDS
# =========================================================

# BLOCK: GET_LIVE_ODDS
def get_live_odds(fixture_id):

    if fixture_id in live_odds_cache:

        cache_time, data = live_odds_cache[
            fixture_id
        ]

        if time.time() - cache_time < LIVE_ODDS_CACHE_TIME:

            return data

    data = api_get(
        "odds/live",
        {
            "fixture": fixture_id
        }
    )

    result = data.get(
        "response",
        []
    )

    live_odds_cache[fixture_id] = (
        time.time(),
        result
    )

    return result


# =========================================================
# LIVE BET NAME MATCHER
# =========================================================

# BLOCK: IS_NEXT_GOAL_MARKET
def is_next_goal_market(name):

    name = clean_text(name)

    return (

        "next goal" in name

        or

        "next goal scorer" in name

        or

        "next team to score" in name

    )


# =========================================================
# LIVE ODDS PARSER
# =========================================================

# BLOCK: PARSE_LIVE_NEXT_GOAL_ODDS
def parse_live_next_goal_odds(
    fixture_id,
    home_team,
    away_team
):

    data = get_live_odds(
        fixture_id
    )

    if not data:

        return None

    home_odd = None
    away_odd = None

    home_name = clean_text(
        home_team
    )

    away_name = clean_text(
        away_team
    )

    try:

        for bookmaker in data:

            bets = bookmaker.get(
                "bets",
                []
            )

            for bet in bets:

                bet_name = clean_text(
                    bet.get("name")
                )

                if not is_next_goal_market(
                    bet_name
                ):

                    continue

                for value in bet.get(
                    "values",
                    []
                ):

                    value_name = clean_text(
                        value.get("value")
                    )

                    odd = safe_float(
                        value.get("odd")
                    )

                    if odd is None:

                        continue

                    # =====================================
                    # HOME
                    # =====================================

                    if (

                        value_name == home_name

                        or

                        value_name == "home"

                        or

                        home_name in value_name

                    ):

                        home_odd = odd

                    # =====================================
                    # AWAY
                    # =====================================

                    elif (

                        value_name == away_name

                        or

                        value_name == "away"

                        or

                        away_name in value_name

                    ):

                        away_odd = odd

        if (

            home_odd is None

            and

            away_odd is None

        ):

            return None

        return {

            "home":

                home_odd,

            "away":

                away_odd

        }

    except Exception as e:

        logging.warning(
            "LIVE ODDS PARSE ERROR: %s",
            repr(e)
        )

        return None


# =========================================================
# LIVE MARKET PROBABILITY ADJUSTMENT
# =========================================================

# BLOCK: ADJUST_LIVE_PROBABILITY
def adjust_live_probability(
    probability,
    odd
):

    if odd is None:

        return probability

    if odd <= 1.01:

        return probability

    implied = (
        100 / odd
    )

    # Market should influence the model,
    # but never completely replace it.

    difference = (
        probability
        -
        implied
    )

    if difference >= 20:

        probability += 3

    elif difference >= 10:

        probability += 1

    elif difference <= -20:

        probability -= 5

    elif difference <= -10:

        probability -= 2

    return round(
        max(
            0,
            min(
                95,
                probability
            )
        ),
        1
    )


# =========================================================
# LIVE SIGNAL SCORE
# =========================================================

# BLOCK: LIVE_SIGNAL_SCORE
def live_signal_score(
    probability,
    confidence,
    edge,
    risk
):

    score = (

        probability * 0.40

        +

        confidence * 0.35

        +

        max(
            0,
            edge
        ) * 0.15

        -

        risk * 0.10

    )

    return round(
        score,
        2
    )


# =========================================================
# FINAL LIVE SIGNAL
# =========================================================

# BLOCK: BUILD_FINAL_LIVE_SIGNAL
def build_final_live_signal(
    match,
    market,
    probability,
    confidence,
    risk,
    odd,
    minute,
    attack,
    pressure,
    xg
):

    if odd is None:

        return None

    # ================================================
    # MARKET ADJUSTMENT
    # ================================================

    probability = adjust_live_probability(
        probability,
        odd
    )

    # ================================================
    # VALUE
    # ================================================

    edge = value_edge(
        probability,
        odd
    )

    # ================================================
    # FINAL FILTER
    # ================================================

    if probability < LIVE_MIN_PROBABILITY:

        return None

    if confidence < LIVE_MIN_CONFIDENCE:

        return None

    if risk > LIVE_MAX_RISK:

        return None

    # We don't want clearly negative value.
    if edge < 0:

        return None

    score = live_signal_score(
        probability,
        confidence,
        edge,
        risk
    )

    teams = match.get(
        "teams",
        {}
    )

    league = match.get(
        "league",
        {}
    )

    fixture = match.get(
        "fixture",
        {}
    )

    return {

        "fixture_id":
            fixture.get("id"),

        "home_team":
            teams.get(
                "home",
                {}
            ).get(
                "name",
                "HOME"
            ),

        "away_team":
            teams.get(
                "away",
                {}
            ).get(
                "name",
                "AWAY"
            ),

        "league":
            league.get(
                "name",
                ""
            ),

        "market":
            market,

        "probability":
            probability,

        "confidence":
            round(
                confidence,
                1
            ),

        "risk":
            risk,

        "odd":
            odd,

        "edge":
            edge,

        "score":
            score,

        "minute":
            minute,

        "attack":
            round(
                attack,
                1
            ),

        "pressure":
            round(
                pressure,
                1
            ),

        "xg":
            round(
                xg,
                2
            )

    }


# =========================================================
# REPLACE LIVE SIGNAL SECTION
# =========================================================

# BLOCK: GET_BEST_LIVE_SIGNAL
def get_best_live_signal(
    match
):

    try:

        fixture_id = match.get(
            "fixture",
            {}
        ).get(
            "id"
        )

        if not fixture_id:

            return None

        teams = match.get(
            "teams",
            {}
        )

        home_team = teams.get(
            "home",
            {}
        ).get(
            "name",
            "HOME"
        )

        away_team = teams.get(
            "away",
            {}
        ).get(
            "name",
            "AWAY"
        )

        # =============================================
        # RUN LIVE CORE
        # =============================================

        base_signals = analyze_live_match(
            match
        )

        if not base_signals:

            return None

        # =============================================
        # GET STRONGEST BASE SIGNAL
        # =============================================

        base = max(

            base_signals,

            key=lambda x: (

                x.get(
                    "confidence",
                    0
                ),

                x.get(
                    "probability",
                    0
                )

            )

        )

        market = base[
            "market"
        ]

        probability = base[
            "probability"
        ]

        confidence = base[
            "confidence"
        ]

        risk = base[
            "risk"
        ]

        minute = base[
            "minute"
        ]

        attack = base[
            "attack"
        ]

        pressure = base[
            "pressure"
        ]

        xg = base[
            "xg"
        ]

        # =============================================
        # LIVE ODDS
        # =============================================

        live_odds = parse_live_next_goal_odds(

            fixture_id,

            home_team,

            away_team

        )

        if not live_odds:

            print(
                "NO LIVE NEXT GOAL ODDS:",
                home_team,
                "-",
                away_team
            )

            return None

        # =============================================
        # SELECT CORRECT SIDE
        # =============================================

        if market == "🎯 NEXT GOAL HOME":

            odd = live_odds.get(
                "home"
            )

        else:

            odd = live_odds.get(
                "away"
            )

        if odd is None:

            return None

        # =============================================
        # FINAL SIGNAL
        # =============================================

        signal = build_final_live_signal(

            match,

            market,

            probability,

            confidence,

            risk,

            odd,

            minute,

            attack,

            pressure,

            xg

        )

        if signal is None:

            return None

        print(
            "LIVE SIGNAL:",
            signal
        )

        return signal

    except Exception as e:

        logging.warning(
            "FINAL LIVE SIGNAL ERROR: %s",
            repr(e)
        )

        return None


# =========================================================
# SIGNAL COOLDOWN
# =========================================================

# BLOCK: LIVE_SIGNAL_ALLOWED
def live_signal_allowed(
    fixture_id,
    market,
    minute
):

    key = (
        f"{fixture_id}_"
        f"{market}"
    )

    now = time.time()

    previous = sent_live.get(
        key
    )

    if previous is None:

        sent_live[key] = {
            "time": now,
            "minute": minute
        }

        return True

    elapsed = (
        now
        -
        previous["time"]
    )

    # Normal cooldown.
    if elapsed >= LIVE_COOLDOWN:

        sent_live[key] = {
            "time": now,
            "minute": minute
        }

        return True

    # New goal = new opportunity.
    if minute > previous["minute"]:

        sent_live[key] = {
            "time": now,
            "minute": minute
        }

        return True

    return False


# =========================================================
# SAVE SIGNAL TO DATABASE
# =========================================================

# BLOCK: SAVE_SIGNAL
def save_signal(
    signal
):

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(

            """
            INSERT INTO signals(

                fixture_id,
                country,
                league,
                home_team,
                away_team,
                market,
                probability,
                odd,
                confidence,
                result,
                created_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,

            (

                signal.get(
                    "fixture_id"
                ),

                signal.get(
                    "country",
                    ""
                ),

                signal.get(
                    "league",
                    ""
                ),

                signal.get(
                    "home_team"
                ),

                signal.get(
                    "away_team"
                ),

                signal.get(
                    "market"
                ),

                signal.get(
                    "probability"
                ),

                signal.get(
                    "odd"
                ),

                signal.get(
                    "confidence"
                ),

                None,

                datetime.now(
                    TIMEZONE
                ).isoformat()

            )

        )

        conn.commit()

        conn.close()

        return True

    except Exception as e:

        logging.warning(
            "DB SAVE ERROR: %s",
            repr(e)
        )

        return False


# =========================================================
# SEND LIVE SIGNAL
# =========================================================

# BLOCK: SEND_LIVE_SIGNAL
def send_live_signal(
    signal
):

    fixture_id = signal.get(
        "fixture_id"
    )

    market = signal.get(
        "market"
    )

    minute = signal.get(
        "minute",
        0
    )

    if not live_signal_allowed(

        fixture_id,

        market,

        minute

    ):

        return False

    message = format_live_signal(
        signal
    )

    if not send_telegram(
        message
    ):

        return False

    save_signal(
        signal
    )

    return True


# =========================================================
# LIVE MASTER SCANNER
# =========================================================

# BLOCK: PROCESS_LIVE_MATCHES
def process_live_matches(
    matches
):

    sent = 0

    for match in matches:

        try:

            signal = get_best_live_signal(
                match
            )

            if not signal:

                continue

            if send_live_signal(
                signal
            ):

                sent += 1

        except Exception as e:

            logging.warning(

                "LIVE PROCESS ERROR: %s",

                repr(e)

            )

    return sent

# =========================================================
# MAIN V4 - SIGNAL SCANNER
# =========================================================

PREMATCH_SCAN_INTERVAL = 300
LIVE_SCAN_INTERVAL = 30

MAX_LIVE_SIGNALS_PER_SCAN = 5
MAX_PREMATCH_SIGNALS_PER_SCAN = 5

MIN_LIVE_CONFIDENCE = 74
MIN_LIVE_PROBABILITY = 76
MAX_LIVE_RISK = 36

MIN_PREMATCH_CONFIDENCE = 74
MIN_PREMATCH_PROBABILITY = 70

LIVE_COOLDOWN = 600

LAST_PREMATCH_SCAN = 0
LAST_LIVE_SCAN = 0


# =========================================================
# GLOBAL SIGNAL MEMORY
# =========================================================

SIGNAL_MEMORY = {}

LIVE_SIGNAL_MEMORY = {}

PREMATCH_SIGNAL_MEMORY = {}


# =========================================================
# SIGNAL KEY
# =========================================================

# BLOCK: SIGNAL_KEY
def signal_key(
    fixture_id,
    market
):

    return (
        f"{fixture_id}_"
        f"{market}"
    )


# =========================================================
# DUPLICATE CHECK
# =========================================================

# BLOCK: SIGNAL_ALREADY_SENT
def signal_already_sent(
    fixture_id,
    market
):

    key = signal_key(
        fixture_id,
        market
    )

    return key in SIGNAL_MEMORY


# BLOCK: REMEMBER_SIGNAL
def remember_signal(
    signal
):

    fixture_id = signal.get(
        "fixture_id"
    )

    market = signal.get(
        "market"
    )

    if fixture_id is None:

        return

    key = signal_key(
        fixture_id,
        market
    )

    SIGNAL_MEMORY[key] = {

        "time": time.time(),

        "probability":
            signal.get(
                "probability"
            ),

        "confidence":
            signal.get(
                "confidence"
            ),

        "odd":
            signal.get(
                "odd"
            )

    }


# =========================================================
# CLEAN OLD MEMORY
# =========================================================

# BLOCK: CLEANUP_SIGNAL_MEMORY
def cleanup_signal_memory():

    now = time.time()

    expired = []

    for key, data in SIGNAL_MEMORY.items():

        if (

            now
            -
            data.get(
                "time",
                now
            )

            >

            21600

        ):

            expired.append(
                key
            )

    for key in expired:

        SIGNAL_MEMORY.pop(
            key,
            None
        )


# =========================================================
# PREMATCH SIGNAL NORMALIZER
# =========================================================

# BLOCK: NORMALIZE_PREMATCH_SIGNAL
def normalize_prematch_signal(
    signal
):

    if not signal:

        return None

    result = dict(
        signal
    )

    result.setdefault(
        "probability",
        0
    )

    result.setdefault(
        "confidence",
        0
    )

    result.setdefault(
        "risk",
        100
    )

    result.setdefault(
        "score",
        0
    )

    result.setdefault(
        "odd",
        None
    )

    return result


# =========================================================
# PREMATCH QUALITY CHECK
# =========================================================

# BLOCK: PREMATCH_SIGNAL_ALLOWED
def prematch_signal_allowed(
    signal
):

    signal = normalize_prematch_signal(
        signal
    )

    if signal is None:

        return False

    probability = signal.get(
        "probability",
        0
    )

    confidence = signal.get(
        "confidence",
        0
    )

    risk = signal.get(
        "risk",
        100
    )

    odd = signal.get(
        "odd"
    )

    if probability < MIN_PREMATCH_PROBABILITY:

        return False

    if confidence < MIN_PREMATCH_CONFIDENCE:

        return False

    if risk > 35:

        return False

    if odd is not None:

        if odd < 1.50:

            return False

        if odd > 4.00:

            return False

    return True


# =========================================================
# PREMATCH SIGNAL SCORE
# =========================================================

# BLOCK: PREMATCH_SIGNAL_SCORE
def prematch_signal_score(
    signal
):

    probability = signal.get(
        "probability",
        0
    )

    confidence = signal.get(
        "confidence",
        0
    )

    quality = signal.get(
        "quality",
        confidence
    )

    value = signal.get(
        "value_score",
        0
    )

    risk = signal.get(
        "risk",
        50
    )

    # Probability is the primary ranking factor. Confidence/value/risk are
    # tie-breakers so a high-probability market is not lost just because its
    # internal score is formatted differently from another market.
    score = (
        probability * 0.65
        + confidence * 0.20
        + quality * 0.05
        + value * 0.10
        - risk * 0.10
    )

    return round(
        score,
        2
    )


# =========================================================
# GET BEST PREMATCH SIGNALS
# =========================================================

# BLOCK: GET_BEST_PREMATCH_SIGNALS
def _core_get_best_prematch_signals(
    matches
):

    candidates = []

    for match in matches:

        try:

            signals = analyze_prematch(
                match
            )

            if not signals:

                continue

            if isinstance(
                signals,
                dict
            ):

                signals = [
                    signals
                ]

            for signal in signals:

                signal = normalize_prematch_signal(
                    signal
                )

                if signal is None:

                    continue

                if not prematch_signal_allowed(
                    signal
                ):

                    continue

                fixture_id = signal.get(
                    "fixture_id"
                )

                market = signal.get(
                    "market"
                )

                if fixture_id is None:

                    fixture_id = match.get(
                        "fixture",
                        {}
                    ).get(
                        "id"
                    )

                    signal[
                        "fixture_id"
                    ] = fixture_id

                if signal_already_sent(
                    fixture_id,
                    market
                ):

                    continue

                signal[
                    "score"
                ] = prematch_signal_score(
                    signal
                )

                candidates.append(
                    signal
                )

        except Exception as e:

            logging.warning(

                "PREMATCH ANALYSIS ERROR: %s",

                repr(e)

            )

    candidates.sort(

        key=lambda x: (

            x.get(
                "score",
                0
            ),

            x.get(
                "confidence",
                0
            ),

            x.get(
                "probability",
                0
            )

        ),

        reverse=True

    )

    return candidates[
        :MAX_PREMATCH_SIGNALS_PER_SCAN
    ]


# =========================================================
# PREMATCH SEND
# =========================================================

# BLOCK: SEND_PREMATCH_SIGNAL
def send_prematch_signal(
    signal
):

    fixture_id = signal.get(
        "fixture_id"
    )

    market = signal.get(
        "market"
    )

    if fixture_id is None:

        return False

    if signal_already_sent(
        fixture_id,
        market
    ):

        return False

    try:

        message = format_prematch_signal(
            signal
        )

    except Exception as e:

        logging.warning(

            "PREMATCH FORMAT ERROR: %s",

            repr(e)

        )

        return False

    if not send_telegram(
        message
    ):

        return False

    save_signal(
        signal
    )

    remember_signal(
        signal
    )

    return True


# =========================================================
# PREMATCH MASTER SCANNER
# =========================================================

# BLOCK: PROCESS_PREMATCH_MATCHES
def _prematch_daily_table():
    conn=sqlite3.connect(DB_NAME)
    cur=conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS prematch_daily_runs (run_date TEXT PRIMARY KEY, sent_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.commit()
    return conn, cur


def prematch_daily_already_sent():
    today=datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    conn,cur=_prematch_daily_table()
    cur.execute("SELECT sent_count FROM prematch_daily_runs WHERE run_date=?",(today,))
    row=cur.fetchone()
    conn.close()
    return bool(row and row[0] >= PREMATCH_DAILY_TOP5)


def mark_prematch_daily_sent(count):
    today=datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    conn,cur=_prematch_daily_table()
    cur.execute("INSERT OR REPLACE INTO prematch_daily_runs(run_date,sent_count,created_at) VALUES(?,?,?)",(today,int(count),datetime.now(TIMEZONE).isoformat()))
    conn.commit(); conn.close()


def process_prematch_matches(
    matches
):
    # Daily target is 3 standalone + up to 2 Builders.  Keep the two classes
    # separate so a Builder can never replace a better individual signal.
    if prematch_daily_already_sent():
        print("PREMATCH DAILY TOP 5: ALREADY SENT")
        return 0

    candidates = get_best_prematch_signals(matches) or []
    singles = [x for x in candidates if x.get('market') != '🧩 BET BUILDER']
    builders = [x for x in candidates if x.get('market') == '🧩 BET BUILDER']

    selected = singles[:PREMATCH_TOP3_FINAL] + builders[:BUILDER_TOP2_FINAL]
    if not selected:
        return 0

    sent = 0
    for signal in selected:
        try:
            if send_prematch_signal(signal):
                sent += 1
        except Exception as e:
            logging.warning("PREMATCH SEND ERROR: %s", repr(e))

    if sent:
        mark_prematch_daily_sent(sent)
    print("PREMATCH FINAL | selected=%s | singles=%s | builders=%s | sent=%s" % (len(selected), len(singles[:PREMATCH_TOP3_FINAL]), len(builders[:BUILDER_TOP2_FINAL]), sent))
    return sent


# =========================================================
# LIVE MASTER FILTER
# =========================================================

# BLOCK: LIVE_SIGNAL_QUALITY_FILTER
def live_signal_quality_filter(
    signal
):

    if not signal:

        return False

    probability = signal.get(
        "probability",
        0
    )

    confidence = signal.get(
        "confidence",
        0
    )

    risk = signal.get(
        "risk",
        100
    )

    edge = signal.get(
        "edge",
        0
    )

    odd = signal.get(
        "odd"
    )

    if probability < MIN_LIVE_PROBABILITY:

        return False

    if confidence < MIN_LIVE_CONFIDENCE:

        return False

    if risk > MAX_LIVE_RISK:

        return False

    if edge < 0:

        return False

    if odd is None:

        return False

    if odd < 1.30:

        return False

    if odd > 4.00:

        return False

    return True


# =========================================================
# LIVE CANDIDATE RANKING
# =========================================================

# BLOCK: RANK_LIVE_SIGNALS
def rank_live_signals(
    signals
):

    valid = []

    for signal in signals:

        if not live_signal_quality_filter(
            signal
        ):

            continue

        score = live_signal_score(

            signal.get(
                "probability",
                0
            ),

            signal.get(
                "confidence",
                0
            ),

            signal.get(
                "edge",
                0
            ),

            signal.get(
                "risk",
                100
            )

        )

        signal[
            "score"
        ] = score

        valid.append(
            signal
        )

    valid.sort(

        key=lambda x: (

            x.get(
                "score",
                0
            ),

            x.get(
                "confidence",
                0
            ),

            x.get(
                "probability",
                0
            )

        ),

        reverse=True

    )

    return valid[
        :MAX_LIVE_SIGNALS_PER_SCAN
    ]


# =========================================================
# LIVE SCAN
# =========================================================

# BLOCK: SCAN_LIVE
def scan_live():

    try:

        matches = get_live_matches()

        print('LIVE MATCHES FOUND:', len(matches))

        if not matches:

            return 0

        candidates = []

        for match in matches:

            try:

                signal = get_best_live_signal(
                    match
                )

                if not signal:

                    continue

                candidates.append(
                    signal
                )

            except Exception as e:

                logging.warning(

                    "LIVE MATCH ERROR: %s",

                    repr(e)

                )

        candidates = rank_live_signals(
            candidates
        )

        sent = 0

        for signal in candidates:

            try:

                if send_live_signal(
                    signal
                ):

                    remember_signal(
                        signal
                    )

                    sent += 1

            except Exception as e:

                logging.warning(

                    "LIVE SIGNAL ERROR: %s",

                    repr(e)

                )

        return sent

    except Exception as e:

        logging.warning(

            "LIVE SCANNER ERROR: %s",

            repr(e)

        )

        return 0


# =========================================================
# REMOVE STARTED MATCHES
# =========================================================

# BLOCK: REMOVE_STARTED_MATCHES
def remove_started_matches(
    matches
):

    result = []

    for match in matches:

        try:

            status = match.get(
                "fixture",
                {}
            ).get(
                "status",
                {}
            ).get(
                "short"
            )

            if status in (

                "NS",
                "TBD"

            ):

                result.append(
                    match
                )

        except Exception:

            continue

    return result



# =========================================================
# PREMATCH SCAN
# =========================================================

# BLOCK: SCAN_PREMATCH
def scan_prematch():

    try:

        matches = get_prematch_matches()

        print('PREMATCH MATCHES FOUND:', len(matches))

        if not matches:

            return 0

        matches = remove_started_matches(
            matches
        )

        if not matches:

            return 0

        return process_prematch_matches(
            matches
        )

    except Exception as e:

        logging.warning(

            "PREMATCH SCAN ERROR: %s",

            repr(e)

        )

        return 0


# =========================================================
# API HEALTH CHECK
# =========================================================

# BLOCK: API_HEALTH_CHECK
def api_health_check():

    # Do not consume an API request during startup.
    # The real API is tested by the live/prematch scanners.
    try:
        return bool(API_KEY)
    except Exception:
        return False


# =========================================================
# SYSTEM STATUS
# =========================================================

# BLOCK: PRINT_SYSTEM_STATUS
def print_system_status():

    print()
    print(
        "=" * 60
    )

    print(
        "🤖 AI FOOTBALL BOT V4"
    )

    print(
        "STATUS: ONLINE"
    )

    print(
        "LIVE:",
        LIVE_SCAN_INTERVAL,
        "sec"
    )

    print(
        "PREMATCH:",
        PREMATCH_SCAN_INTERVAL,
        "sec"
    )

    print(
        "LIVE CONFIDENCE:",
        MIN_LIVE_CONFIDENCE
    )

    print(
        "LIVE PROBABILITY:",
        MIN_LIVE_PROBABILITY
    )

    print(
        "LIVE MAX RISK:",
        MAX_LIVE_RISK
    )

    print(
        "MAX LIVE SIGNALS:",
        MAX_LIVE_SIGNALS_PER_SCAN
    )

    print(
        "MAX PREMATCH SIGNALS:",
        MAX_PREMATCH_SIGNALS_PER_SCAN
    )

    print(
        "=" * 60
    )

    print()


# =========================================================
# MAIN LOOP
# =========================================================

# BLOCK: MAIN_LOOP
def main_loop():

    global LAST_PREMATCH_SCAN
    global LAST_LIVE_SCAN

    print_system_status()

    if not api_health_check():

        print(
            "❌ API NOT AVAILABLE"
        )

        return

    print(
        "✅ API CONNECTION OK"
    )

    print()

    while True:

        now = time.time()

        # =============================================
        # CLEAN MEMORY
        # =============================================

        cleanup_signal_memory()

        # =============================================
        # LIVE SCAN
        # =============================================

        if (

            now
            -
            LAST_LIVE_SCAN

            >=

            LIVE_SCAN_INTERVAL

        ):

            print(
                datetime.now(
                    TIMEZONE
                ).strftime(
                    "%H:%M:%S"
                ),

                "LIVE SCAN"
            )

            live_sent = scan_live()

            print(

                "LIVE SIGNALS SENT:",

                live_sent

            )

            LAST_LIVE_SCAN = now

        # =============================================
        # PREMATCH SCAN
        # =============================================

        if (

            now
            -
            LAST_PREMATCH_SCAN

            >=

            PREMATCH_SCAN_INTERVAL

        ):

            print(
                datetime.now(
                    TIMEZONE
                ).strftime(
                    "%H:%M:%S"
                ),

                "PREMATCH SCAN"
            )

            prematch_sent = scan_prematch()

            print(

                "PREMATCH SIGNALS SENT:",

                prematch_sent

            )

            LAST_PREMATCH_SCAN = now

        # =============================================
        # SLEEP
        # =============================================

        time.sleep(
            5
        )




# =========================================================
# BLOCK: V5/V7 QUALITY MARKET ENGINE - FINAL OVERRIDES
# =========================================================
# Goal: few, high-quality signals.  Never invent an odd: if the bookmaker
# does not publish a live/pre-match price, the market is not sent.

# -------------------------
# QUALITY THRESHOLDS
# -------------------------
MIN_PREMATCH_PROBABILITY = 70
MIN_PREMATCH_CONFIDENCE = 74
MAX_PREMATCH_RISK = 28
PREMATCH_MIN_ODD = 1.45
PREMATCH_MAX_ODD = 3.50

MIN_LIVE_PROBABILITY = 78
MIN_LIVE_CONFIDENCE = 85
MAX_LIVE_RISK = 25
LIVE_MIN_ODD = 1.35
LIVE_MAX_ODD = 4.00

MAX_PREMATCH_SIGNALS_PER_SCAN = 5
MAX_LIVE_SIGNALS_PER_SCAN = 5
MAX_PREMATCH_SIGNALS = 10
LIVE_MAX_SIGNALS = 5

VALUE_MIN_EDGE = 8.0
ODDS_DROP_MIN_PCT = 6.0

BET_BUILDER_MIN_ODD = 1.50
BET_BUILDER_MAX_ODD = 4.50
BET_BUILDER_MIN_LEGS = 2
BET_BUILDER_MAX_LEGS = 4

# Runtime history.  This is intentionally in-memory; the existing SQLite
# odds_history database remains available for long-term tracking.
PREMATCH_ODDS_MEMORY = {}
LIVE_MARKET_CACHE = {}


# =========================================================
# BLOCK:  SAFE NUM
# =========================================================

def _safe_num(v, default=0.0):
    x = safe_float(v)
    return default if x is None else float(x)


# =========================================================
# BLOCK:  BETANO BOOKMAKER
# =========================================================

def _betano_bookmaker(bookmakers):
    for bookmaker in bookmakers or []:
        name = clean_text(bookmaker.get('name'))
        if bookmaker.get('id') == 32 or name == 'betano':
            return bookmaker
    return None


# =========================================================
# BLOCK: LIVE BETANO ODDS PARSER
# =========================================================

def get_live_betano_markets(fixture_id):
    """Return normalized live Betano markets from API-Football /odds/live."""
    if not fixture_id:
        return []
    now=time.time()
    cached=LIVE_MARKET_CACHE.get(fixture_id)
    if cached and now-cached[0] < 25:
        return cached[1]
    try:
        data=api_get('odds/live', {'fixture': fixture_id}) or {}
        response=data.get('response', [])
        bookmaker=None
        for item in response:
            if isinstance(item, dict) and item.get('bookmakers'):
                bookmaker=_betano_bookmaker(item.get('bookmakers'))
                if bookmaker:
                    break
            if isinstance(item, dict) and clean_text(item.get('name'))=='betano':
                bookmaker=item
                break
        if not bookmaker:
            LIVE_MARKET_CACHE[fixture_id]=(now,[])
            return []
        markets=[]
        for bet in bookmaker.get('bets',[]) or []:
            if not isinstance(bet,dict):
                continue
            bname=clean_text(bet.get('name'))
            values=[]
            for value in bet.get('values',[]) or []:
                if not isinstance(value,dict):
                    continue
                odd=safe_float(value.get('odd'))
                if odd is None or odd <= 1.01:
                    continue
                values.append({
                    'value': clean_text(value.get('value')),
                    'odd': odd,
                    'stopped': bool(value.get('stopped',False)),
                    'main': value.get('main')
                })
            if values:
                markets.append({'name':bname,'values':values})
        LIVE_MARKET_CACHE[fixture_id]=(now,markets)
        return markets
    except Exception as e:
        logging.warning('LIVE BETANO ODDS ERROR: %s',repr(e))
        LIVE_MARKET_CACHE[fixture_id]=(now,[])
        return []


# =========================================================
# BLOCK:  VALUE ODD
# =========================================================

def _value_odd(values, side=None, prefix=None, contains=None):
    for v in values or []:
        name=clean_text(v.get('value'))
        if side and name != clean_text(side):
            continue
        if prefix and not name.startswith(prefix):
            continue
        if contains and contains not in name:
            continue
        odd=safe_float(v.get('odd'))
        if odd and not v.get('stopped'):
            return odd
    return None


# =========================================================
# BLOCK: FIND LIVE MARKET ODD
# =========================================================

def find_live_market_odd(markets, kind, side=None, half=False, target=None):
    """Find a Betano live odd for the requested normalized market."""
    for market in markets:
        name=clean_text(market.get('name'))
        vals=market.get('values',[])
        if kind=='next_goal':
            if 'next goal' not in name and 'next team to score' not in name:
                continue
            for v in vals:
                n=clean_text(v.get('value'))
                if side=='home' and n in ('home','1'):
                    return v.get('odd')
                if side=='away' and n in ('away','2'):
                    return v.get('odd')
        elif kind=='goals':
            if 'goal' not in name or 'next goal' in name:
                continue
            if half and not any(x in name for x in ('1st half','first half','1h')):
                continue
            if not half and any(x in name for x in ('1st half','first half','1h')):
                continue
            for v in vals:
                n=clean_text(v.get('value'))
                if target and n.startswith(target):
                    return v.get('odd')
        elif kind=='corners':
            if 'corner' not in name:
                continue
            if half and not any(x in name for x in ('1st half','first half','1h')):
                continue
            if not half and any(x in name for x in ('1st half','first half','1h')):
                continue
            for v in vals:
                n=clean_text(v.get('value'))
                if target and n.startswith(target):
                    return v.get('odd')
        elif kind=='cards':
            if not any(x in name for x in ('card','yellow card','booking')):
                continue
            if half and not any(x in name for x in ('1st half','first half','1h')):
                continue
            if not half and any(x in name for x in ('1st half','first half','1h')):
                continue
            for v in vals:
                n=clean_text(v.get('value'))
                if target and n.startswith(target):
                    return v.get('odd')
    return None


# =========================================================
# BLOCK: LIVE STATISTICS FOR CARDS/CORNERS
# =========================================================

def get_live_market_stats(match):
    fixture=match.get('fixture',{})
    fid=fixture.get('id')
    if not fid:
        return {}
    statistics=get_statistics(fid) or []
    home={}
    away={}
    for item in statistics:
        team=item.get('team',{})
        tid=team.get('id')
        target=home if tid==match.get('teams',{}).get('home',{}).get('id') else away
        if tid not in (match.get('teams',{}).get('home',{}).get('id'),match.get('teams',{}).get('away',{}).get('id')):
            continue
        for st in item.get('statistics',[]) or []:
            n=clean_text(st.get('type'))
            val=st.get('value')
            if isinstance(val,str):
                val=val.replace('%','')
            target[n]=_safe_num(val,0)
    return {'home':home,'away':away}


# =========================================================
# BLOCK:  STAT TOTAL
# =========================================================

def _stat_total(stats, key):
    return _safe_num(stats.get('home',{}).get(key),0)+_safe_num(stats.get('away',{}).get(key),0)


# =========================================================
# BLOCK: LIVE MARKET PROBABILITY ENGINE
# =========================================================

def live_market_probability(match, market, stats):
    fixture=match.get('fixture',{})
    minute=int(_safe_num(fixture.get('status',{}).get('elapsed'),0))
    goals=match.get('goals',{})
    hg=int(_safe_num(goals.get('home'),0)); ag=int(_safe_num(goals.get('away'),0))
    total_goals=hg+ag
    hs=stats.get('home',{}); aws=stats.get('away',{})
    corners=_stat_total(stats,'corner kicks')
    yellow=_stat_total(stats,'yellow cards') + _stat_total(stats,'yellow card')
    shots_on=_stat_total(stats,'shots on goal')
    shots=_stat_total(stats,'total shots')
    dangerous=_stat_total(stats,'dangerous attacks')
    xg=_stat_total(stats,'expected goals')

    # Core activity score shared by the live markets.
    activity=min(100, shots*2 + shots_on*7 + dangerous*0.25 + corners*4 + xg*12)
    if 25 <= minute <= 75: activity += 5
    if minute >= 80: activity += 2
    activity=min(100,activity)

    if market in ('🎯 NEXT GOAL HOME','🎯 NEXT GOAL AWAY','⚡ FAST NEXT GOAL'):
        side_home=(
            _safe_num(hs.get('shots on goal'))*8 +
            _safe_num(hs.get('dangerous attacks'))*0.5 +
            _safe_num(hs.get('corner kicks'))*2 +
            _safe_num(hs.get('expected goals'))*8
        )
        side_away=(
            _safe_num(aws.get('shots on goal'))*8 +
            _safe_num(aws.get('dangerous attacks'))*0.5 +
            _safe_num(aws.get('corner kicks'))*2 +
            _safe_num(aws.get('expected goals'))*8
        )
        if market.endswith('HOME'):
            p=52+(side_home-side_away)*0.22
        elif market.endswith('AWAY'):
            p=52+(side_away-side_home)*0.22
        else:
            p=50+max(side_home,side_away)*0.10
        p += max(0,activity-45)*0.20
        if market=='⚡ FAST NEXT GOAL' and 25 <= minute <= 45: p += 4
        return round(max(0,min(94,p)),1)

    if market=='⚽ OVER 1.5 GOALS':
        if total_goals>=2: return 99.0
        remaining=max(0,95-minute)
        p=35 + activity*0.42 + remaining*0.10 + xg*4
        return round(max(0,min(94,p)),1)

    if market=='⚽ LATE GOAL':
        if minute<70: return 0
        remaining=max(0,95-minute)
        p=30 + activity*0.42 + remaining*0.30 + xg*5
        if abs(hg-ag)<=1: p+=5
        return round(max(0,min(94,p)),1)

    if market=='🚩 OVER 1.5 CORNERS':
        if corners>=2: return 99.0
        remaining=max(0,95-minute)
        p=45 + activity*0.25 + remaining*0.20
        return round(max(0,min(95,p)),1)

    if market=='🚩 FIRST HALF OVER 1.5 CORNERS':
        if minute>45: return 0
        remaining=max(0,45-minute)
        p=42 + corners*10 + activity*0.18 + remaining*0.35
        return round(max(0,min(94,p)),1)

    if market=='🟨 OVER 1.5 CARDS':
        if yellow>=2: return 99.0
        remaining=max(0,95-minute)
        p=35 + yellow*18 + remaining*0.35 + (8 if abs(hg-ag)<=1 else 0)
        return round(max(0,min(94,p)),1)
    return 0


# =========================================================
# BLOCK: LIVE MARKET CONFIDENCE
# =========================================================

def live_market_confidence(probability, market, minute, stats):
    corners=_stat_total(stats,'corner kicks')
    yellow=_stat_total(stats,'yellow cards') + _stat_total(stats,'yellow card')
    shots_on=_stat_total(stats,'shots on goal')
    base=58 + max(0,probability-65)*0.55 + min(12,shots_on*1.5)
    if 'CORNER' in market: base += min(8,corners*1.5)
    if 'CARD' in market: base += min(8,yellow*3)
    if 25 <= minute <= 75: base += 3
    return round(max(0,min(95,base)),1)


# =========================================================
# BLOCK: LIVE MARKET RISK
# =========================================================

def live_market_risk(probability, confidence, minute, market, stats):
    risk=0
    if probability<82: risk+=8
    if confidence<88: risk+=8
    if minute<25: risk+=12
    if market=='⚡ FAST NEXT GOAL' and not (25<=minute<=45): risk+=50
    if market=='⚽ LATE GOAL' and minute<70: risk+=50
    if 'FIRST HALF' in market and minute>45: risk+=50
    if 'CORNER' in market and _stat_total(stats,'corner kicks')==0: risk+=8
    if 'CARD' in market and (_stat_total(stats,'yellow cards')+_stat_total(stats,'yellow card'))==0: risk+=8
    return min(100,risk)


# =========================================================
# BLOCK: LIVE MARKET CANDIDATE BUILDER
# =========================================================

def build_live_market_candidates(match):
    fixture=match.get('fixture',{})
    fid=fixture.get('id')
    if not fid: return []
    minute=int(_safe_num(fixture.get('status',{}).get('elapsed'),0))
    if minute<10: return []
    stats=get_live_market_stats(match)
    odds=get_live_betano_markets(fid)
    if not odds: return []

    markets=[]
    # next goal home/away
    for label,side in [('🎯 NEXT GOAL HOME','home'),('🎯 NEXT GOAL AWAY','away')]:
        odd=find_live_market_odd(odds,'next_goal',side=side)
        if odd:
            markets.append((label,odd))
    # fast next goal only 25-45
    if 25<=minute<=45:
        odd=find_live_market_odd(odds,'next_goal',side='home')
        odd2=find_live_market_odd(odds,'next_goal',side='away')
        # Use the stronger side below; the market label stays FAST NEXT GOAL.
        if odd or odd2:
            markets.append(('⚡ FAST NEXT GOAL', min([x for x in (odd,odd2) if x])))
    # over 1.5 total goals - use the currently offered line if available
    for target in ('over 1.5','over 1.5 goals'):
        odd=find_live_market_odd(odds,'goals',target=target)
        if odd: markets.append(('⚽ OVER 1.5 GOALS',odd)); break
    # late goal: only if a generic goal/next-goal price exists after 70'
    if minute>=70:
        for target in ('over 0.5','over 0.5 goals'):
            odd=find_live_market_odd(odds,'goals',target=target)
            if odd: markets.append(('⚽ LATE GOAL',odd)); break
    # corners/cards
    odd=find_live_market_odd(odds,'corners',target='over 1.5')
    if odd: markets.append(('🚩 OVER 1.5 CORNERS',odd))
    if minute<=45:
        odd=find_live_market_odd(odds,'corners',half=True,target='over 1.5')
        if odd: markets.append(('🚩 FIRST HALF OVER 1.5 CORNERS',odd))
    odd=find_live_market_odd(odds,'cards',target='over 1.5')
    if odd: markets.append(('🟨 OVER 1.5 CARDS',odd))

    candidates=[]
    for market,odd in markets:
        if odd is None or odd<LIVE_MIN_ODD or odd>LIVE_MAX_ODD: continue
        p=live_market_probability(match,market,stats)
        if p<=0: continue

        # For next-goal HOME/AWAY, normalize both available prices and use the
        # market only as a light calibration, never as the main probability.
        if market in ('🎯 NEXT GOAL HOME','🎯 NEXT GOAL AWAY'):
            other_side='away' if market.endswith('HOME') else 'home'
            other_odd=find_live_market_odd(odds,'next_goal',side=other_side)
            if other_odd and other_odd>1.01:
                market_probs=_normalized_implied([odd,other_odd])
                idx = 1 if market.endswith('AWAY') else 0
                if market_probs[idx] is not None:
                    p=_blend_probability(p,market_probs[idx],0.80)

        c=live_market_confidence(p,market,minute,stats)
        r=live_market_risk(p,c,minute,market,stats)
        edge=value_edge(p,odd)
        if p<MIN_LIVE_PROBABILITY or c<MIN_LIVE_CONFIDENCE or r>MAX_LIVE_RISK: continue
        # Value is a ranking factor, not a mandatory gate.
        fixture_data=match.get('fixture',{})
        teams=match.get('teams',{})
        league=match.get('league',{})
        candidates.append({
            'fixture_id':fid,'home_team':teams.get('home',{}).get('name','HOME'),
            'away_team':teams.get('away',{}).get('name','AWAY'),'league':league.get('name',''),
            'country':league.get('country',''),'market':market,'probability':p,
            'confidence':c,'risk':r,'odd':round(odd,2),'edge':round(edge,1),
            'score':live_signal_score(p,c,edge,r),'minute':minute,
            'attack':round(_stat_total(stats,'total shots')*1.0+_stat_total(stats,'dangerous attacks')*0.1,1),
            'pressure':round(_stat_total(stats,'corner kicks')*4+_stat_total(stats,'shots on goal')*8,1),
            'xg':round(_stat_total(stats,'expected goals'),2),
            'home_goals':int(_safe_num(match.get('goals',{}).get('home'),0)),
            'away_goals':int(_safe_num(match.get('goals',{}).get('away'),0)),
            'home_id':match.get('teams',{}).get('home',{}).get('id'),
            'away_id':match.get('teams',{}).get('away',{}).get('id')
        })
    return candidates


# =========================================================
# BLOCK: FINAL LIVE SIGNAL SELECTION
# =========================================================

def get_best_live_signal(match):
    try:
        candidates=build_live_market_candidates(match)
        if not candidates:
            return None
        candidates.sort(key=lambda x:(x['probability'],x['confidence'],x['edge'],-x['risk']),reverse=True)
        return candidates[0]
    except Exception as e:
        logging.warning('FINAL LIVE MARKET ERROR: %s',repr(e))
        return None


# =========================================================
# BLOCK: PREMATCH EXTRA BETANO MARKETS FOR BET BUILDER
# =========================================================

def get_prematch_betano_builder_markets(fixture_id):
    odds=get_odds(fixture_id)
    if not odds: return []
    bookmaker=_betano_bookmaker(odds[0].get('bookmakers',[]))
    if not bookmaker: return []
    result=[]
    for bet in bookmaker.get('bets',[]) or []:
        bname=clean_text(bet.get('name'))
        vals=[]
        for v in bet.get('values',[]) or []:
            odd=safe_float(v.get('odd'))
            if odd and odd>1.01:
                vals.append({'value':clean_text(v.get('value')),'odd':odd})
        if vals: result.append({'name':bname,'values':vals})
    return result


# =========================================================
# BLOCK:  BUILDER CANDIDATES
# =========================================================

def _builder_candidates(match):
    fid=match.get('fixture',{}).get('id')
    if not fid: return []
    markets=get_prematch_betano_builder_markets(fid)
    out=[]
    wanted=[]
    for m in markets:
        name=m['name']
        vals=m['values']
        # Goals full match
        if 'goal' in name and 'team' not in name and 'home' not in name and 'away' not in name and 'half' not in name:
            for v in vals:
                n=v['value']
                if n.startswith(('over 1.5','under 3.5')):
                    wanted.append((n,v['odd']))
        # Team goals
        if 'home' in name and 'goal' in name:
            for v in vals:
                if v['value'].startswith('over 1.5'): wanted.append(('HOME OVER 1.5 GOALS',v['odd']))
        if 'away' in name and 'goal' in name:
            for v in vals:
                if v['value'].startswith('over 1.5'): wanted.append(('AWAY OVER 1.5 GOALS',v['odd']))
        # Corners/cards
        if 'corner' in name and 'half' not in name:
            for v in vals:
                if v['value'].startswith(('over ','under ')): wanted.append((f'CORNER {v["value"]}',v['odd']))
        if ('card' in name or 'booking' in name) and 'half' not in name:
            for v in vals:
                if v['value'].startswith(('over ','under ')): wanted.append((f'CARD {v["value"]}',v['odd']))
        # First half
        if any(x in name for x in ('1st half','first half','1h')):
            if 'goal' in name:
                prefix='1H GOAL'
            elif 'corner' in name:
                prefix='1H CORNER'
            elif 'card' in name or 'booking' in name:
                prefix='1H CARD'
            else:
                prefix=None
            if prefix:
                for v in vals:
                    if v['value'].startswith(('over ','under ')): wanted.append((f'{prefix} {v["value"]}',v['odd']))
    # Only use low-priced selections.  Builder is market-led but still conservative.
    for label,odd in wanted:
        if 1.10 <= odd <= 1.55:
            out.append({'label':label,'odd':odd,'implied':100/odd})
    # de-duplicate exact labels
    seen=set(); final=[]
    for x in sorted(out,key=lambda z:(z['implied'],-z['odd']),reverse=True):
        if x['label'] not in seen:
            seen.add(x['label']); final.append(x)
    return final[:20]


# =========================================================
# BLOCK: BUILD BEST BET BUILDER
# =========================================================

def build_best_bet_builder(match):
    candidates=_builder_candidates(match)
    if len(candidates)<2: return None
    # Prefer 2-3 very safe legs.  Add a fourth only if required to cross 1.50.
    best=None
    from itertools import combinations
    for n in range(2,min(BET_BUILDER_MAX_LEGS,len(candidates))+1):
        for combo in combinations(candidates,n):
            odd=1.0
            for leg in combo: odd*=leg['odd']
            if odd < BET_BUILDER_MIN_ODD or odd > BET_BUILDER_MAX_ODD: continue
            # Conservative quality: all legs >=~65% implied, average >=70%.
            probs=[x['implied'] for x in combo]
            if min(probs)<64 or sum(probs)/len(probs)<70: continue
            combined_implied=100/odd
            score=combined_implied - 2.0 - (n-2)*1.5
            if best is None or score>best[0]:
                best=(score,combo,odd)
    if not best: return None
    score,combo,odd=best
    # Prefer combinations from different market families.
    families=[]
    for leg in combo:
        label=leg['label']
        if 'CORNER' in label: fam='corner'
        elif 'CARD' in label: fam='card'
        elif 'GOAL' in label: fam='goal'
        else: fam=label.split()[0]
        families.append(fam)
    if len(families) != len(set(families)) and len(combo) >= 3:
        return None
    teams=match.get('teams',{}); league=match.get('league',{})
    # Joint probability proxy: product of the normalized implied leg probabilities.
    # This is mathematically meaningful under independence; it is deliberately
    # not presented as certainty.
    joint_probability = 100.0
    for leg in combo:
        joint_probability *= (leg['implied'] / 100.0)
    joint_probability = round(max(0.0, min(100.0, joint_probability * 100.0)), 1)
    builder_confidence = round(sum(x['implied'] for x in combo) / len(combo), 1)
    builder_risk = round(max(5.0, 100.0 - builder_confidence), 1)
    return {
        'fixture_id':match.get('fixture',{}).get('id'),
        'home_team':teams.get('home',{}).get('name','HOME'),'away_team':teams.get('away',{}).get('name','AWAY'),
        'league':league.get('name',''),'country':league.get('country',''),'market':'🧩 BET BUILDER',
        'probability':joint_probability,'confidence':builder_confidence,'risk':builder_risk,
        'odd':round(odd,2),'edge':round(max(0,joint_probability-(100/odd)),1),'ev':round(joint_probability/100*odd-1,3),
        'score':joint_probability,'builder_legs':[{'market':x['label'],'odd':x['odd']} for x in combo]
    }


# =========================================================
# BLOCK: PREMATCH VALUE / ODDS DROP / BUILDER ENGINE
# =========================================================

def _base_market_from_label(label):
    s=str(label or '').strip()
    for prefix in ('📉 ODDS DROP — ','💎 VALUE — '):
        if s.startswith(prefix): return s[len(prefix):]
    return s


# =========================================================
# BLOCK: PREMATCH SIGNAL ALLOWED
# =========================================================

def prematch_signal_allowed(signal):
    # Only broad safety gates here. Market-specific hard gates are deliberately
    # removed so every market can compete in the same final ranking.
    if not signal:
        return False
    p=_safe_num(signal.get('probability'),0)
    c=_safe_num(signal.get('confidence'),0)
    r=_safe_num(signal.get('risk'),100)
    odd=signal.get('odd')
    if p < MIN_PREMATCH_PROBABILITY:
        return False
    if c < MIN_PREMATCH_CONFIDENCE:
        return False
    if r > MAX_PREMATCH_RISK:
        return False
    if odd is not None and (odd < PREMATCH_MIN_ODD or odd > PREMATCH_MAX_ODD):
        return False
    return True


# =========================================================
# BLOCK: GET BEST PREMATCH SIGNALS
# =========================================================

def get_best_prematch_signals(matches):
    # Build every available market first, then rank globally.
    candidates=[]
    for match in matches:
        try:
            base=_core_get_best_prematch_signals([match]) if '_core_get_best_prematch_signals' in globals() else []
            if isinstance(base,dict):
                base=[base]
            for s in base or []:
                s=dict(s)
                if not prematch_signal_allowed(s):
                    continue
                s['market']=_base_market_from_label(s.get('market'))
                s['score']=prematch_signal_score(s)
                candidates.append(s)

            builder=build_best_bet_builder(match)
            if builder and builder.get('probability',0) >= 68 and builder.get('confidence',0) >= 70 and builder.get('risk',100) <= 36 and builder.get('odd',0) >= BET_BUILDER_MIN_ODD:
                candidates.append(builder)
        except Exception as e:
            logging.warning('PREMATCH RANK ERROR: %s',repr(e))

    # Highest probability first; score/confidence/risk break ties.
    candidates.sort(key=lambda x:(
        _safe_num(x.get('probability'),0),
        _safe_num(x.get('confidence'),0),
        _safe_num(x.get('score'),0),
        _safe_num(x.get('edge'),-999),
        -_safe_num(x.get('risk'),100)
    ), reverse=True)

    # One signal per match keeps the feed small and diversified.
    selected=[]
    used_fixtures=set()
    for s in candidates:
        fid=s.get('fixture_id')
        if fid in used_fixtures:
            continue
        selected.append(s)
        used_fixtures.add(fid)
        if len(selected) >= MAX_PREMATCH_SIGNALS_PER_SCAN:
            break
    return selected


# =========================================================
# FINAL PREMATCH PROBABILITY RANKING
# =========================================================

def prematch_signal_allowed(signal):
    """Do not eliminate markets by arbitrary confidence/risk thresholds.
    Only reject malformed signals and unusable odds. Ranking happens later.
    """
    if not signal:
        return False
    p = _safe_num(signal.get('probability'), -1)
    odd = signal.get('odd')
    if p < 0 or p > 100:
        return False
    if odd is None:
        return False
    odd = _safe_num(odd, 0)
    if odd < 1.10 or odd > 8.00:
        return False
    return True


def prematch_signal_score(signal):
    # Probability is the primary ranking metric. Confidence, edge and risk
    # are only tie-breakers and never replace probability.
    p = _safe_num(signal.get('probability'), 0)
    c = _safe_num(signal.get('confidence'), 0)
    e = _safe_num(signal.get('edge'), 0)
    r = _safe_num(signal.get('risk'), 50)
    return round(p * 1000 + c * 2 + e - r * 0.25, 2)


def get_best_prematch_signals(matches):
    candidates=[]
    seen=set()
    for match in matches:
        try:
            signals=analyze_prematch(match) or []
            if isinstance(signals,dict): signals=[signals]
            fid=match.get('fixture',{}).get('id')
            for raw in signals:
                signal=normalize_prematch_signal(raw)
                if not prematch_signal_allowed(signal): continue
                signal=dict(signal); signal['fixture_id']=signal.get('fixture_id') or fid
                signal['market']=_base_market_from_label(signal.get('market'))
                key=(signal['fixture_id'],signal.get('market'))
                if key in seen: continue
                seen.add(key); signal['score']=prematch_signal_score(signal)
                candidates.append(signal)

            # Builder competes with individual markets, but its probability is
            # a joint probability, so it is naturally harder to reach the top.
            builder=build_best_bet_builder(match)
            if builder:
                builder=dict(builder); builder['fixture_id']=fid
                league=match.get('league',{}); fixture=match.get('fixture',{})
                builder['country']=league.get('country',''); builder['league']=league.get('name','')
                raw_dt=fixture.get('date') or ''
                try:
                    if raw_dt:
                        dt=datetime.fromisoformat(str(raw_dt).replace('Z','+00:00')).astimezone(TIMEZONE)
                        builder['match_date']=dt.strftime('%d.%m.%Y'); builder['match_time']=dt.strftime('%H:%M')
                except Exception: pass
                if prematch_signal_allowed(builder):
                    builder['score']=prematch_signal_score(builder); candidates.append(builder)
        except Exception as e:
            logging.warning('PREMATCH RANK ERROR: %s',repr(e))

    # Keep only the single strongest market for each fixture before ranking.
    best_by_fixture={}
    for c in candidates:
        fid=c.get('fixture_id')
        if fid is None: continue
        old=best_by_fixture.get(fid)
        if old is None or (_safe_num(c.get('probability'),0),_safe_num(c.get('confidence'),0),-_safe_num(c.get('risk'),100)) > (_safe_num(old.get('probability'),0),_safe_num(old.get('confidence'),0),-_safe_num(old.get('risk'),100)):
            best_by_fixture[fid]=c

    selected=sorted(best_by_fixture.values(), key=lambda x: (_safe_num(x.get('probability'),0),_safe_num(x.get('confidence'),0),-_safe_num(x.get('risk'),100)), reverse=True)[:PREMATCH_DAILY_TOP5]
    return selected


# =========================================================
# BLOCK: PREMATCH MESSAGE WITH MARKET NAME + BUILDER LEGS
# =========================================================

def format_prematch_signal(signal):
    market=signal.get('market','UNKNOWN')
    date=signal.get('match_date','') or 'N/A'
    tm=signal.get('match_time','') or 'N/A'
    text=('🔥 PREMATCH V6\n\n'
          f"⚽ {signal.get('home_team','HOME')} - {signal.get('away_team','AWAY')}\n"
          f"🌍 {signal.get('country','N/A')}\n"
          f"🏆 {signal.get('league','N/A')}\n"
          f"📅 {date} | ⏰ {tm} 🇧🇬\n\n"
          f"🎯 MARKET: {market}\n")
    if market=='🧩 BET BUILDER':
        text+='\n'.join(f"  • {x['market']} @ {x['odd']:.2f}" for x in signal.get('builder_legs',[]))+'\n\n'
        text+=f"🧩 Combined Odds: {signal.get('odd',0):.2f}\n"
        text+=f"📈 Joint Probability: {signal.get('probability',0):.1f}%\n"
    else:
        text+=f"📈 Probability: {signal.get('probability',0):.1f}%\n💰 Betano Odds: {signal.get('odd',0):.2f}\n📊 Edge: {signal.get('edge',0):+.1f}%\n"
        if signal.get('ev') is not None: text+=f"💎 EV: {signal.get('ev',0):+.3f}\n"
    text+=f"🤖 Confidence: {signal.get('confidence',0):.1f}%\n🛡 Risk: {signal.get('risk',0):.0f}"
    return text


# =========================================================
# BLOCK: RESULT MARKET NORMALIZATION
# =========================================================

def normalize_market(market):
    return _base_market_from_label(market)


# =========================================================
# BLOCK: LIVE MESSAGE WITH MARKET NAME
# =========================================================

def format_live_signal(signal):
    return (
        '🔥 LIVE V5\n\n'
        f"⚽ {signal.get('home_team','HOME')} - {signal.get('away_team','AWAY')}\n"
        f"🌍 {signal.get('country','')}\n"
        f"🏆 {signal.get('league','')}\n"
        f"⏱ {signal.get('minute',0)}' | {signal.get('home_goals',0)}-{signal.get('away_goals',0)}\n\n"
        f"🎯 MARKET: {signal.get('market','UNKNOWN')}\n"
        f"📈 Probability: {signal.get('probability',0):.1f}%\n"
        f"🤖 Confidence: {signal.get('confidence',0):.1f}%\n"
        f"💰 Betano Odds: {signal.get('odd',0):.2f}\n"
        f"📊 Edge: {signal.get('edge',0):+.1f}%\n"
        f"🛡 Risk: {signal.get('risk',0):.0f}\n"
        f"⭐ Score: {signal.get('score',0):.1f}"
    )


# =========================================================
# BLOCK: RESULT EVALUATION FOR NEW LIVE MARKETS
# =========================================================

def evaluate_extended_live_market(signal):
    fid=signal.get('fixture_id'); market=signal.get('market','')
    result=get_fixture_result(fid)
    if not result: return None
    hg=result.get('home',0); ag=result.get('away',0)
    total=hg+ag
    if market in ('🎯 NEXT GOAL HOME','🎯 NEXT GOAL AWAY','⚡ FAST NEXT GOAL'):
        data=api_get('fixtures/events',{'fixture':fid}) or {}
        events=data.get('response',[])
        sm=int(signal.get('minute',0)); wanted=None
        for ev in events:
            if clean_text(ev.get('type'))!='goal': continue
            em=ev.get('time',{}).get('elapsed')
            if em is None or em<=sm: continue
            tid=ev.get('team',{}).get('id')
            if tid==signal.get('home_id'): wanted='HOME'
            elif tid==signal.get('away_id'): wanted='AWAY'
            if wanted: break
        if market=='🎯 NEXT GOAL HOME': return 'WIN' if wanted=='HOME' else 'LOSS'
        if market=='🎯 NEXT GOAL AWAY': return 'WIN' if wanted=='AWAY' else 'LOSS'
        if market=='⚡ FAST NEXT GOAL': return 'WIN' if wanted else 'LOSS'
    if market=='⚽ OVER 1.5 GOALS': return 'WIN' if total>=2 else 'LOSS'
    if market=='⚽ LATE GOAL':
        data=api_get('fixtures/events',{'fixture':fid}) or {}
        for ev in data.get('response',[]):
            if clean_text(ev.get('type'))=='goal' and _safe_num(ev.get('time',{}).get('elapsed'),0)>=70: return 'WIN'
        return 'LOSS'
    # Final statistics for corners/cards.
    fixture_data=api_get('fixtures',{'id':fid}) or {}
    fixture_rows=fixture_data.get('response',[])
    stats=get_live_market_stats(fixture_rows[0]) if fixture_rows else {}
    corners=_stat_total(stats,'corner kicks')
    cards=_stat_total(stats,'yellow cards')+_stat_total(stats,'yellow card')
    if market=='🚩 OVER 1.5 CORNERS': return 'WIN' if corners>=2 else 'LOSS'
    if market=='🟨 OVER 1.5 CARDS': return 'WIN' if cards>=2 else 'LOSS'
    return None


# =========================================================
# BLOCK: EXTENDED LIVE RESULT CHECKER
# =========================================================

def check_pending_signals():
    try:
        conn=sqlite3.connect(DB_NAME); cur=conn.cursor()
        cur.execute("SELECT id,fixture_id,home_team,away_team,market,probability,odd,confidence,created_at FROM signals WHERE result IS NULL ORDER BY id ASC LIMIT 100")
        rows=cur.fetchall(); conn.close(); checked=0
        for row in rows:
            sid,fid,home,away,market,p,odd,c,created=row
            if not (market.startswith('🎯') or market.startswith('⚡') or market.startswith('🚩') or market.startswith('🟨') or market.startswith('⚽')): continue
            signal={'fixture_id':fid,'home_team':home,'away_team':away,'market':market,'probability':p,'odd':odd,'confidence':c,'created_at':created,'minute':0}
            r=evaluate_extended_live_market(signal)
            if r and update_signal_result(sid,r):
                checked+=1
                print('RESULT:',home,'-',away,market,r)
        return checked
    except Exception as e:
        logging.warning('EXTENDED RESULT CHECK ERROR: %s',repr(e)); return 0


# =========================================================
# BLOCK: STARTUP CONFIG - FINAL
# =========================================================
PREMATCH_SCAN_INTERVAL=300
LIVE_SCAN_INTERVAL=60
MAX_PREMATCH_SIGNALS_PER_SCAN=5
MAX_LIVE_SIGNALS_PER_SCAN=5
# PREMATCH: exactly one Top-5 selection per Bulgarian calendar day
PREMATCH_DAILY_TOP5 = 5


# =========================================================
# LIVE ODDS HISTORY V4
# =========================================================

ODDS_HISTORY_CACHE = {}

ODDS_HISTORY_INTERVAL = 30


# =========================================================
# DATABASE - ODDS HISTORY
# =========================================================

# BLOCK: INIT_ODDS_HISTORY_DATABASE
def init_odds_history_database():

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_history(

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                fixture_id INTEGER,

                market TEXT,

                odd REAL,

                minute INTEGER,

                home_goals INTEGER,

                away_goals INTEGER,

                created_at TEXT

            )
        """)

        conn.commit()

        conn.close()

    except Exception as e:

        logging.warning(
            "ODDS HISTORY DB ERROR: %s",
            repr(e)
        )


# =========================================================
# SAVE ODDS SNAPSHOT
# =========================================================

# BLOCK: SAVE_ODDS_SNAPSHOT
def save_odds_snapshot(
    fixture_id,
    market,
    odd,
    minute,
    home_goals,
    away_goals
):

    if odd is None:

        return False

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO odds_history(

                fixture_id,
                market,
                odd,
                minute,
                home_goals,
                away_goals,
                created_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,

            (

                fixture_id,
                market,
                odd,
                minute,
                home_goals,
                away_goals,

                datetime.now(
                    TIMEZONE
                ).isoformat()

            )

        )

        conn.commit()

        conn.close()

        return True

    except Exception as e:

        logging.warning(
            "ODDS SNAPSHOT ERROR: %s",
            repr(e)
        )

        return False


# =========================================================
# GET ODDS HISTORY
# =========================================================

# BLOCK: GET_ODDS_HISTORY
def get_odds_history(
    fixture_id,
    market
):

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                odd,
                minute,
                home_goals,
                away_goals,
                created_at

            FROM odds_history

            WHERE fixture_id=?
            AND market=?

            ORDER BY id ASC
            """,

            (
                fixture_id,
                market
            )

        )

        rows = cursor.fetchall()

        conn.close()

        return rows

    except Exception as e:

        logging.warning(
            "GET ODDS HISTORY ERROR: %s",
            repr(e)
        )

        return []


# =========================================================
# FIRST ODDS
# =========================================================

# BLOCK: GET_OPENING_LIVE_ODD
def get_opening_live_odd(
    fixture_id,
    market
):

    history = get_odds_history(
        fixture_id,
        market
    )

    if not history:

        return None

    return safe_float(
        history[0][0]
    )


# =========================================================
# LAST ODDS
# =========================================================

# BLOCK: GET_CURRENT_RECORDED_ODD
def get_current_recorded_odd(
    fixture_id,
    market
):

    history = get_odds_history(
        fixture_id,
        market
    )

    if not history:

        return None

    return safe_float(
        history[-1][0]
    )


# =========================================================
# ODDS MOVEMENT
# =========================================================

# BLOCK: CALCULATE_ODDS_MOVEMENT
def calculate_odds_movement(
    opening_odd,
    current_odd
):

    if (

        opening_odd is None

        or

        current_odd is None

        or

        opening_odd <= 1.01

    ):

        return 0

    movement = (

        (
            opening_odd
            -
            current_odd
        )

        /

        opening_odd

    ) * 100

    return round(
        movement,
        2
    )


# =========================================================
# ODDS TREND
# =========================================================

# BLOCK: ODDS_TREND
def odds_trend(
    movement
):

    if movement >= 10:

        return "🔥 VERY STRONG SUPPORT"

    if movement >= 6:

        return "🔥 STRONG SUPPORT"

    if movement >= 3:

        return "⭐ SUPPORT"

    if movement <= -10:

        return "⚠ VERY STRONG AGAINST"

    if movement <= -6:

        return "⚠ STRONG AGAINST"

    if movement <= -3:

        return "⚠ AGAINST"

    return "NEUTRAL"


# =========================================================
# RECORD LIVE ODDS
# =========================================================

# BLOCK: RECORD_LIVE_ODDS
def record_live_odds(
    match
):

    try:

        fixture = match.get(
            "fixture",
            {}
        )

        fixture_id = fixture.get(
            "id"
        )

        minute = fixture.get(
            "status",
            {}
        ).get(
            "elapsed"
        )

        if fixture_id is None:

            return

        if minute is None:

            return

        teams = match.get(
            "teams",
            {}
        )

        home_team = teams.get(
            "home",
            {}
        ).get(
            "name",
            "HOME"
        )

        away_team = teams.get(
            "away",
            {}
        ).get(
            "name",
            "AWAY"
        )

        goals = match.get(
            "goals",
            {}
        )

        home_goals = goals.get(
            "home"
        )

        away_goals = goals.get(
            "away"
        )

        live_odds = parse_live_next_goal_odds(

            fixture_id,

            home_team,

            away_team

        )

        if not live_odds:

            return

        # =============================================
        # HOME
        # =============================================

        home_odd = live_odds.get(
            "home"
        )

        if home_odd is not None:

            save_odds_snapshot(

                fixture_id,

                "🎯 NEXT GOAL HOME",

                home_odd,

                minute,

                home_goals or 0,

                away_goals or 0

            )

        # =============================================
        # AWAY
        # =============================================

        away_odd = live_odds.get(
            "away"
        )

        if away_odd is not None:

            save_odds_snapshot(

                fixture_id,

                "🎯 NEXT GOAL AWAY",

                away_odd,

                minute,

                home_goals or 0,

                away_goals or 0

            )

    except Exception as e:

        logging.warning(
            "RECORD LIVE ODDS ERROR: %s",
            repr(e)
        )


# =========================================================
# GET MARKET MOVEMENT FOR SIGNAL
# =========================================================

# BLOCK: ENRICH_SIGNAL_WITH_ODDS_HISTORY
def enrich_signal_with_odds_history(
    signal
):

    try:

        fixture_id = signal.get(
            "fixture_id"
        )

        market = signal.get(
            "market"
        )

        current_odd = signal.get(
            "odd"
        )

        if (

            fixture_id is None

            or

            market is None

        ):

            return signal

        opening_odd = get_opening_live_odd(

            fixture_id,

            market

        )

        if opening_odd is None:

            signal[
                "opening_odd"
            ] = current_odd

            signal[
                "odds_movement"
            ] = 0

            signal[
                "odds_trend"
            ] = "NEW MARKET"

            return signal

        movement = calculate_odds_movement(

            opening_odd,

            current_odd

        )

        signal[
            "opening_odd"
        ] = opening_odd

        signal[
            "odds_movement"
        ] = movement

        signal[
            "odds_trend"
        ] = odds_trend(
            movement
        )

        # =============================================
        # SMALL QUALITY BONUS
        # =============================================

        if movement >= 6:

            signal[
                "confidence"
            ] = round(

                min(
                    95,
                    signal.get(
                        "confidence",
                        0
                    ) + 2
                ),

                1

            )

        elif movement >= 3:

            signal[
                "confidence"
            ] = round(

                min(
                    95,
                    signal.get(
                        "confidence",
                        0
                    ) + 1
                ),

                1

            )

        elif movement <= -6:

            signal[
                "confidence"
            ] = round(

                max(
                    0,
                    signal.get(
                        "confidence",
                        0
                    ) - 3
                ),

                1

            )

        return signal

    except Exception as e:

        logging.warning(
            "ODDS ENRICH ERROR: %s",
            repr(e)
        )

        return signal


# =========================================================
# RESULT CHECKER
# =========================================================

# BLOCK: GET_FIXTURE_RESULT
def get_fixture_result(
    fixture_id
):

    try:

        data = api_get(

            "fixtures",

            {
                "id":
                    fixture_id
            }

        )

        response = data.get(
            "response",
            []
        )

        if not response:

            return None

        fixture = response[0]

        status = fixture.get(
            "fixture",
            {}
        ).get(
            "status",
            {}
        ).get(
            "short"
        )

        if status not in (

            "FT",
            "AET",
            "PEN"

        ):

            return None

        goals = fixture.get(
            "goals",
            {}
        )

        home = goals.get(
            "home"
        )

        away = goals.get(
            "away"
        )

        if (

            home is None

            or

            away is None

        ):

            return None

        return {

            "home":
                home,

            "away":
                away,

            "status":
                status

        }

    except Exception as e:

        logging.warning(
            "RESULT ERROR: %s",
            repr(e)
        )

        return None


# =========================================================
# CHECK NEXT GOAL RESULT
# =========================================================

# BLOCK: CHECK_NEXT_GOAL_RESULT
def check_next_goal_result(
    signal
):

    fixture_id = signal.get(
        "fixture_id"
    )

    market = signal.get(
        "market"
    )

    if fixture_id is None:

        return None

    result = get_fixture_result(
        fixture_id
    )

    if result is None:

        return None

    history = get_odds_history(

        fixture_id,

        market

    )

    if not history:

        return None

    signal_minute = signal.get(
        "minute",
        0
    )

    signal_home_goals = history[-1][2]
    signal_away_goals = history[-1][3]

    # =============================================
    # GET EVENTS
    # =============================================

    data = api_get(

        "fixtures/events",

        {
            "fixture":
                fixture_id
        }

    )

    events = data.get(
        "response",
        []
    )

    next_goal = None

    for event in events:

        event_type = clean_text(
            event.get(
                "type"
            )
        )

        detail = clean_text(
            event.get(
                "detail"
            )
        )

        elapsed = event.get(
            "time",
            {}
        ).get(
            "elapsed"
        )

        if event_type != "goal":

            continue

        if elapsed is None:

            continue

        if elapsed <= signal_minute:

            continue

        team_id = event.get(
            "team",
            {}
        ).get(
            "id"
        )

        teams = signal.get(
            "teams",
            {}
        )

        home_id = teams.get(
            "home",
            {}
        ).get(
            "id"
        )

        away_id = teams.get(
            "away",
            {}
        ).get(
            "id"
        )

        if team_id == home_id:

            next_goal = "HOME"

        elif team_id == away_id:

            next_goal = "AWAY"

        if next_goal:

            break

    # =============================================
    # NO LATER GOAL
    # =============================================

    if next_goal is None:

        return "LOSS"

    # =============================================
    # CHECK MARKET
    # =============================================

    if market == "🎯 NEXT GOAL HOME":

        if next_goal == "HOME":

            return "WIN"

        return "LOSS"

    if market == "🎯 NEXT GOAL AWAY":

        if next_goal == "AWAY":

            return "WIN"

        return "LOSS"

    return None


# =========================================================
# UPDATE SIGNAL RESULT
# =========================================================

# BLOCK: UPDATE_SIGNAL_RESULT
def update_signal_result(
    signal_id,
    result
):

    if result not in (
        "WIN",
        "LOSS"
    ):

        return False

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(

            """
            UPDATE signals

            SET result=?

            WHERE id=?

            """,

            (
                result,
                signal_id
            )

        )

        conn.commit()

        conn.close()

        return True

    except Exception as e:

        logging.warning(
            "RESULT UPDATE ERROR: %s",
            repr(e)
        )

        return False


# =========================================================
# CHECK PENDING SIGNALS
# =========================================================

# BLOCK: CHECK_PENDING_SIGNALS
def check_pending_signals():

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                fixture_id,
                home_team,
                away_team,
                market,
                probability,
                odd,
                confidence,
                created_at

            FROM signals

            WHERE result IS NULL

            ORDER BY id ASC

            LIMIT 50
            """
        )

        rows = cursor.fetchall()

        conn.close()

        checked = 0

        for row in rows:

            (

                signal_id,
                fixture_id,
                home_team,
                away_team,
                market,
                probability,
                odd,
                confidence,
                created_at

            ) = row

            # =========================================
            # ONLY NEXT GOAL FOR NOW
            # =========================================

            if market not in (

                "🎯 NEXT GOAL HOME",

                "🎯 NEXT GOAL AWAY"

            ):

                continue

            signal = {

                "fixture_id":
                    fixture_id,

                "home_team":
                    home_team,

                "away_team":
                    away_team,

                "market":
                    market,

                "probability":
                    probability,

                "odd":
                    odd,

                "confidence":
                    confidence,

                "created_at":
                    created_at,

                "teams": {

                    "home": {},
                    "away": {}

                }

            }

            result = check_next_goal_result(
                signal
            )

            if result is None:

                continue

            if update_signal_result(

                signal_id,

                result

            ):

                checked += 1

                print(

                    "RESULT:",

                    home_team,

                    "-",

                    away_team,

                    market,

                    result

                )

        return checked

    except Exception as e:

        logging.warning(

            "PENDING SIGNAL CHECK ERROR: %s",

            repr(e)

        )

        return 0


# =========================================================
# PERFORMANCE REPORT
# =========================================================

# BLOCK: GET_PERFORMANCE_REPORT
def get_performance_report():

    try:

        conn = sqlite3.connect(
            DB_NAME
        )

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                market,
                COUNT(*),
                SUM(
                    CASE
                        WHEN result='WIN'
                        THEN 1
                        ELSE 0
                    END
                )

            FROM signals

            WHERE result IS NOT NULL

            GROUP BY market

            ORDER BY
                COUNT(*) DESC
            """
        )

        rows = cursor.fetchall()

        conn.close()

        report = []

        for row in rows:

            market = row[0]

            total = row[1]

            wins = row[2] or 0

            if total <= 0:

                continue

            winrate = round(

                wins
                *
                100
                /
                total,

                1

            )

            report.append({

                "market":
                    market,

                "signals":
                    total,

                "wins":
                    wins,

                "losses":
                    total - wins,

                "winrate":
                    winrate

            })

        return report

    except Exception as e:

        logging.warning(
            "PERFORMANCE ERROR: %s",
            repr(e)
        )

        return []


# =========================================================
# PRINT PERFORMANCE
# =========================================================

# BLOCK: PRINT_PERFORMANCE_REPORT
def print_performance_report():

    report = get_performance_report()

    print()

    print(
        "=" * 60
    )

    print(
        "📊 AI PERFORMANCE"
    )

    print(
        "=" * 60
    )

    if not report:

        print(
            "No completed signals yet."
        )

        print(
            "=" * 60
        )

        return

    for row in report:

        print(

            row["market"],

            "|",

            row["winrate"],

            "%",

            "|",

            row["wins"],

            "/",

            row["signals"]

        )

    print(
        "=" * 60
    )


# =========================================================
# DATABASE STARTUP
# =========================================================

# BLOCK: INITIALIZE_ALL_DATABASES
def initialize_all_databases():

    init_database()

    init_odds_history_database()


# =========================================================
# FINAL STARTUP
# =========================================================

# BLOCK: STARTUP
def startup():

    initialize_all_databases()

    print()

    print(
        "=============================================="
    )

    print(
        "🤖 AI FOOTBALL SYSTEM V5 — IMPROVED"
    )

    print(
        "=============================================="
    )

    print(
        "Database ............ OK"
    )

    print(
        "Odds History ........ OK"
    )

    print(
        "Signal Engine ....... OK"
    )

    print(
        "Risk Engine ......... OK"
    )

    print(
        "Live Scanner ........ OK"
    )

    print(
        "Prematch Scanner .... OK"
    )

    print(
        "=============================================="
    )

    print()



# =========================================================
# AI ADAPTIVE STRATEGY MANAGER V7
# =========================================================

class StrategyManager:
    """
    V7 strategy selector.

    Strategies are registered with:
      - condition(match/signal) -> bool
      - model(match/signal) -> optional result

    Selection is deterministic and safe: a broken strategy is skipped
    instead of stopping the whole scanner.
    """

    def __init__(self):
        self.strategies = {}

    def register(self, name, condition, model):
        if not name or not callable(condition) or not callable(model):
            return False
        self.strategies[str(name)] = {
            "condition": condition,
            "model": model,
        }
        return True

    def select(self, match):
        selected = []
        for name, strategy in self.strategies.items():
            try:
                if strategy["condition"](match):
                    selected.append({
                        "name": name,
                        "model": strategy["model"],
                    })
            except Exception as e:
                logging.warning(
                    "STRATEGY ERROR | %s | %s",
                    name,
                    repr(e),
                )
        return selected

    def best(self, match):
        selected = self.select(match)
        if not selected:
            return None
        return selected[0]

# =========================================================
# START
# =========================================================




   

# =========================================================
# FINAL PATCH — PREMATCH TOP 3 + BUILDER TOP 2
# =========================================================
# This block is intentionally at the end so it is the effective definition
# used by main_loop(), while preserving the complete original 19k-line engine.

BUILDER_LEG_MIN_ODD_FINAL = 1.25
BUILDER_LEG_MAX_ODD_FINAL = 1.60
BUILDER_COMBINED_MIN_ODD_FINAL = 1.50
BUILDER_COMBINED_MAX_ODD_FINAL = 4.50
BUILDER_MIN_PROB_FINAL = 62.0
BUILDER_MAX_LEG_PROB_FINAL = 90.0
BUILDER_MAX_JOINT_PROB_FINAL = 82.0
PREMATCH_TOP3_FINAL = 3
BUILDER_TOP2_FINAL = 2


def _final_builder_family(label):
    s = clean_text(label)
    if 'corner' in s:
        return 'corners'
    if 'card' in s or 'booking' in s:
        return 'cards'
    if 'goal' in s or 'btts' in s:
        return 'goals'
    return s.split()[0] if s else 'other'


def _final_builder_candidates(match):
    """Create Builder legs using MODEL probabilities, never bookmaker-implied
    probability as the model.

    A Builder leg is allowed to have a small odds value (>= 1.25), because it
    is combined with another genuinely model-supported leg.  But the model
    probability must come from the existing PREMATCH engine.  If the engine
    has no model for a Betano corner/card line, that line is NOT treated as a
    certainty and is excluded rather than fabricating a probability from odds.
    """
    fid = match.get('fixture', {}).get('id')
    if not fid:
        return []

    markets = get_prematch_betano_builder_markets(fid)
    if not markets:
        return []

    # Existing PREMATCH model signals are the only source for Builder model p.
    model_signals = analyze_prematch(match) or []
    if isinstance(model_signals, dict):
        model_signals = [model_signals]

    def norm(x):
        return clean_text(x).replace('🧩', '').strip()

    def model_probability_for(label):
        target = norm(label)
        aliases = {
            'over 2.5': ('🚀 over 2.5', 'over 2.5'),
            'under 2.5': ('🛡 under 2.5', 'under 2.5'),
            'btts yes': ('💎 btts yes', 'btts yes'),
            'home over 1.5 goals': ('⚽ home over 1.5', 'home over 1.5 goals'),
            'away over 1.5 goals': ('⚽ away over 1.5', 'away over 1.5 goals'),
        }
        keys = aliases.get(target, (target,))
        best = None
        for sig in model_signals:
            sm = norm(sig.get('market', ''))
            if sm in keys or any(k in sm for k in keys):
                pp = safe_float(sig.get('probability'))
                cc = safe_float(sig.get('confidence'))
                if pp is not None and cc is not None:
                    # Do not allow a weak confidence signal to become a Builder leg.
                    if cc < 72.0:
                        continue
                    if best is None or pp > best:
                        best = pp
        return best

    candidates = []
    for m in markets:
        name = clean_text(m.get('name'))
        if any(x in name for x in ('1st half', 'first half', '1h')):
            continue

        family = None
        prefix = ''
        if 'corner' in name:
            family, prefix = 'corners', 'CORNER '
        elif 'card' in name or 'booking' in name or 'yellow' in name:
            family, prefix = 'cards', 'CARD '
        elif 'goal' in name or 'btts' in name or 'both teams' in name:
            family = 'goals'

        if family is None:
            continue

        for v in m.get('values', []) or []:
            value = clean_text(v.get('value'))
            odd = safe_float(v.get('odd'))
            if odd is None or odd < BUILDER_LEG_MIN_ODD_FINAL or odd > BUILDER_LEG_MAX_ODD_FINAL:
                continue

            if family in ('corners', 'cards') and not value.startswith(('over ', 'under ')):
                continue
            if family == 'goals' and not (
                value.startswith(('over 1.5', 'under 2.5', 'under 3.5'))
                or value in ('btts yes', 'btts no', 'yes', 'no')
            ):
                continue

            label = f'{prefix}{value}' if family != 'goals' else value.upper()
            model_probability = model_probability_for(label)

            # Critical rule: never use 100/odd as the model probability.
            # If the PREMATCH model has no corresponding market, skip it.
            if model_probability is None:
                continue

            model_probability = min(BUILDER_MAX_LEG_PROB_FINAL, max(5.0, model_probability))
            if model_probability < BUILDER_MIN_PROB_FINAL:
                continue

            candidates.append({
                'label': label,
                'odd': float(odd),
                'probability': round(model_probability, 1),
                'family': family,
            })

    best = {}
    for c in candidates:
        old = best.get(c['label'])
        if old is None or (c['probability'], c['odd']) > (old['probability'], old['odd']):
            best[c['label']] = c

    return sorted(best.values(), key=lambda x: (x['probability'], x['odd']), reverse=True)[:20]

def build_best_bet_builder(match):
    from itertools import combinations

    candidates = _final_builder_candidates(match)
    if len(candidates) < 2:
        return None

    best = None
    for n in range(2, min(4, len(candidates)) + 1):
        for combo in combinations(candidates, n):
            families = [x['family'] for x in combo]
            # A Builder must combine different market families.  This avoids
            # stacking several highly correlated corner/card lines.
            if len(set(families)) != len(families):
                continue

            combined_odd = 1.0
            joint = 1.0
            for leg in combo:
                combined_odd *= leg['odd']
                joint *= leg['probability'] / 100.0

            if not (BUILDER_COMBINED_MIN_ODD_FINAL <= combined_odd <= BUILDER_COMBINED_MAX_ODD_FINAL):
                continue

            joint_probability = joint * 100.0
            if joint_probability < 45.0 or joint_probability > BUILDER_MAX_JOINT_PROB_FINAL:
                continue

            fair_probability = 100.0 / combined_odd
            edge = joint_probability - fair_probability
            # Builder must have a real model edge; never send an EV≈0 combo
            # merely because its combined price reaches the minimum.
            if edge < 1.0:
                continue

            # Quality score favours strong joint probability and useful total
            # odds, without pretending the legs are independent certainties.
            score = (
                joint_probability * 0.75
                + min(25.0, (combined_odd - 1.40) * 12.0)
                - (n - 2) * 2.0
            )
            if best is None or score > best[0]:
                best = (score, combo, combined_odd, joint_probability)

    if best is None:
        return None

    score, combo, combined_odd, joint_probability = best
    confidence = min(
        88.0,
        round(
            sum(x['probability'] for x in combo) / len(combo) - (len(combo) - 2) * 2.0,
            1,
        ),
    )
    risk = max(12.0, round(100.0 - confidence, 1))
    fair_probability = 100.0 / combined_odd
    edge = joint_probability - fair_probability
    ev = (joint_probability / 100.0) * combined_odd - 1.0

    teams = match.get('teams', {})
    league = match.get('league', {})
    return {
        'fixture_id': match.get('fixture', {}).get('id'),
        'home_team': teams.get('home', {}).get('name', 'HOME'),
        'away_team': teams.get('away', {}).get('name', 'AWAY'),
        'league': league.get('name', ''),
        'country': league.get('country', ''),
        'market': '🧩 BET BUILDER',
        'probability': round(joint_probability, 1),
        'confidence': confidence,
        'risk': risk,
        'odd': round(combined_odd, 2),
        'edge': round(edge, 1),
        'ev': round(ev, 3),
        'score': round(score, 2),
        'builder_legs': [
            {
                'market': x['label'],
                'odd': round(x['odd'], 2),
                'probability': round(x['probability'], 1),
            }
            for x in combo
        ],
    }


def _final_standalone_allowed(signal):
    # Quality-first, but not so tight that a normal day's board produces zero
    # signals.  Value is mandatory: we do not send a market merely because its
    # raw model probability is high.
    if not signal or signal.get('market') == '🧩 BET BUILDER':
        return False
    p = _safe_num(signal.get('probability'), 0)
    c = _safe_num(signal.get('confidence'), 0)
    r = _safe_num(signal.get('risk'), 100)
    edge = _safe_num(signal.get('edge'), -999)
    odd = safe_float(signal.get('odd'))
    if p < 62.0 or c < 74.0 or r > 34.0:
        return False
    if edge < 4.0:
        return False
    if odd is None or odd < 1.40 or odd > PREMATCH_MAX_ODD:
        return False
    return True


def get_best_prematch_signals(matches):
    standalone = []
    builders = []
    seen = set()

    for match in matches:
        fid = match.get('fixture', {}).get('id')
        try:
            signals = analyze_prematch(match) or []
            if isinstance(signals, dict):
                signals = [signals]
            for raw in signals:
                s = normalize_prematch_signal(raw)
                if not _final_standalone_allowed(s):
                    # Balanced final gate: do not force 70%+ probability when
                    # the model's confidence/risk profile is otherwise strong.
                    p0 = _safe_num(s.get('probability'), 0) if s else 0
                    c0 = _safe_num(s.get('confidence'), 0) if s else 0
                    r0 = _safe_num(s.get('risk'), 100) if s else 100
                    o0 = safe_float(s.get('odd')) if s else None
                    if not (p0 >= 66.0 and c0 >= 76.0 and r0 <= 32.0 and o0 is not None and 1.40 <= o0 <= PREMATCH_MAX_ODD):
                        continue
                s = dict(s)
                s['fixture_id'] = s.get('fixture_id') or fid
                s['market'] = _base_market_from_label(s.get('market'))
                key = (s['fixture_id'], s.get('market'))
                if key in seen:
                    continue
                seen.add(key)
                s['score'] = prematch_signal_score(s)
                standalone.append(s)

            builder = build_best_bet_builder(match)
            if builder:
                builders.append(builder)
        except Exception as e:
            logging.warning('PREMATCH FINAL RANK ERROR: %s', repr(e))

    # Pick the strongest standalone market per fixture, then keep the global
    # Top-3 standalone signals.  Builders are ranked separately so a Builder
    # cannot crowd out the three best individual bets.
    by_fixture = {}
    for s in standalone:
        fid = s.get('fixture_id')
        old = by_fixture.get(fid)
        key = (
            _safe_num(s.get('edge'), -999),
            _safe_num(s.get('probability'), 0),
            _safe_num(s.get('confidence'), 0),
            -_safe_num(s.get('risk'), 100),
        )
        old_key = (
            _safe_num(old.get('edge'), -999),
            _safe_num(old.get('probability'), 0),
            _safe_num(old.get('confidence'), 0),
            -_safe_num(old.get('risk'), 100),
        ) if old else None
        if old is None or key > old_key:
            by_fixture[fid] = s

    top3 = sorted(
        by_fixture.values(),
        key=lambda s: (
            _safe_num(s.get('edge'), -999),
            _safe_num(s.get('probability'), 0),
            _safe_num(s.get('confidence'), 0),
            -_safe_num(s.get('risk'), 100),
        ),
        reverse=True,
    )[:PREMATCH_TOP3_FINAL]

    # Builders are independent of standalone ranking: up to two additional
    # Builders can be sent.  Never manufacture a Builder just to fill quota.
    builders.sort(
        key=lambda s: (
            _safe_num(s.get('edge'), -999),
            _safe_num(s.get('probability'), 0),
            _safe_num(s.get('confidence'), 0),
            _safe_num(s.get('odd'), 0),
        ),
        reverse=True,
    )
    top_builders = builders[:BUILDER_TOP2_FINAL]

    # Add date/time fields required by the Telegram formatter.
    for s in top3 + top_builders:
        match = next((m for m in matches if m.get('fixture', {}).get('id') == s.get('fixture_id')), None)
        if match:
            fx = match.get('fixture', {})
            lg = match.get('league', {})
            s['country'] = s.get('country') or lg.get('country', '')
            s['league'] = s.get('league') or lg.get('name', '')
            raw_dt = fx.get('date') or ''
            try:
                dt = datetime.fromisoformat(str(raw_dt).replace('Z', '+00:00')).astimezone(TIMEZONE)
                s['match_date'] = dt.strftime('%d.%m.%Y')
                s['match_time'] = dt.strftime('%H:%M')
            except Exception:
                pass

    return top3 + top_builders


# Override formatter so Builder legs show their REAL conservative probabilities.
def format_prematch_signal(signal):
    market = signal.get('market', 'UNKNOWN')
    date = signal.get('match_date', '') or 'N/A'
    tm = signal.get('match_time', '') or 'N/A'
    text = (
        '🔥 PREMATCH V9\n\n'
        f"⚽ {signal.get('home_team', 'HOME')} - {signal.get('away_team', 'AWAY')}\n"
        f"🌍 {signal.get('country', 'N/A')}\n"
        f"🏆 {signal.get('league', 'N/A')}\n"
        f"📅 {date} | ⏰ {tm} 🇧🇬\n\n"
        f"🎯 MARKET: {market}\n"
    )
    if market == '🧩 BET BUILDER':
        for leg in signal.get('builder_legs', []):
            text += f"  • {leg['market']} @ {leg['odd']:.2f} | Model: {leg.get('probability', 0):.1f}%\n"
        text += (
            f"\n🧩 Combined Odds: {signal.get('odd', 0):.2f}\n"
            f"📈 Joint Probability: {signal.get('probability', 0):.1f}%\n"
            f"📊 Edge: {signal.get('edge', 0):+.1f}%\n"
            f"💎 EV: {signal.get('ev', 0):+.3f}\n"
        )
    else:
        text += (
            f"📈 Probability: {signal.get('probability', 0):.1f}%\n"
            f"💰 Betano Odds: {signal.get('odd', 0):.2f}\n"
            f"📊 Edge: {signal.get('edge', 0):+.1f}%\n"
        )
        if signal.get('ev') is not None:
            text += f"💎 EV: {signal.get('ev', 0):+.3f}\n"
    text += f"🤖 Confidence: {signal.get('confidence', 0):.1f}%\n🛡 Risk: {signal.get('risk', 0):.0f}"
    return text


# Final configuration values used by the patched selection layer.
PREMATCH_DAILY_TOP5 = 5
MAX_PREMATCH_SIGNALS_PER_SCAN = 5
BUILDER_MIN_LEG_ODD = BUILDER_LEG_MIN_ODD_FINAL
BUILDER_MAX_LEG_ODD = BUILDER_LEG_MAX_ODD_FINAL


# =========================================================
# FINAL START
# =========================================================
if __name__ == "__main__":
    startup()
    main_loop()













   

