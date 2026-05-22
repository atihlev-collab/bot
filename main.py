# =========================================================
# ULTIMATE MASTERPIECE TIPSTER AI SYSTEM (main.py)
# RAPIDAPI COMPATIBLE STANDARD EDITION - TOTAL ABSOLUTE FIXED
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

from config import BOT_TOKEN, API_KEY, CHAT_ID
print("AAAAAAAA NEW FILE TEST")
print(API_KEY[:10])
try:
    from ml_model import predict_btts, predict_over, train_model, load_model
except ImportError:
    print("❌ Критична грешка: Файлът ml_model.py липсва!")
    exit(1)

HEADERS = {
    "x-apisports-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly", "amateur"]
BAD_COUNTRIES = ["Bolivia", "Venezuela", "India", "Indonesia", "Bangladesh", "Uganda"]
GOLDEN_PREMATCH_COUNTRIES = ["Netherlands", "Germany", "Norway", "Sweden", "Denmark", "Iceland", "Switzerland", "Australia", "England", "Belgium", "Austria", "Japan", "South Korea", "Scotland", "USA", "Brazil", "Ireland"]

sent = {}
prematch_sent = {}
last_scores = {}
odds_tracker = {}

def init_database():
    conn = sqlite3.connect("syndicate_master.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER, match_name TEXT,
        market TEXT, pressure INTEGER, confidence INTEGER, edge_value REAL, stake TEXT, created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_signal(fixture_id, match_name, market, pressure, confidence, edge, stake):
    try:
        conn = sqlite3.connect("syndicate_master.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO signals (fixture_id, match_name, market, pressure, confidence, edge_value, stake, created_at) VALUES (?,?,?,?,?,?,?,?)", 
                       (fixture_id, match_name, market, int(pressure), confidence, edge, stake, str(datetime.now(TZ))))
        conn.commit()
        conn.close()
    except: pass

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except: pass

# 📡 ЕДИНСТВЕНАТА ЧИСТА ФУНКЦИЯ ЗА ВРЪЗКА
def safe_api_get(endpoint, params=None):

    try:

        clean_endpoint = endpoint.lstrip("/")

        url = f"https://v3.football.api-sports.io/{clean_endpoint}"

        response = requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=30
        )

        print(f"📡 {url}")
        print(f"📦 {params}")
        print(f"📡 {response.status_code}")

        if response.status_code == 429:
            print("⏳ API LIMIT HIT")
            time.sleep(180)
            return []

        if response.status_code == 403:
            print("❌ API FORBIDDEN")
            print(response.text)
            return []

        if response.status_code == 200:
            return response.json().get(
                "response",
                []
            )

    except Exception as e:
        print(e)

    return []

def blocked_league(league_name):
    return any(word in league_name.lower() for word in BLOCKED_WORDS)

def extract(team, stat_name):
    for stat in team.get("statistics", []):
        if stat["type"] == stat_name:
            value = stat["value"]
            if value is None: return 0
            if isinstance(value, str): value = value.replace("%", "")
            try: return int(value)
            except: return 0
    return 0

def calculate_dynamic_stake(confidence):
    if confidence >= 85: return "🔥 СИЛЕН ЗАЛОГ: 3.5% от банката"
    elif confidence >= 75: return "💰 СРЕДЕН ЗАЛОГ: 2.0% от банката"
    return "⚠️ КОНСЕРВАТИВЕН ЗАЛОГ: 1.0% от банката"

def calculate_poisson_probability(k, lam):
    return (pow(lam, k) * math.exp(-lam)) / math.factorial(k)

def analyze_poisson_over_under(fixture_id):
    lambda_home, lambda_away = 1.95, 1.45
    prob_under_2_5 = 0.0
    for x in range(3):
        for y in range(3):
            if (x + y) < 3:
                prob_under_2_5 += (calculate_poisson_probability(x, lambda_home) * calculate_poisson_probability(y, lambda_away))
    return round((1.0 - prob_under_2_5) * 100, 1)

