"""Main5 result checker. Supports core prematch markets and builders."""
import json, os, requests
from config import API_KEY

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
PICKS_FILE = "picks.json"

def load_picks():
    if not os.path.exists(PICKS_FILE): return []
    try:
        with open(PICKS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return []

def save_picks(picks):
    with open(PICKS_FILE, "w", encoding="utf-8") as f: json.dump(picks, f, indent=2, ensure_ascii=False)

def _settle(market, pick, gh, ga):
    total = gh + ga
    s = str(pick or market or "").upper()
    if "BTTS" in s: return gh > 0 and ga > 0
    if "OVER 3.5" in s: return total >= 4
    if "OVER 2.5" in s: return total >= 3
    if "OVER 1.5" in s: return total >= 2
    if "UNDER 3.5" in s: return total <= 3
    if "UNDER 2.5" in s: return total <= 2
    if "UNDER 1.5" in s: return total <= 1
    if "HOME OVER 1.5" in s: return gh >= 2
    if "AWAY OVER 1.5" in s: return ga >= 2
    return None

def check_results():
    picks = load_picks(); updated = False
    for p in picks:
        if p.get("checked") or not p.get("fixture_id"): continue
        try:
            r = requests.get(f"{BASE_URL}/fixtures?id={p['fixture_id']}", headers=HEADERS, timeout=15).json()
            resp = r.get("response") or []
            if not resp or resp[0]["fixture"]["status"]["short"] != "FT": continue
            m = resp[0]; gh = int(m["goals"].get("home") or 0); ga = int(m["goals"].get("away") or 0)
            result = _settle(p.get("market"), p.get("pick"), gh, ga)
            if result is None: continue
            p["win"] = bool(result); p["checked"] = True; updated = True
        except Exception: continue
    if updated: save_picks(picks)
    return updated

def stats():
    picks = [p for p in load_picks() if p.get("checked")]
    if not picks: return {}
    stake_total = sum(float(p.get("stake", 1) or 0) for p in picks)
    profit = 0.0
    wins = 0
    for p in picks:
        stake = float(p.get("stake", 1) or 0); odds = float(p.get("odds", 2) or 2)
        if p.get("win"): wins += 1; profit += stake * (odds - 1)
        else: profit -= stake
    return {"bets":len(picks), "wins":wins, "winrate":wins/len(picks), "profit":profit,
            "roi": profit/stake_total if stake_total else 0.0}

if __name__ == "__main__":
    check_results(); print(stats())

