# =========================================================
# PRACTICAL LIVE AI SYSTEM
# SMART RESET + PREMATCH AI VERSION (FIXED & COMPLETED)
# =========================================================

import time
import sqlite3
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot

# Дърпане на конфигурацията от config.py
from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")

bot = Bot(token=BOT_TOKEN)
logging.basicConfig(level=logging.WARNING)

# ФИЛТРИ
BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly"]
BAD_COUNTRIES = ["Bolivia", "Venezuela", "India", "Indonesia"]

# КЕШ ПАМЕТ
sent = {}
last_scores = {}
prematch_sent = {}

# =========================================================
# DATABASE
# =========================================================

def init_database():
    conn = sqlite3.connect("practical_live_ai.db")
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

def save_signal(fixture_id, match_name, market, pressure, confidence, edge):
    conn = sqlite3.connect("practical_live_ai.db")
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO signals (fixture_id, match_name, market, pressure, confidence, edge_value, created_at) 
    VALUES (?,?,?,?,?,?,?)
    """, (fixture_id, match_name, market, pressure, confidence, edge, str(datetime.now())))
    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    try:
        # Асинхронно изпращане, съобразено с новите версии на Telegram библиотеки
        asyncio.run_coroutine_threadsafe(bot.send_message(chat_id=CHAT_ID, text=message), asyncio.get_event_loop())
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

def can_send_prematch(key, cooldown=21600):
    now = time.time()
    if key in prematch_sent:
        if now - prematch_sent[key] < cooldown:
            return False
    return True

def save_prematch(key):
    prematch_sent[key] = time.time()

def blocked_league(league_name):
    text = league_name.lower()
    return any(word in text for word in BLOCKED_WORDS)

# =========================================================
# API REQUESTS
# =========================================================

def get_live_matches():
    url = f"{BASE_URL}/fixtures"
    try:
        response = requests.get(url, headers=HEADERS, params={"live": "all"}, timeout=20)
        return response.json().get("response", [])
    except:
        return []

def get_upcoming_matches():
    matches = []
    now = datetime.now(TZ)
    for i in range(2):
        date = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            r = requests.get(f"{BASE_URL}/fixtures?date={date}", headers=HEADERS, timeout=20).json()
            matches.extend(r.get("response", []))
        except:
            pass
    return matches

def get_statistics(fixture_id):
    url = f"{BASE_URL}/fixtures/statistics"
    try:
        response = requests.get(url, headers=HEADERS, params={"fixture": fixture_id}, timeout=20)
        return response.json().get("response", [])
    except:
        return []

def extract(team, stat_name):
    for stat in team.get("statistics", []):
        if stat["type"] == stat_name:
            value = stat["value"]
            if value is None: return 0
            if isinstance(value, str):
                value = value.replace("%", "")
                try: value = int(value)
                except: return 0
            return value
    return 0

# =========================================================
# AI ENGINES
# =========================================================

def estimate_xg(shots_on, total_shots, dangerous_attacks):
    xg = (shots_on * 0.28) + (total_shots * 0.05) + (dangerous_attacks * 0.020)
    return round(xg, 2)

def calculate_pressure(team):
    pressure = 0
    possession = extract(team, "Ball Possession")
    shots_on = extract(team, "Shots on Goal")
    total_shots = extract(team, "Total Shots")
    corners = extract(team, "Corner Kicks")
    attacks = extract(team, "Dangerous Attacks")

    if possession >= 55: pressure += 8
    if possession >= 62: pressure += 8
    if shots_on >= 3: pressure += 15
    if shots_on >= 5: pressure += 12
    if total_shots >= 7: pressure += 10
    if total_shots >= 11: pressure += 10
    if corners >= 3: pressure += 6
    if corners >= 6: pressure += 6
    if attacks >= 16: pressure += 14
    if attacks >= 26: pressure += 14

    xg = estimate_xg(shots_on, total_shots, attacks)
    if xg >= 1.1: pressure += 10
    if xg >= 1.8: pressure += 10

    return pressure, xg

def value_edge(confidence, odds):
    probability = 100 / odds
    return round(confidence - probability, 2)

def calculate_match_score(country, league, home, away):
    score = 0
    market = "OVER 2.5 GOALS"
    odd = "1.80"

    OVER_COUNTRIES = ["Netherlands", "Norway", "Sweden", "Germany", "Denmark", "Brazil", "Argentina", "USA"]
    UNDER_COUNTRIES = ["Italy", "Romania", "Bulgaria", "Croatia"]

    if any(x.lower() in country.lower() for x in OVER_COUNTRIES):
        score += 10
        market = "OVER 2.5 GOALS"
        odd = "1.75"

    if any(x.lower() in country.lower() for x in UNDER_COUNTRIES):
        score += 8
        market = "UNDER 2.5 GOALS"
        odd = "1.70"

    return score, market, odd

# =========================================================
# LIVE ANALYSIS CORE
# =========================================================

def analyze_match(match):
    fixture_id = match["fixture"]["id"]
    league = match["league"]["name"]
    if blocked_league(league): return

    country = match["league"]["country"]
    if country in BAD_COUNTRIES: return

    minute = match["fixture"]["status"]["elapsed"]
    if minute is None or minute < 30 or minute > 75: return

    home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
    away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
    total_goals = home_goals + away_goals

    if total_goals >= 6: return
    score = f"{home_goals}-{away_goals}"

    # RESET AFTER GOAL LOGIC
    if fixture_id not in last_scores:
        last_scores[fixture_id] = score
    else:
        if last_scores[fixture_id] != score:
            if fixture_id in sent:
                del sent[fixture_id]
            last_scores[fixture_id] = score

    if not can_send(fixture_id): return

    stats = get_statistics(fixture_id)
    if len(stats) < 2: return

    home = stats[0]
    away = stats[1]

    home_pressure, home_xg = calculate_pressure(home)
    away_pressure, away_xg = calculate_pressure(away)

    best_pressure = max(home_pressure, away_pressure)
    best_xg = max(home_xg, away_xg)
    dominance = abs(home_pressure - away_pressure)

    # СТРОГИ ФИЛТРИ
    minimum_pressure = 52 if minute < 60 else 56
    if best_pressure < minimum_pressure or dominance < 8 or best_xg < 1.0: return

    home_shots = extract(home, "Shots on Goal")
    away_shots = extract(away, "Shots on Goal")
    if max(home_shots, away_shots) < 4: return
    if minute >= 70 and total_goals == 0: return

    total_shots_on = home_shots + away_shots
    home_attacks = extract(home, "Dangerous Attacks")
    away_attacks = extract(away, "Dangerous Attacks")

    # ОПРЕДЕЛЯНЕ НА ПАЗАР
    if total_goals <= 2 and total_shots_on >= 6 and home_attacks >= 12 and away_attacks >= 12 and best_xg >= 1.3 and minute >= 35 and dominance <= 10:
        market = "OVER 1.5 LIVE"
    else:
        if home_pressure > away_pressure:
            market = f"NEXT GOAL HOME ({match['teams']['home']['name']})"
        else:
            market = f"NEXT GOAL AWAY ({match['teams']['away']['name']})"

    # ИЗЧИСЛЯВАНЕ НА CONFIDENCE & EDGE
    confidence_bonus = 4 if best_pressure >= 78 else 0
    confidence = min(best_pressure, 90)
    if minute >= 60: confidence += 2
    if minute >= 70: confidence += 2
    confidence = min(confidence + confidence_bonus, 95)

    if confidence < 70: return

    estimated_odds = 1.80
    edge = value_edge(confidence, estimated_odds)
    if edge < 6: return

    home_team = match["teams"]["home"]["name"]
    away_team = match["teams"]["away"]["name"]
    match_name = f"{home_team} vs {away_team}"

    message = f"""
