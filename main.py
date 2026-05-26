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

    "friendly"
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

        return 0

    if key not in odds_cache:

        odds_cache[key] = odd

        return 0

    old_odd = odds_cache[key]

    drop = old_odd - odd

    odds_cache[key] = odd

    return round(
        drop,
        2
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

def calculate_match_score(
    country,
    league,
    home,
    away
):

    score = 0

    market = "⚽ OVER 2.5 GOALS"
    odd = "1.80"

    # GOAL LEAGUES
    if country in [

        "Netherlands",
        "Germany",
        "Norway"

    ]:

        score += 12

        market = "⚽ OVER 2.5 GOALS"

        odd = "1.80"

    # UNDER LEAGUES
    elif country in [

        "Italy",
        "Romania",
        "Bulgaria"

    ]:

        score += 10

        market = "📉 UNDER 2.5 GOALS"

        odd = "1.75"

    # BTTS LEAGUES
    elif country in [

        "Sweden",
        "Denmark"

    ]:

        score += 11

        market = "💎 BTTS"

        odd = "1.85"

    # DERBY BONUS
    if "derby" in league.lower():

        score += 5

    # CUP PENALTY
    if "cup" in league.lower():

        score -= 5

    # REMOVE BAD LEAGUES
    if any(

        x in league.lower()

        for x in [

            "u21",
            "women",
            "reserve",
            "friendly"

        ]

    ):

        score -= 100

    # BIG TEAMS SMALL BOOST
    big_teams = [

        "Ajax",
        "PSV",
        "Liverpool",
        "Arsenal",
        "Barcelona",
        "Real Madrid",
        "Manchester City"

    ]

    if home in big_teams:
        score += 3

    if away in big_teams:
        score += 3

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

    market = None
    bonus_market = ""

    # NEXT GOAL
    if dominance >= 15:

        if home_pressure > away_pressure:

            market = (
                f"🎯 NEXT GOAL HOME "
                f"({match['teams']['home']['name']})"
            )

        else:

            market = (
                f"🎯 NEXT GOAL AWAY "
                f"({match['teams']['away']['name']})"
            )

    # BTTS
    elif (
        best_xg >= 2.4
        and home_shots >= 4
        and away_shots >= 4
        and total_goals <= 3
        and (
            home_goals == 0
            or away_goals == 0
        )
    ):

        market = "💎 BTTS / GOAL-GOAL"

    # OVER GOALS
    elif (
        total_goals <= 1
        and best_pressure >= 68
        and best_xg >= 2
        and minute >= 40
    ):

        market = (
            f"⚽ OVER {total_goals+1}.5 GOALS"
        )

    # LATE GOAL
    elif (
        minute >= 75
        and best_pressure >= 72
        and abs(home_goals-away_goals) < 4
    ):

        market = "🔥 GOAL 75-90"


    # CARDS MARKET
    elif (

        minute >= 30
        and minute <= 75

    ):

        home_fouls = extract(
            home,
            "Fouls"
        )

        away_fouls = extract(
            away,
            "Fouls"
        )

        home_yellow = extract(
            home,
            "Yellow Cards"
        )

        away_yellow = extract(
            away,
            "Yellow Cards"
        )

        total_fouls = (
            home_fouls
            +
            away_fouls
        )

        total_cards = (
            home_yellow
            +
            away_yellow
        )

        if (

            total_fouls >= 22
            and total_cards >= 3
            and abs(
                home_goals-away_goals
            ) <= 1

        ):

            market = (
                f"🟨 OVER {total_cards+1}.5 CARDS"
            )

    # SMART CORNERS
    elif (
        minute >= 30
        and minute <= 50
        and total_corners >= 4
        and best_pressure >= 70
        and abs(home_goals-away_goals) <= 1
    ):

        market = (
            f"📐 OVER {total_corners+3}.5 CORNERS"
        )

    if market is None:

        return
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

    # LIVE само 78%+
    if confidence < 78:

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
{home_name} vs {away_name}

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

            score, market, odd = (
                calculate_match_score(
                    country,
                    league,
                    home,
                    away
                )
            )

            confidence = 65 + score

            if confidence < 75:
                continue

            odd = float(odd)

            if odd < 1.50:
                continue

            if odd > 2.10:
                continue

            picks.append(
                (
                    home,
                    away,
                    market,
                    odd
                )
            )

            total_odds *= odd

            if total_odds >= 5:
                break

        except:
            pass

    if len(picks) >= 3:

        msg = "🔥 DAILY AI BET SLIP\n\n"

        for p in picks:

            msg += (
                f"⚽ {p[0]} vs {p[1]}\n"
                f"🎯 {p[2]}\n"
                f"💰 {p[3]}\n\n"
            )

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

                    date = datetime.fromisoformat(
                        m["fixture"]["date"].replace(
                            "Z","+00:00"
                        )
                    ).astimezone(TZ)

                    # само бъдещи мачове до 8 часа
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

                    drop = odds_drop_signal(
                        home,
                        away,
                        odd
                    )

                    if drop >= 0.15:

                        confidence += 8

                        market = (
                            "🔥 BET365 VALUE DROP"
                        )

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

                    if confidence < 80:
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

        await asyncio.sleep(1200)

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

