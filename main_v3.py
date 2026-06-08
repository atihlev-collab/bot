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
