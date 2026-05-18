# =========================================================
# PRACTICAL LIVE AI SYSTEM
# IMPROVED + SMART RESET + PREMATCH AI
# =========================================================

import os
import requests
import time
import sqlite3
import threading
import asyncio
import logging

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
# FILTERS
# =========================================================

BLOCKED_WORDS = [
    "women","female",
    "youth","u17","u18","u19",
    "u20","u21","u23",
    "reserve","reserves",
    "friendly"
]

BAD_COUNTRIES = [
    "Bolivia",
    "Venezuela",
    "India",
    "Indonesia"
]

sent={}
last_scores={}
prematch_sent={}

# =========================================================
# DATABASE
# =========================================================

def init_database():

    conn=sqlite3.connect(
        "practical_live_ai.db"
    )

    c=conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS signals(

        id INTEGER PRIMARY KEY,

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

def send_telegram(msg):

    try:

        bot.send_message(
            chat_id=CHAT_ID,
            text=msg
        )

    except Exception as e:

        print(e)

# =========================================================
# COOLDOWN
# =========================================================

def can_send(
    fixture_id,
    cooldown=1200
):

    now=time.time()

    if fixture_id in sent:

        if now-sent[fixture_id]<cooldown:
            return False

    return True


def save_sent(fixture_id):

    sent[fixture_id]=time.time()

# =========================================================
# API
# =========================================================

def safe_get(url,params=None):

    try:

        r=requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=20
        )

        r.raise_for_status()

        return r.json()

    except:

        return {}

# =========================================================
# LIVE
# =========================================================

def get_live_matches():

    data=safe_get(
        f"{BASE_URL}/fixtures",
        {"live":"all"}
    )

    return data.get(
        "response",
        []
    )

# =========================================================

def get_statistics(fixture_id):

    data=safe_get(
        f"{BASE_URL}/fixtures/statistics",
        {"fixture":fixture_id}
    )

    return data.get(
        "response",
        []
    )

# =========================================================

def extract(team,name):

    for s in team["statistics"]:

        if s["type"]==name:

            value=s["value"]

            if value is None:
                return 0

            if isinstance(value,str):

                value=value.replace("%","")

                try:
                    return int(value)

                except:
                    return 0

            return value

    return 0

# =========================================================
# xG
# =========================================================

def estimate_xg(
    shots_on,
    total_shots,
    dangerous_attacks,
    corners
):

    xg=0

    xg += shots_on*0.33
    xg += total_shots*0.08
    xg += corners*0.03

    if dangerous_attacks>=20:
        xg+=0.15

    if dangerous_attacks>=35:
        xg+=0.20

    return round(xg,2)

# =========================================================
# PRESSURE
# =========================================================

def calculate_pressure(team):

    pressure=0

    possession=extract(
        team,
        "Ball Possession"
    )

    shots_on=extract(
        team,
        "Shots on Goal"
    )

    total_shots=extract(
        team,
        "Total Shots"
    )

    corners=extract(
        team,
        "Corner Kicks"
    )

    attacks=extract(
        team,
        "Dangerous Attacks"
    )

    if possession>=55:
        pressure+=6

    if possession>=62:
        pressure+=6

    if shots_on>=2:
        pressure+=12

    if shots_on>=4:
        pressure+=10

    if shots_on>=6:
        pressure+=8

    if total_shots>=6:
        pressure+=8

    if total_shots>=10:
        pressure+=8

    if corners>=3:
        pressure+=5

    if corners>=6:
        pressure+=5

    if attacks>=15:
        pressure+=10

    if attacks>=25:
        pressure+=10

    xg=estimate_xg(
        shots_on,
        total_shots,
        attacks,
        corners
    )

    if xg>=1.1:
        pressure+=8

    if xg>=1.8:
        pressure+=8

    return pressure,xg

# =========================================================
# EDGE
# =========================================================

def value_edge(confidence,odds):

    probability=100/odds

    return round(
        confidence-probability,
        2
    )

# =========================================================
# ANALYZE
# =========================================================

def analyze_match(match):

    fixture_id=match["fixture"]["id"]

    minute=match["fixture"]["status"]["elapsed"]

    if minute is None:
        return

    if minute<30 or minute>75:
        return

    if not can_send(fixture_id):
        return

    stats=get_statistics(
        fixture_id
    )

    if len(stats)<2:
        return

    home=stats[0]
    away=stats[1]

    hp,hxg=calculate_pressure(home)
    ap,axg=calculate_pressure(away)

    best=max(hp,ap)

    dominance=abs(
        hp-ap
    )

    minimum_pressure=50

    if minute>=60:
        minimum_pressure=54

    if minute>=70:
        minimum_pressure=57

    if best<minimum_pressure:
        return

    if dominance<7:
        return

    home_shots=extract(
        home,
        "Shots on Goal"
    )

    away_shots=extract(
        away,
        "Shots on Goal"
    )

    min_shots=4

    if minute>=60:
        min_shots=5

    if minute>=70:
        min_shots=6

    if max(
        home_shots,
        away_shots
    )<min_shots:

        return

    confidence=min(
        best,
        92
    )

    edge=value_edge(
        confidence,
        1.80
    )

    if edge<6:
        return

    market=(
        "NEXT GOAL HOME"
        if hp>ap
        else
        "NEXT GOAL AWAY"
    )

    msg=f"""

🔥 LIVE SIGNAL

⏱ {minute}'

🔥 Pressure: {best}

⚔ Dominance:{dominance}

📈 Edge:+{edge}%

🎯 {market}

✅ {confidence}%

"""

    send_telegram(msg)

    save_sent(
        fixture_id
    )

# =========================================================
# LOOP
# =========================================================

async def live_loop():

    while True:

        try:

            matches=get_live_matches()

            for m in matches:

                try:

                    analyze_match(m)

                except:
                    pass

        except:
            pass

        await asyncio.sleep(45)

# =========================================================

def start():

    loop=asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        live_loop()
    )

# =========================================================

def main():

    init_database()

    t=threading.Thread(
        target=start,
        daemon=True
    )

    t.start()

    while True:
        time.sleep(60)

# =========================================================

if __name__=="__main__":
    main()
