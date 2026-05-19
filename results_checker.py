# =========================================================
# PROFESSIONAL RESULTS CHECKER & DATASET GENERATOR
# AUTOMATIC ROI CALCULATION & ML FEEDBACK LOOP (results_checker.py)
# =========================================================

import json
import requests
import os
import time
from datetime import datetime
from config import API_KEY

HEADERS = {"x-apisports-key": API_KEY}
PICKS_FILE = "picks.json"
DATA_FILE = "dataset.json"

def load_json_file(filename):
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_json_file(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def check_results():
    picks = load_json_file(PICKS_FILE)
    dataset = load_json_file(DATA_FILE)
    updated = False

    for p in picks:
        if p.get("checked"):
            continue

        fid = p.get("fixture_id")
        if not fid:
            continue

        try:
            r = requests.get(
                f"https://api-sports.io{fid}",
                headers=HEADERS,
                timeout=15
            ).json()

            response_data = r.get("response", [])
            if not response_data:
                continue

            m = response_data[0]

            if m["fixture"]["status"]["short"] != "FT":
                continue

            gh = m["goals"]["home"] if m["goals"]["home"] is not None else 0
            ga = m["goals"]["away"] if m["goals"]["away"] is not None else 0
            total_goals = gh + ga

            pick = p.get("pick", "")
            win = False

            # ПРОВЕРКА НА НОВИТЕ ПАЗАРИ
            if "BTTS" in pick or "ДВАТА ОТБОРА" in pick:
                win = gh > 0 and ga > 0
            elif "Over 2.5" in pick or "НАД 2.5" in pick:
                win = total_goals > 2
            elif "НАД 1.5" in pick:
                win = total_goals > 1
            elif "ДОМАКИН" in pick or "NEXT GOAL HOME" in pick or "PREMATCH_1" in pick:
                win = gh > ga
            elif "ГОСТ" in pick or "NEXT GOAL AWAY" in pick or "PREMATCH_2" in pick:
                win = ga > gh
            elif "КОРНЕРА" in pick:
                # Взимане на корнери за крайна проверка
                corn_h = 0
                corn_a = 0
                for stat_type in m.get("statistics", []):
                    if stat_type.get("team", {}).get("id") == m["teams"]["home"]["id"]:
                        for s in stat_type.get("statistics", []):
                            if s["type"] == "Corner Kicks": corn_h = int(s["value"]) if s["value"] else 0
                    else:
                        for s in stat_type.get("statistics", []):
                            if s["type"] == "Corner Kicks": corn_a = int(s["value"]) if s["value"] else 0
                
                # Тъй като линията е азиатска, проверяваме дали общите корнери накрая са повече от тези при сигнала
                win = (corn_h + corn_a) > p.get("total_corners", 0)

            p["win"] = win
            p["checked"] = True
            updated = True

            # ХРАНЕНЕ НА МОДЕЛА (DATASET.JSON)
            ml_sample = {
                "shots_h": p.get("sh", 0),
                "shots_a": p.get("sa", 0),
                "att_h": p.get("ah", 0),
                "att_a": p.get("aa", 0),
                "goals": p.get("trigger_total_goals", 0),
                "btts": 1 if (gh > 0 and ga > 0) else 0,
                "over25": 1 if (total_goals > 2) else 0
            }
            dataset.append(ml_sample)

        except Exception as e:
            print(f"Error checking fixture {fid}: {e}")
            continue

    if updated:
        save_json_file(PICKS_FILE, picks)
        save_json_file(DATA_FILE, dataset)
        print("✅ Резултатите са обновени и записани за обучение на AI.")


def stats():
    picks = load_json_file(PICKS_FILE)

    total = 0
    wins = 0
    profit = 0

    for p in picks:
        if not p.get("checked"):
            continue

        total += 1
        stake = float(p.get("stake", 1.0))
        odds = float(p.get("odds", 2.0))

        if p.get("win"):
            wins += 1
            profit += stake * (odds - 1.0)
        else:
            profit -= stake

    if total == 0:
        print("\n📊 НЯМА НАЛИЧНИ ПРОВЕРЕНИ ЗАЛОЗИ ВСЕ ОЩЕ.")
        return

    roi = (profit / total)

    print("\n📊 ОФИЦИАЛНА ТИПСТЪРСКА СТАТИСТИКА")
    print(f"Общо залози:  {total}")
    print(f"Успеваемост:  {round(wins / total * 100, 1)}%")
    print(f"Чист профит:  {round(profit, 2)} единици")
    print(f"Текущ ROI:    {round(roi * 100, 1)}%")


if __name__ == "__main__":
    check_results()
    stats()

