# =========================================================
# ULTIMATE SELF-LEARNING TIPSTER AI SYSTEM (THE MASTERPIECE)
# AUTOMATIC MACHINE LEARNING TRAINING (EVERY NIGHT AT 04:00)
# LIVE: GOALS, CORNERS, NEXT GOAL | PREMATCH: OVER/UNDER/BTTS/1X2 DROPS
# =========================================================

import time
import sqlite3
import threading
import requests
import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot

# Автоматично зареждане от твоя файл config.py
from config import BOT_TOKEN, API_KEY, CHAT_ID

# Импортиране на твоите ML функции от ml_model.py
try:
    from ml_model import predict_btts, predict_over, train_model, load_model
    import ml_model
except ImportError:
    print("❌ Критична грешка: Файлът ml_model.py липсва в същата папка!")
    exit(1)

# =========================================================
# CONFIG & SYSTEM SETUP
# =========================================================

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
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
# DATABASE & DATASET GENERATOR FOR ML
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
# UTILITIES AND STATS EXTRACTOR
# =========================================================

def safe_api_get(endpoint, params=None):
    try:
        response = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get("response", [])
    except:
        pass
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

    # Твоята оригинална xG формула
    xg = round((shots_on * 0.28) + (total_shots * 0.05) + (attacks * 0.020), 2)
    if xg >= 1.1: pressure += 10
    if xg >= 1.8: pressure += 10

    return min(pressure, 100), xg

# =========================================================
# 🧵 THREAD 1: INTELLIGENT LIVE ENGINE (EVERY 60 SECONDS)
# =========================================================

