import time
import sqlite3
import threading
import requests
import asyncio
import os
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

try:
    from ml_model import predict_btts, predict_over, train_model, load_model
except:
    pass

HEADERS = {
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
    "x-rapidapi-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

def send_telegram(message):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML"))
        loop.close()
    except: pass

def live_analysis_runner():
    print("⚡ LIVE Мулти-пазарен скенер е активен...")
    while True:
        try:
            # ТВЪРДО ЗАПИСАН ЧИСТ АДРЕС БЕЗ ФУНКЦИИ И СГЛОБЯВАНИЯ
            url = "https://rapidapi.com"
            response = requests.get(url, headers=HEADERS, params={"live": "all"}, timeout=10)
            
            print(f"📡 [API CHECK] URL: {url} | Status Code: {response.status_code}")
            
        except Exception as e:
            print(f"❌ Критична грешка при връзка с API: {e}")
        time.sleep(60)

if __name__ == "__main__":
    print("🔥 AI PRO v1000 READY")
    send_telegram("🚀 БОТЪТ СТАРТИРА УСПЕШНО И Е ОНЛАЙН!")
    
    t1 = threading.Thread(target=live_analysis_runner)
    t1.start()
    t1.join()













