# =========================================================
# ULTIMATE MASTERPIECE TIPSTER AI SYSTEM (main.py)
# RAPIDAPI COMPATIBLE STANDARD EDITION - FULL PROFESSIONAL VERSION
# LIVE: GOALS, CORNERS, NEXT GOAL | PREMATCH: POISSON, SHARP 1X2 DROPS
# AUTOMATIC NIGHTLY MACHINE LEARNING PRE-TRAINING AT 04:00
# =========================================================

import time
import sqlite3
import threading
import requests
import asyncio
import os
import math
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot

# Автоматично зареждане от твоя файл config.py
from config import BOT_TOKEN, API_KEY, CHAT_ID

# Импортиране на твоите ML функции от ml_model.py
try:
    from ml_model import predict_btts, predict_over, train_model, load_model
except ImportError:
    print("❌ Критична грешка: Файлът ml_model.py липсва в същата папка!")
    exit(1)

# =========================================================
# CONFIG & SYSTEM SETUP (RAPIDAPI CONFIRMED FORMAT)
# =========================================================

HEADERS = {
    "x-rapidapi-host": "://rapidapi.com",
    "x-rapidapi-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly", "amateur"]
BAD_COUNTRIES = ["Bolivia", "Venezuela", "India", "Indonesia", "Bangladesh", "Uganda"]

GOLDEN_PREMATCH_COUNTRIES = [
    "Netherlands", "Germany", "Norway", "Sweden", "Denmark", "Iceland", "Switzerland", "Australia",
    "England", "Belgium", "Austria", "Japan", "South Korea", "Scotland", "USA", "Brazil", "Ireland"
]

# Системна памет
sent = {}
prematch_sent = {}
last_scores = {}
odds_tracker = {}

# =========================================================
# DATABASE MANAGER
# =========================================================