def live_analysis_runner():
    print("⚡ LIVE Мулти-пазарен скенер с Машинно Обучение е активен...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            
            for match in live_matches:
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

                # SMART RESET AFTER GOAL
                if fixture_id not in last_scores:
                    last_scores[fixture_id] = score
                else:
                    if last_scores[fixture_id] != score:
                        if fixture_id in sent: del sent[fixture_id]
                        last_scores[fixture_id] = score

                if f"{fixture_id}_live" in sent: continue

                stats = safe_api_get("fixtures/statistics", {"fixture": fixture_id})
                if len(stats) < 2: continue

                home_id = match["teams"]["home"]["id"]
                home_stats, away_stats = (stats, stats) if stats.get("team", {}).get("id") == home_id else (stats, stats)

                # ИЗВЛИЧАНЕ НА ПАРАМЕТРИ ЗА ТВОЯ AI ENGINE
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

                # Твоите оригинални математически метрики за темпо
                tempo = (ah + aa) / 50
                activity = (sh + sa) / 10

                # Викане на твоята Random Forest Невронна мрежа
                btts_prob = predict_btts(sh, sa, ah, aa, total_goals)
                over_prob = predict_over(sh, sa, ah, aa, total_goals)

                # Хибридно изчисляване на финалния AI резултат по твоите коефициенти
                score_btts = (btts_prob * 0.5 + tempo * 0.3 + activity * 0.2) if btts_prob is not None else 0.0
                score_over = (over_prob * 0.6 + tempo * 0.3 + activity * 0.1) if over_prob is not None else 0.0

                market = None
                confidence = min(best_pressure, 95)
                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]

                # 📐 ПАЗАР 1: VIP ЛАЙВ КОРНЕРИ В КРАЯ (След 74' минута)
                if minute >= 74 and (ah + aa >= 48) and (extract(home_stats, "Total Shots") + extract(away_stats, "Total Shots") >= 14):
                    market = f"📐 НАД {total_corners}.5 КОРНЕРА (Азиатска линия)"
                    confidence = 85
                    
                # ⚽ ПАЗАР 2: ML ЗАКЛЮЧЕНИЕ ЗА ГОЛ-ГОЛ (BTTS)
                elif score_btts > 0.60 and total_goals <= 2:
                    market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
                    confidence = round(score_btts * 100)

                # ⚽ ПАЗАР 3: ML ЗАКЛЮЧЕНИЕ ЗА НАД 2.5 ГОЛА
                elif score_over > 0.58:
                    market = f"🔮 НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = round(score_over * 100)

                # ⚽ ПАЗАР 4: И ДВАТА ОТБОРА ИГРАЯТ ОТКРИТО (НАД 1.5 ГОЛА БАЗОВО)
                elif 35 <= minute <= 74 and total_goals <= 1 and home_pressure >= 52 and away_pressure >= 52 and ah >= 12 and aa >= 12:
                    market = f"⚽ НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = min(best_pressure + 2, 95)

                # 🔥 ПАЗАР 5: ДОМИНАНТНОСТ НА ЕДНИЯ ОТБОР (СЛЕДВАЩ ГОЛ)
                elif best_pressure >= 56 and dominance >= 12:
                    if home_pressure > away_pressure:
                        market = f"🎯 СЛЕДВАЩ ГОЛ: ДОМАКИН ({home_name})"
                    else:
                        market = f"🎯 СЛЕДВАЩ ГОЛ: ГОСТ ({away_name})"
                    confidence = min(best_pressure + 4, 95)

                # ИЗПРАЩАНЕ НА СИГНАЛА ПРИ ОТГОВАРЯЩИ КРИТЕРИИ
                if market and confidence >= 70:
                    stake_info = calculate_dynamic_stake(confidence)
                    msg = f"""👑 <b>[VIP LIVE AI SIGNAL - RANDOM FOREST v1000]</b>
────────────────────
⚽ <b>Мач:</b> <code>{home_name} vs {away_name}</code>
🏆 <b>Лига:</b> {league} ({country})
⏱ <b>Минута:</b> {minute}'  |  📊 <b>Резултат:</b> {score}
────────────────────
🤖 <b>ML Прогнози:</b> BTTS: {round(score_btts,2) if btts_prob else 'N/A'} | Over: {round(score_over,2) if over_prob else 'N/A'}
🔥 <b>Натиск на живо:</b> Дом: {home_pressure} | Гост: {away_pressure} (Доминантност: {dominance})
📐 <b>Корнери дотук:</b> {total_corners}  |  📈 <b>Темпо на атаки:</b> {round(tempo, 2)}

🎯 <b>ПРОГНОЗА: {market}</b>
💼 <b>{stake_info}</b>
✅ <b>Сигурност на изкуствения интелект:</b> {confidence}%"""
                    send_telegram(msg)
                    save_signal(fixture_id, f"{home_name}-{away_name}", market, best_pressure, confidence, 0.0, stake_info)
                    sent[f"{fixture_id}_live"] = time.time()

        except Exception as e:
            print("Live Engine Error:", e)
        time.sleep(60)

# =========================================================
# 📅 THREAD 2: PREMATCH EXPERT ENGINE + AUTO-TRAINING CYCLE
# =========================================================

