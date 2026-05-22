import json
import os

PICKS_FILE = "picks.json"
CONFIG_FILE = "auto_config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "btts_threshold": 0.58,
            "over_threshold": 0.55,
            "min_score": 0.60,
            "bad_leagues": []
        }
    return json.load(open(CONFIG_FILE))


def save_config(cfg):
    json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)


def run_optimize():

    if not os.path.exists(PICKS_FILE):
        return

    picks = json.load(open(PICKS_FILE))
    cfg = load_config()

    total, wins = 0, 0
    markets, leagues = {}, {}

    for p in picks:

        if not p.get("checked"):
            continue

        total += 1
        win = p.get("win", False)

        if win:
            wins += 1

        market = p.get("pick")
        league = p.get("league")

        markets.setdefault(market, {"w":0,"t":0})
        leagues.setdefault(league, {"w":0,"t":0})

        markets[market]["t"] += 1
        leagues[league]["t"] += 1

        if win:
            markets[market]["w"] += 1
            leagues[league]["w"] += 1

    if total < 20:
        return

    winrate = wins / total

    if winrate < 0.50:
        cfg["min_score"] += 0.02

    for m, d in markets.items():
        if d["t"] >= 5 and d["w"]/d["t"] < 0.5:
            if "BTTS" in m:
                cfg["btts_threshold"] += 0.02
            if "Over" in m:
                cfg["over_threshold"] += 0.02

    for l, d in leagues.items():
        if d["t"] >= 6 and d["w"]/d["t"] < 0.45:
            if l not in cfg["bad_leagues"]:
                cfg["bad_leagues"].append(l)

    save_config(cfg)
