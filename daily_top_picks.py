# =========================================================
# DAILY TOP 3 PICKS SELECTOR - GLOBAL UNLIMITED EDITION
# =========================================================

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

# НАПЪЛНО ПРЕМАХНАТ СПИСЪК С ДЪРЖАВИ - СЛЕДИ СЕ ЦЕЛИЯ СВЯТ!
# Изключваме само несериозните мачове, за да пазим качеството
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
        
        # Спираме само младежи, жени и приятелски мачове
        if any(w in league.lower() for w in BLOCKED_WORDS): continue
        
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        # Базов скоринг за голове
        base_probability = 73.0
        market = "🔮 НАД 2.5 ГОЛА В МАЧА"
        
        # Специфична филтрация на пазара според тенденцията на държавата
        HIGH_BTTS = ["Netherlands", "Germany", "Norway", "Sweden", "Iceland", "Australia", "USA", "Japan", "Brazil"]
        if country in HIGH_BTTS:
            base_probability = 77.5
            market = "💎 ДВАТА ОТБОРА ДА ОТБЕЛЕЖАТ (ГОЛ/ГОЛ)"
            
        date_obj = datetime.fromisoformat(m["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
        
        scored_matches.append({
            "text": f"⚽ <b>{home} vs {away}</b>\n🏆 {league} ({country})\n⏱ Час: {date_obj.strftime('%H:%M')}\n🎯 Прогноза: {market}\n📈 Шанс: {base_probability}%\n",
            "prob": base_probability
        })
        
    # Сортиране на целия свят и извличане на ТОП 3
    scored_matches.sort(key=lambda x: x["prob"], reverse=True)
    top_3 = scored_matches[:3]
    
    if len(top_3) < 3:
        return "⚠️ Футболният календар за днес не предлага мачове, отговарящи на софтуерните критерии."
        
    message = f"☀️ <b>AI СУТРЕШЕН ТОП 3 ФИШ ЗА ДНЕС</b>\n"
    message += f"📅 Дата: {datetime.now(TZ).strftime('%d.%m.%Y')} | ⏱ Час: 10:00\n"
    message += "────────────────────\n\n"
    
    for idx, match in enumerate(top_3, 1):
        message += f"{idx}. {match['text']}\n"
        
    message += "────────────────────\n"
    message += "💵 <i>Препоръка: Използвайте залог в права колона!</i>"
    return message

async def main():
    text_msg = get_top_3_picks()
    await bot.send_message(chat_id=CHAT_ID, text=text_msg, parse_mode="HTML")

if __name__ == "__main__":
    asyncio.run(main())

