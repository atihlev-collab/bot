# =========================================================
# SYNDICATE MASTER - NIGHT TOP PICKS (BALANCED PRO)
# TARGET: 1-3 TOP QUALITY NIGHT PICKS (LATAM/USA)
# =========================================================

import time
import requests
import asyncio
from datetime import datetime
from telegram import Bot
from config import BOT_TOKEN, API_KEY, CHAT_ID

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
bot = Bot(token=BOT_TOKEN)

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except Exception as e:
        print("Telegram Error:", e)

def get_night_picks():
    print("🌙 Сканиране на нощния тираж за Латам/USA мачове...")
    today = datetime.now().strftime("%Y-%m-%d")
    matches = requests.get(f"{BASE_URL}/fixtures", headers=HEADERS, params={"date": today}).json().get("response", [])
    
    selected_picks = []
    
    for m in matches:
        if m["fixture"]["status"]["short"] != "NS": continue
        league = m["league"]["name"]
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        # Проверяваме дали мачът е в нощните лиги (Бразилия, САЩ, Аржентина, Мексико, Колумбия)
        latam_leagues = ["MLS", "Serie A", "Liga Profesional", "Liga MX", "Primera A", "Copa Libertadores", "Copa Sudamericana"]
        if any(l in league for l in latam_leagues) or m["league"]["country"] in ["Brazil", "USA", "Argentina", "Mexico", "Colombia"]:
            
            # Тъй като нощните лиги са изключително резултатни, залагаме автоматично за голове
            selected_picks.append(f"🌙 {home} vs {away}\n🏆 {league}\n🎯 <b>Прогноза: НАД 1.5 ГОЛА В МАЧА</b>\n")
            
        if len(selected_picks) >= 3:
            break

    if selected_picks:
        msg = "🌙 <b>[AI НОЩЕН ТОП ФИШ]</b> 🌙\n\n" + "\n".join(selected_picks) + "\n💼 <i>Препоръчителен залог: 2.5% от банката</i>\n🤖 Syndicate Master Pro"
        send_telegram(msg)
    else:
        msg = "🌙 <b>[AI НОЩЕН ТОП ФИШ]</b> 🌙\n\n🔥 Пазар: <b>НАД 0.5 ГОЛА ПЪРВО ПОЛУВРЕМЕ</b> на живо за нощните мачове от Южна Америка!\n🤖 Система Синдикат"
        send_telegram(msg)

if __name__ == "__main__":
    get_night_picks()
