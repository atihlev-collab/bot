"""Main5 result-driven optimizer."""
import json, os

PICKS_FILE = "picks.json"
CONFIG_FILE = "auto_config.json"

DEFAULT_CONFIG = {
    "btts_threshold": 0.58,
    "over_threshold": 0.55,
    "min_score": 0.60,
    "bad_leagues": []
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f: cfg = json.load(f)
        out = dict(DEFAULT_CONFIG); out.update(cfg); return out
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def run_optimize():
    if not os.path.exists(PICKS_FILE): return False
    try:
        with open(PICKS_FILE, encoding="utf-8") as f: picks = json.load(f)
    except Exception: return False

    cfg = load_config(); markets = {}; leagues = {}
    checked = [p for p in picks if p.get("checked")]
    if len(checked) < 20: return False

    for p in checked:
        market = str(p.get("market") or p.get("pick") or "UNKNOWN")
        league = str(p.get("league") or "UNKNOWN")
        d = markets.setdefault(market, {"w":0,"t":0})
        l = leagues.setdefault(league, {"w":0,"t":0})
        d["t"] += 1; l["t"] += 1
        if p.get("win"): d["w"] += 1; l["w"] += 1

    winrate = sum(bool(p.get("win")) for p in checked) / len(checked)
    cfg["min_score"] = min(0.90, max(0.50, cfg["min_score"] + (0.02 if winrate < 0.50 else -0.005 if winrate > 0.62 else 0)))

    for market, d in markets.items():
        if d["t"] < 10: continue
        wr = d["w"] / d["t"]
        if wr < 0.50:
            if "BTTS" in market: cfg["btts_threshold"] = min(0.85, cfg["btts_threshold"] + 0.02)
            if "OVER" in market.upper(): cfg["over_threshold"] = min(0.85, cfg["over_threshold"] + 0.02)

    bad = set(cfg.get("bad_leagues", []))
    for league, d in leagues.items():
        wr = d["w"] / d["t"]
        if d["t"] >= 15 and wr < 0.45: bad.add(league)
        elif d["t"] >= 20 and wr >= 0.55: bad.discard(league)
    cfg["bad_leagues"] = sorted(bad)
    save_config(cfg)
    return True
