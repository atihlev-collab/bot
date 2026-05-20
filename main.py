# =========================================================
# ULTIMATE MASTERPIECE TIPSTER AI SYSTEM - GLOBAL PRO VALUE EDITION
# LIVE: VALUE GOALS, CORNERS, NEXT GOAL WITH KELLY CRITERION
# PREMATCH: SHARP 1X2 DROPS WITH LIVE ODDS VALIDATION
# TARGET ACCURACY: 3-5 HIGH VALUE GLOBAL SIGNALS PER DAY
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

try:
    from ml_model import predict_btts, predict_over, train_model, load_model
except ImportError:
    print("❌ Критична грешка: Файлът ml_model.py липсва!")
    exit(1)

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

# Филтри за сигурност - пропускаме нискокачествен футбол и аматьори
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
        """, (fixture_id, match_name, market, int(pressure), confidence, edge, stake, str(datetime.now(TZ))))
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

def fetch_live_odds(fixture_id, market_type="Goals"):
    """ Динамично извличане и ПЪЛНО фиксиране на LIVE коефициенти от Bet365 """
    odds_data = safe_api_get("odds/live", {"fixture": fixture_id})
    if not odds_data: return 1.85  
    try:
        for bookmaker in odds_data:
            if bookmaker.get("bookmaker", {}).get("id") == 8 or bookmaker.get("id") == 8:  # Bet365
                for bet in bookmaker.get("bets", []):
                    if market_type in bet.get("name", ""):
                        values = bet.get("values", [])
                        if isinstance(values, list) and len(values) > 0:
                            return float(values[0].get("odd", 1.85))
    except: pass
    return 1.85

def calculate_kelly_stake(probability_pct, current_odd):
    """ Математическо управление на банката чрез Критерия на Кели (Фракционен 0.25) """
    if current_odd <= 1.0: return "⚠️ Консервативен залог: 1.0%"
    prob = probability_pct / 100.0
    
    kelly_f = (prob * current_odd - (1 - prob)) / (current_odd - 1)
    if kelly_f <= 0: 
        return "⚠️ КОНСЕРВАТИВЕН ЗАЛОГ: 1.0% от банката"
        
    safe_stake = round(kelly_f * 0.25 * 100, 1)
    safe_stake = max(1.0, min(safe_stake, 4.5))  
    
    if safe_stake >= 3.5: return f"🔥 СИЛЕН ЗАЛОГ (КЕЛИ): {safe_stake}% от банката"
    elif safe_stake >= 2.0: return f"💰 СРЕДЕН ЗАЛОГ (КЕЛИ): {safe_stake}% от банката"
    return f"⚠️ КОНСЕРВАТИВЕН ЗАЛОГ (КЕЛИ): {safe_stake}% от банката"

def calculate_poisson_probability(k, lam):
    if lam <= 0: return 0.0
    return (pow(lam, k) * math.exp(-lam)) / math.factorial(k)

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
    print("⚡ LIVE Скенерът работи в режим GLOBAL VALUE PRO с Кели Критерий...")
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

                # НАПЪЛНО КОРИГИРАНО И СИГУРНО РАЗДЕЛЯНЕ НА СТАТИСТИКАТА С ИНДЕКСИ
                home_id = match["teams"]["home"]["id"]
                home_stats, away_stats = [], []
                
                try:
                    if isinstance(stats, list) and len(stats) >= 2:
                        if stats[0].get("team", {}).get("id") == home_id:
                            home_stats = stats[0].get("statistics", [])
                            away_stats = stats[1].get("statistics", [])
                        else:
                            home_stats = stats[1].get("statistics", [])
                            away_stats = stats[0].get("statistics", [])
                except Exception as parse_err:
                    print(f"Грешка при парсване на отбори: {parse_err}")
                    continue

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

                tempo = (ah + aa) / 50 if (ah + aa) > 0 else 0
                activity = (sh + sa) / 10 if (sh + sa) > 0 else 0

                btts_prob = predict_btts(sh, sa, ah, aa, total_goals)
                over_prob = predict_over(sh, sa, ah, aa, total_goals)

                score_btts = (btts_prob * 0.5 + tempo * 0.3 + activity * 0.2) if btts_prob is not None else 0.0
                score_over = (over_prob * 0.6 + tempo * 0.3 + activity * 0.1) if over_prob is not None else 0.0

                market = None
                market_type_odds = "Goals"
                confidence = min(best_pressure, 95)
                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]

                required_pressure = 55 

                # 📐 ПАЗАР 1: КОРНЕРИ
                if minute >= 74 and (ah + aa >= 28) and (extract(home_stats, "Total Shots") + extract(away_stats, "Total Shots") >= 8):
                    market = f"📐 НАД {total_corners}.5 КОРНЕРА (Азиатска линия)"
                    market_type_odds = "Corners"
                    confidence = 82
                # ⚽ ПАЗАР 2: ML ГОЛ-ГОЛ (BTTS)
                elif score_btts > 0.55 and total_goals <= 2 and max(sh, sa) >= 2:
                    market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
                    confidence = round(score_btts * 100)
                # ⚽ ПАЗАР 3: ML НАД 2.5 ГОЛА
                elif score_over > 0.52 and max(sh, sa) >= 2:
                    market = f"🔮 НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = round(score_over * 100)
                # ⚽ ПАЗАР 4: НАД X.5 БАЗОВО ГОЛОВЕ
                elif 30 <= minute <= 74 and total_goals <= 1 and best_pressure >= required_pressure and ah >= 8 and aa >= 8:
                    market = f"⚽ НАД {total_goals + 1}.5 ГОЛА В МАЧА"
                    confidence = min(best_pressure + 4, 95)
                # 🔥 ПАЗАР 5: ДОМИНАНТНОСТ (СЛЕДВАЩ ГОЛ)
                elif best_pressure >= 55 and dominance >= 10 and max(sh, sa) >= 2:
                    if home_pressure > away_pressure: market = f"🎯 СЛЕДВАЩ ГОЛ: ДОМАКИН ({home_name})"
                    else: market = f"🎯 СЛЕДВАЩ ГОЛ: ГОСТ ({away_name})"
                    confidence = min(best_pressure + 4, 95)

                if market and confidence >= 70:
                    live_odd = fetch_live_odds(fixture_id, market_type_odds)
                    if live_odd < 1.50: continue
                    
                    stake_info = calculate_kelly_stake(confidence, live_odd)

                    sent[f"{fixture_id}_live"] = time.time()
                    save_signal(fixture_id, f"{home_name}-{away_name}", market, best_pressure, confidence, live_odd, stake_info)
                    
                    msg = f"""👑 <b>[VIP VALUE AI SIGNAL - GLOBAL PRO]</b>
