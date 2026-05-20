# =========================================================
# SYNDICATE MASTER GLOBAL AI SYSTEM - ULTRA LIGHT STABLE
# LIVE: GOALS, CORNERS, NEXT GOAL | PREMATCH: SHARP DROPS
# TARGET: 3-5 HIGH VALUE VALUE SIGNALS PER DAY
# =========================================================

import time
import sqlite3
import threading
import requests
import asyncio
import os
import math
import json
from datetime import datetime
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly", "amateur", "women", "female"]

sent = {}
prematch_sent = {}
last_scores = {}
odds_tracker = {}

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
        """, (fixture_id, match_name, market, int(pressure), confidence, edge, stake, str(datetime.now())))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB Error:", e)

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except Exception as e:
        print("Telegram Error:", e)

def safe_api_get(endpoint, params=None):
    try:
        response = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=10)
        if response.status_code == 200: return response.json().get("response", [])
    except: pass
    return []

def is_first_league_or_global(league_name):
    text = league_name.lower()
    first_league_keywords = ["premier", "liga 1", "league 1", "serie a", "division 1", "primera", "super", "pro league", "eredivisie", "bundesliga", "championship"]
    if any(kw in text for kw in first_league_keywords):
        return True
    return not any(word in text for word in BLOCKED_WORDS)

def extract(team_stats_list, stat_name):
    if not team_stats_list: return 0
    for stat in team_stats_list:
        if stat.get("type") == stat_name:
            value = stat.get("value")
            if value is None: return 0
            if isinstance(value, str):
                value = value.replace("%", "").strip()
                try: return int(value)
                except: return 0
            return int(value)
    return 0

def calculate_dynamic_stake(confidence):
    if confidence >= 83: return "🔥 СИЛЕН ЗАЛОГ: 3.5% от банката"
    elif confidence >= 73: return "💰 СРЕДЕН ЗАЛОГ: 2.0% от банката"
    else: return "⚠️ КОНСЕРВАТИВЕН ЗАЛОГ: 1.0% от банката"

def calculate_pressure(team_stats_list):
    pressure = 0
    possession = extract(team_stats_list, "Ball Possession")
    shots_on = extract(team_stats_list, "Shots on Goal")
    total_shots = extract(team_stats_list, "Total Shots")
    corners = extract(team_stats_list, "Corner Kicks")
    attacks = extract(team_stats_list, "Dangerous Attacks")

    if possession >= 54: pressure += 5
    if possession >= 62: pressure += 5
    if shots_on >= 2: pressure += 18
    if shots_on >= 4: pressure += 15
    if total_shots >= 5: pressure += 12
    if total_shots >= 8: pressure += 10
    if corners >= 2: pressure += 5
    if corners >= 4: pressure += 5
    if attacks >= 12: pressure += 12
    if attacks >= 20: pressure += 13

    xg = round((shots_on * 0.32) + (total_shots * 0.06) + (attacks * 0.025), 2)
    if xg >= 0.8: pressure += 10
    if xg >= 1.4: pressure += 10

    return min(pressure, 100), xg

def live_analysis_runner():
    print("⚡ LIVE Скенерът работи стабилно...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            for match in live_matches:
                fixture_id = match["fixture"]["id"]
                league = match["league"]["name"]
                country = match["league"]["country"]
                
                if not is_first_league_or_global(league): continue

                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 15 or minute > 85: continue

                home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
                away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
                total_goals = home_goals + away_goals
                if total_goals >= 6: continue 
                score = f"{home_goals}-{away_goals}"

                if fixture_id not in last_scores: last_scores[fixture_id] = score
                else:
                    if last_scores[fixture_id] != score:
                        if f"{fixture_id}_live" in sent: del sent[f"{fixture_id}_live"]
                        last_scores[fixture_id] = score

                if f"{fixture_id}_live" in sent: continue

                stats = safe_api_get("fixtures/statistics", {"fixture": fixture_id})
                if not stats or len(stats) < 2: continue

                home_id = match["teams"]["home"]["id"]
                home_stats, away_stats = [], []
                
                for item in stats:
                    if item.get("team", {}).get("id") == home_id:
                        home_stats = item.get("statistics", [])
                    else:
                        away_stats = item.get("statistics", [])

                if not home_stats or not away_stats: continue

                sh = extract(home_stats, "Shots on Goal")
                sa = extract(away_stats, "Shots on Goal")
                ah = extract(home_stats, "Dangerous Attacks")
                aa = extract(away_stats, "Dangerous Attacks")
                corn_home = extract(home_stats, "Corner Kicks")
                corn_away = extract(away_stats, "Corner Kicks")
                total_corners = corn_home + corn_away

                home_pressure, home_xg = calculate_pressure(home_stats)
                away_pressure, away_xg = calculate_pressure(away_stats)
                best_pressure = max(home_pressure, away_pressure)
                dominance = abs(home_pressure - away_pressure)

                market = None
                confidence = min(best_pressure, 95)
                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]

                required_pressure = 55 

                # 📐 ПАЗАР 1: КОРНЕРИ
                if minute >= 74 and (ah + aa >= 28) and (extract(home_stats, "Total Shots") + extract(away_stats, "Total Shots") >= 8):
                    market = f"📐 НАД {total_corners}.5 КОРНЕРА (Азиатска линия)"
                    confidence = 82
                # ⚽ ПАЗАР 2: СТАТИСТИЧЕСКИ НАД 1.5/2.5 ГОЛА
                elif 30 <= minute <= 74 and total_goals <= 1 and best_pressure >= required_pressure and ah >= 8 and aa >= 8:
                    market = f"⚽ НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = min(best_pressure + 4, 95)
                # 🔥 ПАЗАР 3: ДОМИНАНТНОСТ (СЛЕДВАЩ ГОЛ)
                elif best_pressure >= 55 and dominance >= 10 and max(sh, sa) >= 2:
                    if home_pressure > away_pressure: market = f"🎯 СЛЕДВАЩ ГОЛ: ДОМАКИН ({home_name})"
                    else: market = f"🎯 СЛЕДВАЩ ГОЛ: ГОСТ ({away_name})"
                    confidence = min(best_pressure + 4, 95)

                if market and confidence >= 70:
                    stake_info = calculate_dynamic_stake(confidence)
                    sent[f"{fixture_id}_live"] = time.time()
                    save_signal(fixture_id, f"{home_name}-{away_name}", market, best_pressure, confidence, 0.0, stake_info)
                    
                    msg = f"""👑 <b>[VIP AI SIGNAL - GLOBAL PRO]</b>
