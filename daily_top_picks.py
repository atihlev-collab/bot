# =========================================================
# SYNDICATE MASTER - DAILY TOP PICKS (BALANCED PRO)
# TARGET: 1-3 TOP QUALITY DAILY ACCAS
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

def get_daily_picks():
    print("📅 Сканиране на дневния тираж за Топ Прогнози...")
    today = datetime.now().strftime("%Y-%m-%d")
    matches = requests.get(f"{BASE_URL}/fixtures", headers=HEADERS, params={"date": today}).json().get("response", [])
    
    selected_picks = []
    
    for m in matches:
        if m["fixture"]["status"]["short"] != "NS": continue
        league = m["league"]["name"]
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        fixture_id = m["fixture"]["id"]
        
        # Олекотен филтър по коефициенти за намиране на стойност (Value Фаворити)
        odds_res = requests.get(f"{BASE_URL}/odds", headers=HEADERS, params={"fixture": fixture_id, "bookmaker": 8, "bet": 1}).json().get("response", [])
        
        home_odd, away_odd = 0.0, 0.0
        try:
            if odds_res and len(odds_res) > 0:
                values = odds_res[0]["bookmakers"][0]["bets"][0]["values"]
                for v in values:
                    if v["value"] == "Home": home_odd = float(v["odd"])
                    if v["value"] == "Away": away_odd = float(v["odd"])
        except: pass

        # Балансирано условие: Търсим изявен, но резонен фаворит в мача (коефициент между 1.30 и 1.85)
        if 1.30 <= home_odd <= 1.85:
            selected_picks.append(f"⚽ {home} vs {away}\n🏆 {league}\n🎯 <b>Прогноза: ПОБЕДА ЗА ДОМАКИНА (1)</b> | Коеф: {home_odd}\n")
        elif 1.30 <= away_odd <= 1.85:
            selected_picks.append(f"⚽ {home} vs {away}\n🏆 {league}\n🎯 <b>Прогноза: ПОБЕДА ЗА ГОСТА (2)</b> | Коеф: {away_odd}\n")
            
        # Лимитираме до максимум 3-те най-добри мача за деня, за да няма спам
        if len(selected_picks) >= 3:
            break

    if selected_picks:
        msg = "☀️ <b>[AI ДНЕВЕН ТОП ФИШ]</b> ☀️\n\n" + "\n".join(selected_picks) + "\n💼 <i>Препоръчителен залог: 2% от банката</i>\n🤖 Syndicate Master Pro"
        send_telegram(msg)
    else:
        # Резервен вариант: Ако няма чисти фаворити, ботът автоматично пуска сигурна линия за голове, вместо да почива!
        msg = "☀️ <b>[AI ДНЕВЕН ТОП ФИШ]</b> ☀️\n\n🔥 Търсете пазара <b>НАД 1.5 ГОЛА</b> на сингъл или в права колона за дневните дербита от тиража!\n🤖 Система Синдикат"
        send_telegram(msg)

if __name__ == "__main__":
    get_daily_picks()
