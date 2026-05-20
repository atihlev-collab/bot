import time
import sqlite3
import threading
import requests
import asyncio
import os
from datetime import datetime
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

BASE_URL = "https://api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
bot = Bot(token=BOT_TOKEN)

BLOCKED_WORDS = ["youth", "u17", "u18", "u19", "u20", "u21", "u23", "reserve", "reserves", "friendly", "amateur", "women", "female"]

sent = {}
last_scores = {}

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
        response = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=10)
        if response.status_code == 200: return response.json().get("response", [])
    except: pass
    return []

def live_analysis_runner():
    print("⚡ LIVE Скенерът работи стабилно...")
    while True:
        try:
            live_matches = safe_api_get("fixtures", {"live": "all"})
            for match in live_matches:
                fixture_id = match["fixture"]["id"]
                minute = match["fixture"]["status"]["elapsed"]
                if minute is None or minute < 15 or minute > 85: continue

                home_name = match["teams"]["home"]["name"]
                away_name = match["teams"]["away"]["name"]
                
                if fixture_id not in sent:
                    sent[fixture_id] = True
                    msg = f"⚽ <b>Мач на живо:</b> {home_name} vs {away_name}\n⏱ <b>Минута:</b> {minute}'"
                    send_telegram(msg)
            time.sleep(60)
        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    print("🚀 Ботът стартира успешно в защитен режим...")
    live_analysis_runner()










