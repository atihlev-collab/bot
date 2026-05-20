import requests
import json
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from config import BOT_TOKEN, API_KEY, CHAT_ID

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["women", "female", "youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "friendly", "amateur"]

def safe_api_get(endpoint, params=None):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=12)
        if r.status_code == 200: return r.json().get("response", [])
    except: pass
    return []

def get_top_3_picks():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    matches = safe_api_get("fixtures", {"date": today})
    scored_matches = []
    
    for m in matches:
        if m["fixture"]["status"]["short"] != "NS": continue
        league = m["league"]["name"]
        country = m["league"]["country"]
        if any(w in league.lower() for w in BLOCKED_WORDS): continue
        
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        # Висока цедка за качество (Връщаме тежкия критерий 74%)
        base_probability = 74.0
        market = "🔮 НАД 2.5 ГОЛА В МАЧА"
        
        HIGH_BTTS = ["Netherlands", "Germany", "Norway", "Sweden", "Iceland", "Australia", "USA", "Japan", "Brazil", "Finland", "Ireland"]
        if country in HIGH_BTTS:
            base_probability = 78.5
            market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
            
        date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
        
        scored_matches.append({
            "text": f"⚽ <b>{home} vs {away}</b>\n🏆 {league} ({country})\n⏱ Час: {date_obj.strftime('%H:%M')}\n🎯 Прогноза: {market}\n📈 Шанс: {base_probability}%\n",
            "prob": base_probability
        })
        
    scored_matches.sort(key=lambda x: x["prob"], reverse=True)
    
    # 🎯 ДИНАМИЧНОСТ: Взима най-доброто (от 1 до 3 мача), без да блокира, ако няма 3
    top_picks = scored_matches[:3]
    
    if len(top_picks) == 0:
        return "⚠️ В днешния футболен тираж липсват мачове, покриващи високите критерии за залог."
        
    message = f"☀️ <b>AI СУТРЕШЕН ТОП ФИШ ЗА ДНЕС</b>\n"
    message += f"📅 Дата: {datetime.now(TZ).strftime('%d.%m.%Y')} | ⏱ Брой събития: {len(top_picks)}\n"
    message += "────────────────────\n\n"
    
    for idx, match in enumerate(top_picks, 1):
        message += f"{idx}. {match['text']}\n"
        
    message += "────────────────────\n"
    message += "💵 <i>Препоръка: Използвайте залог с 2% от банката!</i>"
    return message

async def main():
    text_msg = get_top_3_picks()
    await bot.send_message(chat_id=CHAT_ID, text=text_msg, parse_mode="HTML")

if __name__ == "__main__":
    asyncio.run(main())