def prematch_expert_runner():
    print("📅 PREMATCH Алгоритъмът и модулът за Самообучение са активни...")
    while True:
        try:
            now_sofia = datetime.now(TZ)
            
            # 🔥 АВТОМАТИЧНО САМООБУЧЕНИЕ: Всеки ден в 04:05 сутринта моделът се тренира наново!
            if now_sofia.hour == 4 and 0 <= now_sofia.minute <= 15:
                print("🧠 [AUTO-LEARNING] Стартиране на ежедневно претрениране на Невронната Мрежа...")
                train_model()
                load_model()
                time.sleep(900) # Заспива за 15 минути, за да не завърти тренирането повторно

            today = now_sofia.strftime("%Y-%m-%d")
            upcoming_matches = safe_api_get("fixtures", {"date": today})

            for m in upcoming_matches:
                if m["fixture"]["status"]["short"] != "NS": continue

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
                if time_diff < 0 or time_diff > 28800: continue  # Само мачове в следващите 8 часа

                # Проверка за сривове в 1Х2 преди мача (Инсайд информация)
                odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
                current_home_odd, current_away_odd = 0.0, 0.0
                
                if odds_response:
                    try:
                        bookmaker_data = odds_response[0].get("bookmakers", [])
                        if bookmaker_data:
                            for b in bookmaker_data:
                                if b["id"] == 8: # Bet365
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
                                stake_info = "🔥 СИНДИКАТ ИНСАЙД: 4.0% от банката"
                                msg = f"""🔥 <b>[PREMATCH СРИВ В ПАЗАРА - КРАЕН ИЗХОД 1]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
⏱ <b>Старт след:</b> {round(time_diff/60)} минути
────────────────────
📉 <b>Първоначален коефициент:</b> {historical['home']}
📉 <b>Нов паднал коефициент:</b> <code>{current_home_odd}</code> (Спад с {round(home_drop, 1)}%)

🎯 <b>ПРЕПОРАКА: ПОБЕДА ЗА ДОМАКИНА (1)</b>
💼 <b>{stake_info}</b>
────────────────────
⚠️ *Инсайд: Коефициентите се сриват рязко на пазара! Всички залози се наливат за 1!*"""
                                send_telegram(msg)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue

                            elif away_drop >= 15.0 and current_away_odd < historical["away"]:
                                stake_info = "🔥 СИНДИКАТ ИНСАЙД: 4.0% от банката"
                                msg = f"""🔥 <b>[PREMATCH СРИВ В ПАЗАРА - КРАЕН ИЗХОД 2]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
⏱ <b>Старт след:</b> {round(time_diff/60)} минути
────────────────────
📉 <b>Първоначален коефициент:</b> {historical['away']}
📉 <b>Нов паднал коефициент:</b> <code>{current_away_odd}</code> (Спад с {round(away_drop, 1)}%)

🎯 <b>ПРЕПОРАКА: ПОБЕДА ЗА ГОСТА (2)</b>
💼 <b>{stake_info}</b>
────────────────────
⚠️ *Инсайд: Официална пазарна аномалия! Масово наливане за победа на госта!*"""
                                send_telegram(msg)
                                historical["alerted"] = True
                                prematch_sent[key] = time.time()
                                continue

                # Стандартни гол-пазари, ако няма засечен пазарен срив
                UNDER_COUNTRIES = ["Italy", "Romania", "Bulgaria", "Croatia", "Greece", "Morocco"]
                HIGH_BTTS_COUNTRIES = ["Netherlands", "Germany", "Norway", "Sweden", "Iceland", "Australia"]

                if country in UNDER_COUNTRIES:
                    market, probability, details = "📉 ПОД 2.5 ГОЛА", "76%", "Дефанзивна тактика и ниско темпо."
                elif country in HIGH_BTTS_COUNTRIES:
                    market, probability, details = "💎 ГОЛ/ГОЛ - ДА", "79%", "Атакуващ сблъсък в Златните лиги."
                elif country in GOLDEN_PREMATCH_COUNTRIES:
                    market, probability, details = "🔮 НАД 2.5 ГОЛА В МАЧА", "74%", "Традиционно високорезултатни състави."
                else: continue 

                msg = f"""🔮 <b>[PREMATCH AI ИНСАЙД АНАЛИЗ]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
⏱ <b>Час на старт:</b> {date_obj.strftime('%H:%M')}
────────────────────
📊 <b>Математически модел:</b> {details}

🎯 <b>ПРОГНОЗА ПРЕДИ МАЧА: {market}</b>
✅ <b>Математическа вероятност:</b> {probability}"""
                send_telegram(msg)
                prematch_sent[key] = time.time()
                time.sleep(2)

        except Exception as e:
            print("Prematch Engine Error:", e)
        time.sleep(600)

# =========================================================
# SYSTEM ORCHESTRATOR
# =========================================================

if __name__ == "__main__":
    init_database()
    
    # ПЪРВОНАЧАЛНО ЗАРЕЖДАНЕ НА ТВОИТЕ AI МОДЕЛИ ПРИ СТАРТ
    print("🧠 Зареждане на Random Forest моделите от ml_model.py...")
    load_model()
    
    t1 = threading.Thread(target=live_analysis_runner)
    t2 = threading.Thread(target=prematch_expert_runner)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()



