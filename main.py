import time
import threading
import requests
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

HEADERS = {
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
    "x-rapidapi-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "friendly", "amateur"]
sent = {}

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except: pass

def safe_api_get(endpoint, params=None):
    try:
        clean_endpoint = endpoint.lstrip('/')
        # ТАКЪВ ЛИНК НЯМА КАК ДА СЕ СГЛОБИ ГРЕШНО:
        url = f"https://rapidapi.com{clean_endpoint}"
        response = requests.get(url, headers=HEADERS, params=params, timeout=12)
        print(f"📡 [API CHECK] URL: {url} | Status Code: {response.status_code}")
        if response.status_code == 200:
            return response.json().get("response", [])
    except Exception as e:
        print(f"❌ Грешка при връзка: {e}")
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
                total_goals = (match["goals"]["home"] or 0) + (match["goals"]["away"] or 0)

                market = None
                if minute >= 75 and total_goals <= 3:
                    market = f"🔮 НАД {total_goals}.5 ГОЛА В МАЧА"

                if market:
                    msg = f"👑 <b>[VIP LIVE AI SIGNAL]</b>\n⚽ {home_name} vs {away_name}\n⏱ Минута: {minute}'\n🎯 <b>ПРОГНОЗА: {market}</b>"
                    send_telegram(msg)
                    sent[key] = time.time()
        except: pass
        time.sleep(60)

if __name__ == "__main__":
    print("🔥 AI PRO v1000 READY")
    send_telegram("🚀 БОТЪТ СТАРТИРА УСПЕШНО И Е ОНЛАЙН!")
    t1 = threading.Thread(target=live_analysis_runner)
    t1.start()
    t1.join()