🔥 PRACTICAL LIVE AI SIGNAL

🌍 Country: {country}
⚽ Match: {match_name}
🏆 League: {league}
⏱ Minute: {minute}
📊 Score: {score}

🔥 Pressure: {best_pressure}/100
⚔ Dominance: {dominance}
📈 Estimated xG: {best_xg}
💎 Value Edge: +{edge}%
📈 Market: {market}
✅ Confidence: {confidence}%
"""
    print(message)
    send_telegram(message)
    save_signal(fixture_id, match_name, market, best_pressure, confidence, edge)
    save_sent(fixture_id)

# =========================================================
# PREMATCH ANALYSIS LOOP
# =========================================================

async def prematch_loop():
    while True:
        try:
            matches = get_upcoming_matches()
            for m in matches:
                try:
                    league = m["league"]["name"]
                    if blocked_league(league): continue

                    country = m["league"]["country"]
                    if country in BAD_COUNTRIES: continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    date = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                    diff = (date - datetime.now(TZ)).total_seconds()

                    # Само бъдещи мачове до 8 часа
                    if diff < 0 or diff > 28800: continue

                    key = f"{m['fixture']['id']}_pre"
                    if not can_send_prematch(key): continue

                    score, market, odd = calculate_match_score(country, league, home, away)
                    confidence = 65 + score

                    if confidence >= 75:
                        msg = f"""
🔮 PRACTICAL PREMATCH AI SIGNAL

⚽ Match: {home} vs {away}
🏆 League: {league} ({country})
⏱ Start Time: {date.strftime('%H:%M')}
📈 Market: {market} (Est. Odds: {odd})
✅ Confidence: {confidence}%
"""
                        print(msg)
                        send_telegram(msg)
                        save_prematch(key)
                except Exception as e:
                    pass
        except Exception as e:
            pass
        await asyncio.sleep(1800) # Проверява на всеки 30 минути

# =========================================================
# LIVE MAIN LOOP
# =========================================================

async def live_loop():
    while True:
        try:
            live_matches = get_live_matches()
            for match in live_matches:
                analyze_match(match)
        except Exception as e:
            print("Live Loop Error:", e)
        await asyncio.sleep(60) # Обновява на живо на всяка минута

async def main():
    init_database()
    print("✅ AI Системата работи по твоята оригинална стратегия!")
    await asyncio.gather(live_loop(), prematch_loop())

if __name__ == "__main__":
    asyncio.run(main())

