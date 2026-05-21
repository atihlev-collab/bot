import requests
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from config import BOT_TOKEN, API_KEY, CHAT_ID

HEADERS = {
    "x-rapidapi-host": "://rapidapi.com",
    "x-rapidapi-key": API_KEY
}
TZ = ZoneInfo("Europe/Sofia")
bot = Bot(token=BOT_TOKEN)

def check_results():
    picks_file = "picks.json"
    dataset_file = "dataset.json"
    if not os.path.exists(picks_file): return
    try:
        with open(picks_file, "r") as f: picks = json.load(f)
        updated_picks = []
        dataset_updates = []
        
        for p in picks:
            if p.get("checked") == True:
                updated_picks.append(p)
                continue
            
            fid = p["fixture_id"]
            url = f"https://://rapidapi.com/v3/fixtures?id={fid}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            
            if r.status_code == 200 and r.json().get("response"):
                match_data = r.json()["response"][0]
                status = match_data["fixture"]["status"]["short"]
                
                if status in ["FT", "AET", "PEN"]:
                    fh = match_data["goals"]["home"]
                    fa = match_data["goals"]["away"]
                    p["final_home_goals"] = fh
                    p["final_away_goals"] = fa
                    p["checked"] = True
                    
                    # Логика за проверка на пазара
                    win = False
                    pick_text = p["pick"].lower()
                    if "гол/гол" in pick_text and fh > 0 and fa > 0: win = True
                    elif "над 2.5" in pick_text and (fh + fa) > 2.5: win = True
                    elif "над 1.5" in pick_text and (fh + fa) > 1.5: win = True
                    elif "домакин" in pick_text and fh > p.get("trigger_home_goals", 0): win = True
                    elif "гост" in pick_text and fa > p.get("trigger_away_goals", 0): win = True
                    
                    p["win"] = win
                    dataset_updates.append(p)
            updated_picks.append(p)
            
        with open(picks_file, "w") as f: json.dump(updated_picks, f, indent=2)
        
        if dataset_updates:
            curr_data = []
            if os.path.exists(dataset_file):
                with open(dataset_file, "r") as f: curr_data = json.load(f)
            curr_data.extend(dataset_updates)
            with open(dataset_file, "w") as f: json.dump(curr_data, f, indent=2)
            print(f"📊 [CHECKER] Успешно обновени {len(dataset_updates)} мача.")
    except: pass

if __name__ == "__main__":
    check_results()
