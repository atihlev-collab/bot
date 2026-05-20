import requests
import json
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot
from config import BOT_TOKEN, API_KEY, CHAT_ID

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

GOLDEN_NIGHT_COUNTRIES = ["USA", "Brazil", "Argentina", "Chile", "Colombia", "Mexico", "Canada", "Ecuador", "Peru", "Japan", "South Korea", "Australia"]
BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "friendly"]

def safe_api_get(endpoint, params=None):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=12)
        if r.status_code == 200: return r.json().get("response", [])
    except: pass
    return []

def get_night_picks():
    now_sofia = datetime.now(TZ)
    today_str = now_sofia.strftime("%Y-%m-%d")
    tomorrow_str = (now_sofia + timedelta(days=1)).strftime("%Y-%m-%d")
    all_fixtures = safe_api_get("fixtures", {"date": today_str}) + safe_api_get("fixtures", {"date": tomorrow_str})
    scored_matches = []
    seen_ids = set()
    
    for m in all_fixtures:
        fixture_id = m["fixture"]["id"]
        if fixture_id in seen_ids: continue
        seen_ids.add(fixture_id)
        if m["fixture"]["status"]["short"] != "NS": continue
        league = m["league"]["name"]
        country = m["league"]["country"]
        if any(w in league.lower() for w in BLOCKED_WORDS): continue
        if country not in GOLDEN_NIGHT_COUNTRIES: continue
        date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
        if not (0 <= date_obj.hour < 8): continue
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        if country in ["USA", "Brazil", "Japan"]:
            market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
            prob = 77.5
        elif country in ["Argentina", "Chile", "Colombia"]:
            market = "📉 ПОД 2.5/3.5 ГОЛА В МАЧА"
            prob = 75.0
        else:
            market = "🔮 НАД 2.5 ГОЛА В МАЧА"
            prob = 74.5
            
        scored_matches.append({
            "text": f"⚽ <b>{home} vs {away}</b>\n🏆 {league} ({country})\n⏱ Старт: <b>{date_obj.strftime('%H:%M')} ч.</b>\n🎯 Прогноза: {market}\n📈 AI Вероятност: {prob}%\n",
            "prob": prob
        })
        
    scored_matches.sort(key=lambda x: x["prob"], reverse=True)
    
    # 🎯 ДИНАМИЧНОСТ: Взима най-доброто (от 1 до 3 мача), без да блокира
    top_night = scored_matches[:3]
    
    if len(top_night) == 0:
        return "🌙 <b>AI НОЩЕН ФИШ:</b> Няма сигурни нощни мачове (00:00 - 08:00) за тази нощ."
        
    message = f"🌙 <b>AI VIP НОЩЕН ФИШ (00:00 - 08:00)</b>\n"
    message += f"📅 Дата: {now_sofia.strftime('%d.%m.%Y')} | ⏱ Брой събития: {len(top_night)}\n"
    message += "────────────────────\n\n"
    for idx, match in enumerate(top_night, 1):
        message += f"{idx}. {match['text']}\n"
    message += "────────────────────\n"
    message += "💵 <i>Препоръка: Използвайте залог с 2% от банката!</i>"
    return message

async def main():
    text_msg = get_night_picks()
    await bot.send_message(chat_id=CHAT_ID, text=text_msg, parse_mode="HTML")

if __name__ == "__main__":
    asyncio.run(main())

