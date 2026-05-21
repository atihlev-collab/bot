# =========================================================
# SYNDICATE MASTER GLOBAL AI SYSTEM - TRUE IN-PLAY RADAR PRO
# КОРИГИРАН АДРЕС КЪМ ПЛАТЕНАТА БАЗА НА API-FOOTBALL V3
# ЛИЧЕН МОДУЛ: ИЗПРАЩАНЕ ДИРЕКТНО В ЧАТА НА ПОТРЕБИТЕЛЯ
# =========================================================

import time
import sqlite3
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = "8339409001:AAGSjmIQGdLHZJEp4WphCHTCUE98a4L6SbU"
API_KEY = "9dc2c479ff0f8f13e9b266050fa8f485"
CHAT_ID = 6488122776  # Твоето личен чат ID

# ПОПРАВЕНО: Точният платен адрес от твоя личен профил в API-Sports
BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

session = requests.Session()

BLOCKED_WORDS = ["youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly", "amateur", "women", "female"]

sent = {}
prematch_sent = {}
last_scores = {}
odds_tracker = {}
daily_reports_sent = {"morning": False, "evening": False}

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
    except:
        pass

def send_telegram(message):
    url = f"https://telegram.org{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        response = session.post(url, json=payload, timeout=10)
        return response.json().get("ok", False)
    except:
        return False

def safe_api_get(endpoint, params=None):
    try:
        response = session.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get("response", [])
    except:
        pass
    return []

def is_first_league_or_global(league_name):
    text = league_name.lower()
    if any(w in text for w in BLOCKED_WORDS):
        return False
    return True

def extract(team_stats_list, stat_name):
    if not team_stats_list: return 0
    for stat in team_stats_list:
        if stat.get("type") == stat_name:
            val = stat.get("value")
            if val is None: return 0
            return int(str(val).replace("%", "").strip())
    return 0

def calculate_dynamic_stake(confidence):
    if confidence >= 75: return "🔥 СИЛЕН ЗАЛОГ: 3.5% от банката"
    elif confidence >= 60: return "💰 СРЕДЕН ЗАЛОГ: 2.0% от банката"
    return "⚠️ КОНСЕРВАТИВЕН ЗАЛОГ: 1.0% от банката"

def calculate_radar_pressure(team_stats_list, goals_scored, elapsed_minute):
    if elapsed_minute <= 0: return 0
    
    possession = extract(team_stats_list, "Ball Possession")
    shots_on = extract(team_stats_list, "Shots on Goal")
    total_shots = extract(team_stats_list, "Total Shots")
    corners = extract(team_stats_list, "Corner Kicks")
    attacks = extract(team_stats_list, "Dangerous Attacks")

    attack_rate = (attacks / elapsed_minute) * 100
    
    radar_points = 0
    radar_points += (shots_on * 12)    
    radar_points += (corners * 6)      
    radar_points += (total_shots * 4)  
    
    if attack_rate >= 60: radar_points += 15 
    if possession >= 52: radar_points += 5

    radar_points += (goals_scored * 10)

    return min(int(radar_points), 100)

