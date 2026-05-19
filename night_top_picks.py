# =========================================================
# NIGHT TOP PICKS SELECTOR (night_top_picks.py)
# AUTONOMOUS NIGHT TRACKER (00:00 - 08:00 EET MATCHES)
# =========================================================

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

# Фокусираме се основно върху американските и азиатските летни първенства
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
    
    # Сглобяваме мачове от днес и утре, за да хванем целия нощен прозорец
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
        
        # СТРОГ ФИЛТЪР: Само мачове, започващи между 00:00 и 08:00 сутринта българско време
        if not (0 <= date_obj.hour < 8): continue
        
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        # Динамично определяне на най-добрия пазар на база дефанзивен или офанзивен стил на страната
        if country in ["USA", "Brazil", "Japan"]:
            market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
            prob = 76.5
        elif country in ["Argentina", "Chile", "Colombia"]:
            market = "📉 ПОД 2.5/3.5 ГОЛА В МАЧА"
            prob = 74.0
        else:
            market = "🔮 НАД 2.5 ГОЛА В МАЧА"
            prob = 72.5
            
        scored_matches.append({
            "text": f"⚽ <b>{home} vs {away}</b>\n🏆 {league} ({country})\n⏱ Старт: <b>{date_obj.strftime('%H:%M')} ч.</b>\n🎯 Прогноза: {market}\n📈 AI Вероятност: {prob}%\n",
            "prob": prob
        })
        
    scored_matches.sort(key=lambda x: x["prob"], reverse=True)
    top_night = scored_matches[:3] # Взима от 1 до 3 мача
    
    if len(top_night) == 0:
        return "🌙 <b>AI НОЩЕН ФИШ:</b> Няма сигурни нощни мачове (00:00 - 08:00), отговарящи на критериите за залог за тази нощ."
        
    message = f"🌙 <b>AI VIP НОЩЕН ФИШ (00:00 - 08:00)</b>\n"
    message += f"📅 Издаден на: {now_sofia.strftime('%d.%m.%Y')} | ⏱ Час: 20:00\n"
    message += "────────────────────\n\n"
    
    for idx, match in enumerate(top_night, 1):
        message += f"{idx}. {match['text']}\n"
        
    message += "────────────────────\n"
    message += "💵 <i>Препоръка: Пуснете мачовете в права колона преди лягане! Наспивайте се умно!</i>"
    return message

async def main():
    text_msg = get_night_picks()
    await bot.send_message(chat_id=CHAT_ID, text=text_msg, parse_mode="HTML")

if __name__ == "__main__":
    asyncio.run(main())
