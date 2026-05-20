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
    except: pass

def get_daily_picks():
    today = datetime.now().strftime("%Y-%m-%d")
    matches = requests.get(f"{BASE_URL}/fixtures", headers=HEADERS, params={"date": today}).json().get("response", [])
    selected_picks = []
    
    for m in matches:
        if m["fixture"]["status"]["short"] != "NS": continue
        league = m["league"]["name"]
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        
        # Задължително генериране на 1-3 топ мача за голове по подразбиране
        first_leagues = ["premier", "liga 1", "serie a", "division 1", "super", "eredivisie", "bundesliga"]
        if any(kw in league.lower() for kw in first_leagues):
            selected_picks.append(f"⚽ <b>{home} vs {away}</b>\n🏆 {league}\n🎯 Прогноза: <b>НАД 1.5 ГОЛА</b>\n")
        
        if len(selected_picks) >= 3: break

    if selected_picks:
        msg = "☀️ <b>[AI ДНЕВЕН ТОП ФИШ]</b> ☀️\n\n" + "\n".join(selected_picks) + "🤖 Syndicate Master Pro"
        send_telegram(msg)

if __name__ == "__main__":
    get_daily_picks()