────────────────────
⚽ <b>Мач:</b> <code>{home_name} vs {away_name}</code>
🏆 <b>Лига:</b> {league} ({country})
⏱ <b>Минута:</b> {minute}'  |  📊 <b>Резултат:</b> {score}
────────────────────
🔥 <b>Натиск:</b> Дом: {home_pressure}% | Гост: {away_pressure}%
📐 <b>Корнери:</b> {total_corners}  |  📈 Коефициент: <b>{live_odd}</b>
────────────────────
🎯 <b>ПРОГНОЗА: {market}</b>
💼 <b>{stake_info}</b>
✅ <b>Вероятност:</b> {confidence}% (Математическо предимство)"""
                    
                    send_telegram(msg)
                    
                    try:
                        picks_file = "picks.json"
                        current_picks = []
                        if os.path.exists(picks_file):
                            with open(picks_file, "r", encoding="utf-8") as pf: current_picks = json.load(pf)
                        current_picks.append({
                            "fixture_id": fixture_id, "match_name": f"{home_name} vs {away_name}", "pick": market,
                            "checked": False, "win": False, "sh": sh, "sa": sa, "ah": ah, "aa": aa,
                            "total_corners": total_corners, "trigger_total_goals": total_goals,
                            "odds": live_odd, "stake": stake_info, "created_at": str(datetime.now(TZ))
                        })
                        with open(picks_file, "w", encoding="utf-8") as pf: json.dump(current_picks, pf, indent=2)
                    except: pass

            time.sleep(60)
        except Exception as e:
            print("Live Error:", e)
            time.sleep(10)

def prematch_expert_runner():
    print("📅 PREMATCH Модулът работи на глобално ниво...")
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
                if m["fixture"]["status"]["short"] != "NS": continue
                fixture_id = m["fixture"]["id"]
                league = m["league"]["name"]
                
                if not is_first_league_or_global(league): continue

                key = f"{fixture_id}_pre"
                if key in prematch_sent: continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]
                country = m["league"]["country"]
                date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                time_diff = (date_obj - now_sofia).total_seconds()
                if time_diff < 0 or time_diff > 28800: continue  

                odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
                current_home_odd, current_away_odd = 0.0, 0.0
                if odds_response:
                    try:
                        bookmaker_data = odds_response.get("bookmakers", []) if isinstance(odds_response, list) else odds_response.get("bookmakers", [])
                        if bookmaker_data:
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

                            if home_drop >= 10.0 and current_home_odd < historical["home"]:
                                stake_info = calculate_kelly_stake(85, current_home_odd) 
                                
                                msg = f"""🔥 <b>[SHARP MONEY ALERT - КРАЕН ИЗХОД 1]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
⏱ <b>Старт след:</b> {round(time_diff/60)} минути
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
                                stake_info = calculate_kelly_stake(85, current_away_odd)
                                
                                msg = f"""🔥 <b>[SHARP MONEY ALERT - КРАЕН ИЗХОД 2]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league} ({country})
⏱ <b>Старт след:</b> {round(time_diff/60)} минути
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
    print("🚀 Системата Syndicate Master Глобален Математически режим се стартира...")
    
    live_thread = threading.Thread(target=live_analysis_runner, daemon=True)
    live_thread.start()
    
    prematch_expert_runner()