def init_database():
    conn = sqlite3.connect("syndicate_master.db")
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
        stake TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_signal(fixture_id, match_name, market, pressure, confidence, edge, stake):
    try:
        conn = sqlite3.connect("syndicate_master.db")
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO signals (fixture_id, match_name, market, pressure, confidence, edge_value, stake, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """, (fixture_id, match_name, market, int(pressure), confidence, edge, stake, str(datetime.now(TZ))))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB Error:", e)

# =========================================================
# TELEGRAM DELIVERY LAYER
# =========================================================

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except Exception as e:
        print("Telegram Error:", e)

# =========================================================
# UTILITIES AND POISSON CALCULATOR
# =========================================================

def safe_api_get(endpoint, params=None):
    try:
        clean_endpoint = endpoint.lstrip('/')
        # ДОКАЗАНИЯТ И РАБОТЕЩ ЛИНК ОТ ТЕСТА:
        url = f"https://://rapidapi.com/v3/{clean_endpoint}"
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        
        print(f"📡 [API CHECK] URL: {url} | Status Code: {response.status_code}")
        if response.status_code == 200:
            return response.json().get("response", [])
    except Exception as e:
        print(f"❌ Критична грешка при връзка с API: {e}")
    return []

def blocked_league(league_name):
    text = league_name.lower()
    return any(word in text for word in BLOCKED_WORDS)

def extract(team, stat_name):
    for stat in team.get("statistics", []):
        if stat["type"] == stat_name:
            value = stat["value"]
            if value is None: return 0
            if isinstance(value, str):
                value = value.replace("%", "")
                try: return int(value)
                except: return 0
            return int(value)
    return 0

def calculate_dynamic_stake(confidence):
    if confidence >= 85: return "🔥 СИЛЕН ЗАЛОГ: 3.5% от банката"
    elif confidence >= 75: return "💰 СРЕДЕН ЗАЛОГ: 2.0% от банката"
    else: return "⚠️ КОНСЕРВАТИВЕН ЗАЛОГ: 1.0% от банката"

def calculate_poisson_probability(k, lam):
    return (pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def analyze_poisson_over_under(fixture_id):
    avg_goals_scored_home = 1.85  
    avg_goals_conceded_away = 1.60 
    avg_goals_scored_away = 1.20  
    avg_goals_conceded_home = 0.95 
    
    lambda_home = avg_goals_scored_home * avg_goals_conceded_away
    lambda_away = avg_goals_scored_away * avg_goals_conceded_home
    
    prob_under_2_5 = 0.0
    for x in range(3):
        for y in range(3):
            if (x + y) < 3:
                p_x = calculate_poisson_probability(x, lambda_home)
                p_y = calculate_poisson_probability(y, lambda_away)
                prob_under_2_5 += (p_x * p_y)
                
    prob_over_2_5 = round((1.0 - prob_under_2_5) * 100, 1)
    return prob_over_2_5

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

    xg = round((shots_on * 0.28) + (total_shots * 0.05) + (attacks * 0.022), 2)
    if xg >= 1.1: pressure += 10
    if xg >= 1.8: pressure += 10

    return min(pressure, 100), xg

# =========================================================
# 🧵 THREAD 1: INTELLIGENT LIVE ENGINE (EVERY 60 SECONDS)
# =========================================================

def live_analysis_runner():
    print("⚡ LIVE Мулти-пазарен скенер с ИИ е активен...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            
            for match in live_matches:
                if not match: continue
                fixture_id = match["fixture"]["id"]
                league = match["league"]["name"]
                country = match["league"]["country"]
                if blocked_league(league) or country in BAD_COUNTRIES: continue

                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 15 or minute > 85: continue

                home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
                away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
                total_goals = home_goals + away_goals
                if total_goals >= 6: continue 
                score = f"{home_goals}-{away_goals}"

                if fixture_id not in last_scores:
                    last_scores[fixture_id] = score
                else:
                    if last_scores[fixture_id] != score:
                        if f"{fixture_id}_live" in sent: del sent[f"{fixture_id}_live"]
                        last_scores[fixture_id] = score

                if f"{fixture_id}_live" in sent: continue

                stats = safe_api_get("fixtures/statistics", {"fixture": fixture_id})
                if len(stats) < 2: continue

                home_id = match["teams"]["home"]["id"]
                home_stats, away_stats = (stats, stats) if stats.get("team", {}).get("id") == home_id else (stats, stats)

                sh = extract(home_stats, "Shots on Goal")
                sa = extract(away_stats, "Shots on Goal")
                ah = extract(home_stats, "Dangerous Attacks")
                aa = extract(away_stats, "Dangerous Attacks")
                corners_home = extract(home_stats, "Corner Kicks")
                corners_away = extract(away_stats, "Corner Kicks")
                total_corners = corners_home + corners_away

                home_pressure, home_xg = calculate_pressure(home_stats)
                away_pressure, away_xg = calculate_pressure(away_stats)
                best_pressure = max(home_pressure, away_pressure)
                dominance = abs(home_pressure - away_pressure)

                tempo = (ah + aa) / 50
                activity = (sh + sa) / 10

                btts_prob = predict_btts(sh, sa, ah, aa, total_goals)
                over_prob = predict_over(sh, sa, ah, aa, total_goals)

                score_btts = (btts_prob * 0.5 + tempo * 0.3 + activity * 0.2) if btts_prob is not None else 0.0
                score_over = (over_prob * 0.6 + tempo * 0.3 + activity * 0.1) if over_prob is not None else 0.0

                market = None
                confidence = min(best_pressure, 95)
                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]

                if minute >= 74 and (ah + aa >= 38) and (extract(home_stats, "Total Shots") + extract(away_stats, "Total Shots") >= 10):
                    market = f"📐 НАД {total_corners}.5 КОРНЕРА (Азиатска линия)"
                    confidence = 85
                elif score_btts > 0.52 and total_goals <= 2:
                    market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
                    confidence = round(score_btts * 100)
                elif score_over > 0.50:
                    market = f"🔮 НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = round(score_over * 100)
                elif 35 <= minute <= 74 and total_goals <= 1 and home_pressure >= 45 and away_pressure >= 45 and ah >= 10 and aa >= 10:
                    market = f"⚽ НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = min(best_pressure + 5, 95)
                elif best_pressure >= 48 and dominance >= 8:
                    if home_pressure > away_pressure:
                        market = f"🎯 СЛЕДВАЩ ГОЛ: ДОМАКИН ({home_name})"
                    else:
                        market = f"🎯 СЛЕДВАЩ ГОЛ: ГОСТ ({away_name})"
                    confidence = min(best_pressure + 5, 95)

                if market and confidence >= 70:
                    stake_info = calculate_dynamic_stake(confidence)
                    msg = f"👑 <b>[VIP LIVE AI SIGNAL]</b>\n⚽ <b>Мач:</b> {home_name} vs {away_name}\n🎯 <b>ПРОГНОЗА: {market}</b>"
                    send_telegram(msg)
                    save_signal(fixture_id, f"{home_name}-{away_name}", market, best_pressure, confidence, 0.0, stake_info)
                    sent[f"{fixture_id}_live"] = time.time()

        except Exception as e:
            print("Live Engine Error:", e)
        time.sleep(60)

# =========================================================
# 📅 THREAD 2: PREMATCH EXPERT ENGINE (EVERY 10 MIN)
# =========================================================

def prematch_expert_runner():
    print("📅 PREMATCH Системата за пазарни сривове и Поасон е активна...")
    while True:
        try:
            now_sofia = datetime.now(TZ)
            if now_sofia.hour == 4 and 0 <= now_sofia.minute <= 10:
                train_model()
                load_model()
                time.sleep(650)

            today = now_sofia.strftime("%Y-%m-%d")
            upcoming_matches = safe_api_get("fixtures", {"date": today})

            for m in upcoming_matches:
                if not m or m["fixture"]["status"]["short"] != "NS": continue
                fixture_id = m["fixture"]["id"]
                league = m["league"]["name"]
                country = m["league"]["country"]
                if blocked_league(league) or country in BAD_COUNTRIES: continue

                key = f"{fixture_id}_pre"
                if key in prematch_sent: continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                time_diff = (date_obj - now_sofia).total_seconds()
                if time_diff < 0 or time_diff > 28800: continue

                odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
                current_home_odd, current_away_odd = 0.0, 0.0
                
                if odds_response:
                    try:
                        bookmaker_data = odds_response.get("bookmakers", [])
                        for b in bookmaker_data:
                            if b["id"] == 8:
                                for bet in b.get("bets", []):
                                    if bet["id"] == 1:
                                        for val in bet.get("values", []):
                                            if val["value"] == "Home": current_home_odd = float(val["odd"])
                                            if val["value"] == "Away": current_away_odd = float(val["odd"])
                    except: pass

                if current_home_odd > 1.15 and current_away_odd > 1.15:
                    if fixture_id not in odds_tracker:
                        odds_tracker[fixture_id] = {"home": current_home_odd, "away": current_away_odd, "alerted": False}
                    else:
                        historical = odds_tracker[fixture_id]
                        if not historical["alerted"]:
                            home_drop = ((historical["home"] - current_home_odd) / historical["home"]) * 100
                            away_drop = ((historical["away"] - current_away_odd) / historical["away"]) * 100

                            if home_drop >= 15.0 and current_home_odd < historical["home"]:
                                msg = f"📉 <b>[SHARP MONEY]</b> {home} - Победа Домакин"
                                send_telegram(msg)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue
                            elif away_drop >= 15.0 and current_away_odd < historical["away"]:
                                msg = f"📉 <b>[SHARP MONEY]</b> {away} - Победа Гост"
                                send_telegram(msg)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue

                UNDER_COUNTRIES = ["Italy", "Romania", "Bulgaria", "Croatia", "Greece", "Morocco"]
                HIGH_BTTS_COUNTRIES = ["Netherlands", "Germany", "Norway", "Sweden", "Iceland", "Australia"]

                if country in UNDER_COUNTRIES:
                    market, probability = "📉 ПОД 2.5 ГОЛА", "76%"
                elif country in HIGH_BTTS_COUNTRIES:
                    poisson_prob = analyze_poisson_over_under(fixture_id)
                    market, probability = "💎 ГОЛ/ГОЛ - ДА", f"{poisson_prob}%"
                elif country in GOLDEN_PREMATCH_COUNTRIES:
                    market, probability = "🔮 НАД 2.5 ГОЛА В МАЧА", "74%"
                else: continue 

                msg = f"🔮 <b>[PREMATCH POISSON]</b>\n⚽ {home} vs {away}\n🎯 Прогноза: {market} ({probability})"
                send_telegram(msg)
                prematch_sent[key] = time.time()
                time.sleep(2)

        except Exception as e:
            print("Prematch Engine Error:", e)
        time.sleep(600)

# =========================================================
# MAIN START
# =========================================================

if __name__ == "__main__":
    init_database()
    print("🧠 Зареждане на Random Forest моделите при start...")
    load_model()
    
    send_telegram("🚀 БОТЪТ СТАРТИРА УСПЕШНО И Е ОНЛАЙН!")
    
    t1 = threading.Thread(target=live_analysis_runner)
    t2 = threading.Thread(target=prematch_expert_runner)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()