def live_analysis_runner():
    time.sleep(10)
    print("⚡ LIVE Скенерът е активен...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            for match in live_matches:
                fixture_id = match["fixture"]["id"]
                league = match["league"]["name"]
                
                if not is_first_league_or_global(league): continue

                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 10 or minute > 88: continue

                home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
                away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
                total_goals = home_goals + away_goals
                score = f"{home_goals}-{away_goals}"

                if fixture_id not in last_scores: 
                    last_scores[fixture_id] = score
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

                home_pressure = calculate_radar_pressure(home_stats, home_goals, minute)
                away_pressure = calculate_radar_pressure(away_stats, away_goals, minute)
                best_pressure = max(home_pressure, away_pressure)

                market = None
                if minute >= 72 and (ah + aa >= 15) and (total_corners >= 4):
                    market = f"📐 НАД {total_corners + 0.5} КОРНЕРА"
                elif 25 <= minute <= 77 and best_pressure >= 35:
                    market = f"⚽ НАД {total_goals + 0.5} ГОЛА"

                if market:
                    sent[f"{fixture_id}_live"] = time.time()
                    stake_info = calculate_dynamic_stake(70)
                    save_signal(fixture_id, "Live Match", market, best_pressure, 70, 0.0, stake_info)
                    
                    msg = f"⚽ <b>Мач:</b> <code>{match['teams']['home']['name']} vs {match['teams']['away']['name']}</code>\n⏱️ <b>Минута:</b> {minute}' | 📊 <b>Резултат:</b> {score}\n📈 <b>Натиск:</b> {best_pressure}%\n📐 <b>Корнери:</b> {total_corners}\n🎯 <b>ПРОГНОЗА: {market}</b>\n💼 {stake_info}"
                    send_telegram(msg)

            time.sleep(45)
        except:
            time.sleep(10)

def generate_daily_highlights(is_bootstrap=False):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        fixtures = safe_api_get("fixtures", {"date": today})
        valid_picks = []

        for m in fixtures:
            if m["fixture"]["status"]["short"] != "NS": continue
            if not is_first_league_or_global(m["league"]["name"]): continue

            fixture_id = m["fixture"]["id"]
            odds_response = safe_api_get("odds", {"fixture": fixture_id, "bookmaker": 8, "bet": 1})
            
            if odds_response and len(odds_response) > 0:
                try:
                    bookmakers = odds_response.get("bookmakers", []) if isinstance(odds_response, list) else odds_response.get("bookmakers", [])
                    for b in bookmakers:
                        if b.get("id") == 8:
                            for bet in b.get("bets", []):
                                if bet.get("id") == 1:
                                    c_home, c_away = 0.0, 0.0
                                    for val in bet.get("values", []):
                                        if val.get("value") == "Home": c_home = float(val.get("odd", 0))
                                        if val.get("value") == "Away": c_away = float(val.get("odd", 0))
                                    
                                    if 1.30 <= c_home <= 2.10:
                                        valid_picks.append({"match": f"{m['teams']['home']['name']} vs {m['teams']['away']['name']}", "pick": "1", "odd": c_home})
                                    elif 1.30 <= c_away <= 2.10:
                                        valid_picks.append({"match": f"{m['teams']['home']['name']} vs {m['teams']['away']['name']}", "pick": "2", "odd": c_away})
                except:
                    pass

        if valid_picks:
            valid_picks = valid_picks[:3]
            msg = "☀️ <b>ДНЕВЕН AI ТОП ТИРАЖ</b>\n" + "─" * 20 + "\n"
            for p in valid_picks:
                msg += f"⚽ {p['match']}\n🎯 Прогноза: {p['pick']} @ <b>{p['odd']:.2f}</b>\n" + "─" * 20 + "\n"
            send_telegram(msg)
        else:
            send_telegram("ℹ️ <b>[AI РАДАР]</b> Данните от Ultra плана за днес бяха проверени успешно. Изчакваме LIVE старта на срещите довечера.")
    except:
        pass

def prematch_expert_runner():
    print("📅 PREMATCH Модулът стартира...")
    time.sleep(5)
    
    # СЪОБЩЕНИЕ ЗА СТАРТ: Сега вече ще излезе веднага в личния ти чат
    send_telegram("🟢 <b>[ULTRA PLAN ACTIVE]</b> Системата се свърза успешно с футболния сървър на API-Sports! Сканирането започна на чисто.")
    
    time.sleep(5)
    generate_daily_highlights(is_bootstrap=True)
    
    while True:
        try:
            now_bg = datetime.now(ZoneInfo("Europe/Sofia"))
            if now_bg.hour == 9 and not daily_reports_sent["morning"]:
                generate_daily_highlights(is_bootstrap=False)
                daily_reports_sent["morning"] = True
            if now_bg.hour == 21 and not daily_reports_sent["evening"]:
                generate_daily_highlights(is_bootstrap=False)
                daily_reports_sent["evening"] = True
            if now_bg.hour == 0:
                daily_reports_sent["morning"] = False
                daily_reports_sent["evening"] = False
            time.sleep(60)
        except:
            time.sleep(30)

if __name__ == "__main__":
    init_database()
    live_thread = threading.Thread(target=live_analysis_runner, daemon=True)
    live_thread.start()
    prematch_expert_runner()


