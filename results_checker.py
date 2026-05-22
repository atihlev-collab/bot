import json
import requests
import os
from datetime import datetime
from config import API_KEY

HEADERS = {"x-apisports-key": API_KEY}
PICKS_FILE = "picks.json"

def load_picks():
    if not os.path.exists(PICKS_FILE):
        return []
    return json.load(open(PICKS_FILE))


def save_picks(picks):
    json.dump(picks, open(PICKS_FILE, "w"), indent=2)


def check_results():

    picks = load_picks()
    updated = False

    for p in picks:

        if p.get("checked"):
            continue

        fid = p.get("fixture_id")
        if not fid:
            continue

        try:
            r = requests.get(
                f"https://v3.football.api-sports.io/fixtures?id={fid}",
                headers=HEADERS
            ).json()

            m = r["response"][0]

            if m["fixture"]["status"]["short"] != "FT":
                continue

            gh = m["goals"]["home"]
            ga = m["goals"]["away"]

            pick = p["pick"]

            win = False

            if pick == "BTTS":
                win = gh > 0 and ga > 0

            elif pick == "Over 2.5":
                win = (gh + ga) > 2

            p["win"] = win
            p["checked"] = True

            updated = True

        except:
            continue

    if updated:
        save_picks(picks)


def stats():

    picks = load_picks()

    total = 0
    wins = 0
    profit = 0

    for p in picks:
        if not p.get("checked"):
            continue

        total += 1

        stake = p.get("stake",1)
        odds = p.get("odds",2)

        if p.get("win"):
            wins += 1
            profit += stake * (odds - 1)
        else:
            profit -= stake

    if total == 0:
        return

    roi = (profit / total)

    print("\n📊 STATS")
    print(f"Bets: {total}")
    print(f"Winrate: {round(wins/total*100,1)}%")
    print(f"Profit: {round(profit,2)}")
    print(f"ROI: {round(roi,3)}")


if __name__ == "__main__":
    check_results()
    stats()