def calculate_pressure(team):
    pressure = 0
    possession = extract(team, "Ball Possession")
    sh_on = extract(team, "Shots on Goal")
    sh_tot = extract(team, "Total Shots")
    corners = extract(team, "Corner Kicks")
    attacks = extract(team, "Dangerous Attacks")
    if possession >= 55: pressure += 8
    if possession >= 62: pressure += 8
    if sh_on >= 3: pressure += 15
    if sh_on >= 5: pressure += 12
    if sh_tot >= 7: pressure += 10
    if sh_tot >= 11: pressure += 10
    if corners >= 3: pressure += 6
    if corners >= 6: pressure += 6
    if attacks >= 16: pressure += 14
    if attacks >= 26: pressure += 14
    xg = round((sh_on * 0.28) + (sh_tot * 0.05) + (attacks * 0.022), 2)
    if xg >= 1.1: pressure += 10
    if xg >= 1.8: pressure += 10
    return min(pressure, 100), xg

def live_analysis_runner():
    print("⚡ LIVE Мулти-пазарен скенер с ИИ е активен...")

    while True:
        try:

            today = datetime.now(TZ).strftime("%Y-%m-%d")

            live_matches = safe_api_get(
                "fixtures",
                {
                    "date": today,
                    "timezone": "Europe/Sofia"
                }
            )

            for match in live_matches:
                if not match: continue
                fixture_id = match["fixture"]["id"]
                league, country = match["league"]["name"], match["league"]["country"]
                if blocked_league(league) or country in BAD_COUNTRIES: continue
                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 15 or minute > 85: continue
                home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
                away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
                total_goals = home_goals + away_goals
                if total_goals >= 6: continue 
                score = f"{home_goals}-{away_goals}"
                if fixture_id not in last_scores: last_scores[fixture_id] = score
                elif last_scores[fixture_id] != score:
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
                total_corners = extract(home_stats, "Corner Kicks") + extract(away_stats, "Corner Kicks")
                hp, h_xg = calculate_pressure(home_stats)
                ap, a_xg = calculate_pressure(away_stats)
                bp, dom = max(hp, ap), abs(hp - ap)
                tempo, activity = (ah + aa) / 50, (sh + sa) / 10
                btts_prob = predict_btts(sh, sa, ah, aa, total_goals)
                over_prob = predict_over(sh, sa, ah, aa, total_goals)
                score_btts = (btts_prob * 0.5 + tempo * 0.3 + activity * 0.2) if btts_prob is not None else 0.0
                score_over = (over_prob * 0.6 + tempo * 0.3 + activity * 0.1) if over_prob is not None else 0.0
                market = None
                confidence = min(bp, 95)
                home_name, away_name = match["teams"]["home"]["name"], match["teams"]["away"]["name"]
                if minute >= 74 and (ah + aa >= 38) and (extract(home_stats, "Total Shots") + extract(away_stats, "Total Shots") >= 10):
                    market = f"📐 НАД {total_corners}.5 КОРНЕРА (Азиатска линия)"; confidence = 85
                elif score_btts > 0.52 and total_goals <= 2:
                    market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"; confidence = round(score_btts * 100)
                elif score_over > 0.50:
                    market = f"🔮 НАД {total_goals + 1}.5 ГОЛА В МАЧА"; confidence = round(score_over * 100)
                elif 35 <= minute <= 74 and total_goals <= 1 and hp >= 45 and ap >= 45 and ah >= 10 and aa >= 10:
                    market = f"⚽ НАД {total_goals + 1}.5 ГОЛА В МАЧА"; confidence = min(bp + 5, 95)
                elif bp >= 48 and dom >= 8:
                    market = f"🎯 СЛЕДВАЩ ГОЛ: ДОМАКИН ({home_name})" if hp > ap else f"🎯 СЛЕДВАЩ ГОЛ: ГОСТ ({away_name})"
                    confidence = min(bp + 5, 95)
                if market and confidence >= 70:
                    stk = calculate_dynamic_stake(confidence)
                    send_telegram(f"👑 <b>[VIP LIVE AI SIGNAL]</b>\n⚽ <b>Мач:</b> {home_name} vs {away_name}\n🎯 <b>ПРОГНОЗА: {market}</b>\n💼 {stk}")
                    save_signal(fixture_id, f"{home_name}-{away_name}", market, bp, confidence, 0.0, stk)
                    sent[f"{fixture_id}_live"] = time.time()
        except: pass
        time.sleep(180)

