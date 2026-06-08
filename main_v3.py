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

import numpy as np

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

    "reserve",
    "reserves"
]

BAD_COUNTRIES = [

    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia",

    "Russia",
    "Belarus"
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

            matches.extend(
                r.get(
                    "response",
                    []
                )
            )

        except:

            pass

    return matches
    
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

            if (gh + ga) >= 3:
                over25 += 1

            if gh > 0 and ga > 0:
                btts += 1

        total = len(games)

        return {

            "avg_scored":
                round(scored / total, 2),

            "avg_conceded":
                round(conceded / total, 2),

            "wins":
                wins,

            "over25":
                over25,

            "btts":
                btts

        }

    except:

        return None

# =========================================================
# POISSON
# =========================================================

def poisson_over25(home_attack, away_attack):

    expected_goals = (
        home_attack +
        away_attack
    )

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

    score += (
        home_form["wins"]
        +
        away_form["wins"]
    )

    score += (
        home_form["over25"]
        +
        away_form["over25"]
    )

    score += (
        home_form["btts"]
        +
        away_form["btts"]
    )

    return score
