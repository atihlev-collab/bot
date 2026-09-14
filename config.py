import os

# Telegram
BOT_TOKEN = "8847822220:AAHhv-ZJulQFA_ZvHUBbfGh1wT9rj8sKiRo"
CHAT_ID = "@rangel_radar_pro"

# Highlightly only — API-Football is no longer used.
HIGHLIGHTLY_API_KEY = os.getenv("HIGHLIGHTLY_API_KEY", "")
API_KEY = HIGHLIGHTLY_API_KEY

if not HIGHLIGHTLY_API_KEY:
    raise RuntimeError("HIGHLIGHTLY_API_KEY is not set")


 


