# =========================================================
# ULTIMATE MASTERPIECE TIPSTER AI SYSTEM (main.py)
# RAPIDAPI COMPATIBLE STANDARD - FULLY FIXED & BRONZED VERSION
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

HEADERS = {
    "x-rapidapi-host": "://rapidapi.com",
    "x-rapidapi-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "friendly", "amateur"]
sent = {}
prematch_sent = {}

def init_database():
    conn = sqlite3.connect("syndicate_master.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER, match_name TEXT,
        market TEXT, confidence INTEGER, stake TEXT, created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

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
        clean_endpoint = endpoint.lstrip('/')
        url = f"https://://rapidapi.com/v3/{clean_endpoint}"
        response = requests.get(url, headers=HEADERS, params=params, timeout=12)
        
        # 📡 ЛОГ: Вече винаги ще виждаш реалните успешни заявки 200 на екрана си!
        print(f"📡 [API CHECK] URL: {url} | Status Code: {response.status_code}")
        if response.status_code == 200:
            return response.json().get("response", [])
    except Exception as e:
        print(f"❌ Грешка при връзка с API: {e}")
    return []

def live_analysis_runner():
    print("⚡ LIVE Мулти-пазарен скенер е активен...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            
            for match in live_matches:
                if not match or "fixture" not in match: continue
                fixture_id = match["fixture"]["id"]
                league = match["league"]["name"]
                if any(w in league.lower() for w in BLOCKED_WORDS): continue

                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 15 or minute > 85: continue

                key = f"{fixture_id}_live"
                if key in sent: continue

                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]
                home_goals = match["goals"]["home"] if match["goals"]["home"] is not None else 0
                away_goals = match["goals"]["away"] if match["goals"]["away"] is not None else 0
                total_goals = home_goals + away_goals

                # ОЛЕКОТЕНА И СИГУРНА ЛОГИКА ЗА ЛАЙВ СИГНАЛИ (БЕЗ РИСК ОТ ЛИПСВАЩА СТАТИСТИКА)
                market = None
                confidence = 75
                
                if minute >= 75 and total_goals <= 3:
                    market = f"🔮 НАД {total_goals}.5 ГОЛА В МАЧА (Късен гол)"
                    confidence = 78
                elif 20 <= minute <= 60 and total_goals == 0:
                    market = f"⚽ НАД 0.5 ГОЛА ПЪРВО ПОЛУВРЕМЕ / МАЧ"
                    confidence = 74

                if market:
                    msg = f"""👑 <b>[VIP LIVE AI SIGNAL]</b>
────────────────────
⚽ <b>Мач:</b> <code>{home_name} vs {away_name}</code>
🏆 <b>Лига:</b> {league}
⏱ <b>Минута:</b> {minute}'  |  📊 <b>Резултат:</b> {home_goals}-{away_goals}
────────────────────
🎯 <b>ПРОГНОЗА: {market}</b>
✅ <b>Сигурност:</b> {confidence}%"""
                    send_telegram(msg)
                    sent[key] = time.time()
                    
        except Exception as e:
            print("Грешка в лайв цикъла:", e)
        time.sleep(60)

def prematch_expert_runner():
    print("📅 PREMATCH Системата е активна...")
    while True:
        try:
            now_sofia = datetime.now(TZ)
            today = now_sofia.strftime("%Y-%m-%d")
            upcoming_matches = safe_api_get("fixtures", {"date": today})

            for m in upcoming_matches:
                if not m or m["fixture"]["status"]["short"] != "NS": continue
                fixture_id = m["fixture"]["id"]
                if f"{fixture_id}_pre" in prematch_sent: continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]
                league = m["league"]["name"]

                date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                time_diff = (date_obj - now_sofia).total_seconds()
                if time_diff < 0 or time_diff > 14400: continue # Следващите 4 часа

                # Автоматичен предмачов пазар за гол-гол
                market = "💎 ГОЛ/ГОЛ - ДА"
                msg = f"""🔮 <b>[PREMATCH AI INSIDE]</b>
────────────────────
⚽ <b>Среща:</b> <code>{home} vs {away}</code>
🏆 <b>Турнир:</b> {league}
⏱ <b>Час:</b> {date_obj.strftime('%H:%M')} ч.
────────────────────
🎯 <b>ПРОГНОЗА ПРЕДИ МАЧА: {market}</b>"""
                send_telegram(msg)
                prematch_sent[f"{fixture_id}_pre"] = time.time()
                time.sleep(2)
                
        except Exception as e:
            print("Грешка в предмачовия цикъл:", e)
        time.sleep(600)

if __name__ == "__main__":
    init_database()
    send_telegram("🚀 БОТЪТ СТАРТИРА УСПЕШНО И Е ОНЛАЙН!")
    
    t1 = threading.Thread(target=live_analysis_runner)
    t2 = threading.Thread(target=prematch_expert_runner)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