────────────────────
⚽ <b>Мач:</b> <code>{home_name} vs {away_name}</code>
🏆 <b>Лига:</b> {league} ({country})
⏱ <b>Минута:</b> {minute}'  |  📊 <b>Резултат:</b> {score}
────────────────────
🔥 <b>Натиск:</b> Дом: {home_pressure}% | Гост: {away_pressure}%
📐 <b>Корнери:</b> {total_corners}
────────────────────
🎯 <b>ПРОГНОЗА: {market}</b>
💼 <b>{stake_info}</b>
✅ <b>Сигурност:</b> {confidence}%"""
                    
                    send_telegram(msg)

            time.sleep(60)
        except Exception as e:
            print("Live Error:", e)
            time.sleep(10)

def prematch_expert_runner():
    print("📅 PREMATCH Модулът работи стабилно...")
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            upcoming_matches = safe_api_get("fixtures", {"date": today})

            for m in upcoming_matches:
                if m["fixture"]["status"]["short"] != "NS": continue
                fixture_id = m["fixture"]["id"]
                league = m["league"]["name"]
                
                if not is_first_league_or_global(league): continue

                key = f"{fixture_id}_pre"
                if key in prematch_sent: continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]
                country = m["league"]["country"]

                odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
                current_home_odd, current_away_odd = 0.0, 0.0
                if odds_response:
                    try:
                        bookmaker_data = odds_response if isinstance(odds_response, list) else odds_response.get("bookmakers", [])
                        if bookmaker_data and len(bookmaker_data) > 0:
                            for b in bookmaker_data:
                                if b.get("id") == 8 or b.get("bookmaker", {}).get("id") == 8:
                                    for bet in b.get("bets", []):
                                        if bet.get("id") == 1:
                                            for val in bet.get("values", []):
                                                if val.get("value") == "Home": current_home_odd = float(val.get("odd", 0))
                                                if val.get("value") == "Away": current_away_odd = float(val.get("odd", 0))
                    except: pass

                if current_home_odd > 1.15 and current_away_odd > 1.15:
                    if fixture_id not in odds_tracker:
                        odds_tracker[fixture_id] = {"home": current_home_odd, "away": current_away_odd, "alerted": False}
                    else:
                        historical = odds_tracker[fixture_id]
                        if not historical["alerted"]:
                            home_drop = ((historical["home"] - current_home_odd) / historical["home"]) * 100
                            away_drop = ((historical["away"] - current_away_odd) / historical["away"]) * 100

                            if home_drop >= 10.0 and current_home_odd < historical["home"]:
                                stake_info = calculate_dynamic_stake(85)
                                msg = f"""🔥 <b>[SHARP MONEY ALERT - КРАЕН ИЗХОД 1]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
────────────────────
📉 <b>Първоначален коефициент:</b> {historical['home']}
📉 <b>Нов паднал коефициент:</b> <code>{current_home_odd}</code> (Спад с -{round(home_drop, 1)}%)

🎯 <b>ПРЕПОРАКА: ПОБЕДА ЗА ДОМАКИНА (1)</b>
💼 <b>{stake_info}</b>"""
                                send_telegram(msg)
                                save_signal(fixture_id, f"{home}-{away}", "PREMATCH_1", f"DROP_{round(home_drop)}%", 85, current_home_odd, stake_info)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue

                            elif away_drop >= 10.0 and current_away_odd < historical["away"]:
                                stake_info = calculate_dynamic_stake(85)
                                msg = f"""🔥 <b>[SHARP MONEY ALERT - КРАЕН ИЗХОД 2]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
────────────────────
📉 <b>Първоначален коефициент:</b> {historical['away']}
📉 <b>Нов паднал коефициент:</b> <code>{current_away_odd}</code> (Спад с -{round(away_drop, 1)}%)

🎯 <b>ПРЕПОРАКА: ПОБЕДА ЗА ГОСТА (2)</b>
💼 <b>{stake_info}</b>"""
                                send_telegram(msg)
                                save_signal(fixture_id, f"{home}-{away}", "PREMATCH_2", f"DROP_{round(away_drop)}%", 85, current_away_odd, stake_info)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue
            time.sleep(300)
        except Exception as e:
            print("Prematch Error:", e)
            time.sleep(30)

if __name__ == "__main__":
    init_database()
    print("🚀 Системата се стартира успешно в стабилен режим...")
    
    live_thread = threading.Thread(target=live_analysis_runner, daemon=True)
    live_thread.start()
    
    prematch_expert_runner()