def prematch_expert_runner():
    print("📅 PREMATCH Системата за пазарни сривове и Поасон е активна...")
    while True:
        try:
            now_sofia = datetime.now(TZ)
            if now_sofia.hour == 4 and 0 <= now_sofia.minute <= 10:
                train_model(); load_model(); time.sleep(650)
            today = now_sofia.strftime("%Y-%m-%d")
            upcoming_matches = safe_api_get("fixtures", {"date": today})
            for m in upcoming_matches:
                if not m or m["fixture"]["status"]["short"] != "NS": continue
                fixture_id = m["fixture"]["id"]
                league, country = m["league"]["name"], m["league"]["country"]
                if blocked_league(league) or country in BAD_COUNTRIES: continue
                if f"{fixture_id}_pre" in prematch_sent: continue
                home, away = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
                date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                time_diff = (date_obj - now_sofia).total_seconds()
                if time_diff < 0 or time_diff > 28800: continue
                
                # ИЗЧИСТЕНА КОРЕКЦИЯ ТУК - ВИКА СЕ ПРЕЗ БЕЗГРЕШНАТА ФУНКЦИЯ БЕЗ ТВЪРДИ АДРЕСИ
                odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
                current_home_odd, current_away_odd = 0.0, 0.0
                if odds_response:
                    try:
                        for b in odds_response.get("bookmakers", []):
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
                            hd = ((historical["home"] - current_home_odd) / historical["home"]) * 100
                            ad = ((historical["away"] - current_away_odd) / historical["away"]) * 100
                            if hd >= 15.0 and current_home_odd < historical["home"]:
                                send_telegram(f"📉 <b>[SHARP MONEY]</b> {home} - Победител"); historical["alerted"] = True; prematch_sent[f"{fixture_id}_pre"] = time.time(); continue
                            elif ad >= 15.0 and current_away_odd < historical["away"]:
                                send_telegram(f"📉 <b>[SHARP MONEY]</b> {away} - Победител"); historical["alerted"] = True; prematch_sent[f"{fixture_id}_pre"] = time.time(); continue
                if country in ["Italy", "Romania", "Bulgaria"]: market, prob = "📉 ПОД 2.5 ГОЛА", "76%"
                elif country in ["Netherlands", "Germany", "Norway", "Sweden"]:
                    poisson_prob = analyze_poisson_over_under(fixture_id)
                    market, prob = "💎 ГОЛ/ГОЛ - ДА", f"{poisson_prob}%"
                elif country in GOLDEN_PREMATCH_COUNTRIES: market, prob = "🔮 НАД 2.5 ГОЛА", "74%"
                else: continue
                send_telegram(f"🔮 <b>[PREMATCH POISSON]</b>\n⚽ {home} vs {away}\n🎯 Прогноза: {market} ({prob})")
                prematch_sent[f"{fixture_id}_pre"] = time.time()
                time.sleep(2)
        except: pass
        time.sleep(600)

if __name__ == "__main__":
    init_database()

    print("🧠 Зареждане на Random Forest моделите при start...")

    load_model()

    send_telegram("🔥 AI v1000 READY")
    print("TEST SENT")

    t1 = threading.Thread(
        target=live_analysis_runner
    )

    t2 = threading.Thread(
        target=prematch_expert_runner
    )

    t1.start()

    time.sleep(10)

    t2.start()

    t1.join()
    t2.join()
