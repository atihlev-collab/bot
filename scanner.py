# BUILD: HIGHLIGHTLY-DAILY-STATISTICAL-SCANNER-FINAL-20260917
# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs once per day at/after 10:30 Bulgaria time.
# Fixture window: 12:00 BG -> next day 12:00 BG.
# =========================================================

import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import API_KEY, CHAT_ID, HIGHLIGHTLY_API_KEY
import threading

# Backward-compatibility alias: older deployments referenced HIGHLIGHTLY.
# Keep the canonical key name HIGHLIGHTLY_API_KEY everywhere else.
HIGHLIGHTLY = HIGHLIGHTLY_API_KEY

BASE_URL = "https://soccer.highlightly.net"
HEADERS = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY, "x-rapidapi-host": "football-highlights-api.p.rapidapi.com"}
TZ = ZoneInfo("Europe/Sofia")
DB_FILE = "v3_ai.db"
HISTORY_GAMES = None
MAX_WORKERS = 8
_SCAN_FIXTURE_STATS = {}
_SCAN_HISTORY = {}
_API_LOCK = threading.Lock()
_LAST_API_CALL = 0.0
_API_MIN_INTERVAL = 0.12

class APIQuotaExceeded(RuntimeError):
    """Highlightly daily quota was exhausted; do not make more API calls."""
    def __init__(self, provider):
        self.provider = provider
        super().__init__(f"HIGHLIGHTLY {provider} API quota exceeded (HTTP 429)")


def _quota_lock_table():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_quota_locks (
            provider TEXT PRIMARY KEY,
            locked_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def _quota_is_locked(provider):
    _quota_lock_table()
    conn = _db()
    row = conn.execute("SELECT locked_at FROM api_quota_locks WHERE provider=?", (provider,)).fetchone()
    if not row:
        conn.close()
        return False
    try:
        locked = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        locked_day = locked.astimezone(TZ).date()
    except Exception:
        locked_day = datetime.now(TZ).date()
    today = datetime.now(TZ).date()
    if locked_day != today:
        conn.execute("DELETE FROM api_quota_locks WHERE provider=?", (provider,))
        conn.commit()
        conn.close()
        return False
    conn.close()
    return True

def _set_quota_lock(provider):
    _quota_lock_table()
    conn = _db()
    conn.execute("INSERT OR REPLACE INTO api_quota_locks(provider, locked_at) VALUES (?, ?)",
                 (provider, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def _clear_quota_lock(provider):
    _quota_lock_table()
    conn = _db()
    conn.execute("DELETE FROM api_quota_locks WHERE provider=?", (provider,))
    conn.commit()
    conn.close()


def _api(endpoint, params=None, timeout=25):
    """Single Highlightly request. 429 permanently locks football for this process/day.
    No retries are made: retrying a quota error only wastes calls and cannot restore quota.
    """
    global _LAST_API_CALL
    if _quota_is_locked("football"):
        raise APIQuotaExceeded("football")

    with _API_LOCK:
        wait = _API_MIN_INTERVAL - (time.monotonic() - _LAST_API_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_API_CALL = time.monotonic()

    try:
        r = requests.get(
            f"{BASE_URL}/{endpoint}",
            headers=HEADERS,
            params=params or {},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print("SCANNER REQUEST ERROR:", endpoint, repr(exc))
        return None

    remaining = r.headers.get("x-ratelimit-requests-remaining")
    limit = r.headers.get("x-ratelimit-requests-limit")
    print(f"FOOTBALL API: {endpoint} status={r.status_code} remaining={remaining} limit={limit}")

    if r.status_code == 429:
        _set_quota_lock("football")
        print("HIGHLIGHTLY FOOTBALL API LIMIT: 429 — LOCKED; no further football requests.")
        raise APIQuotaExceeded("football")

    if r.status_code >= 400:
        print("SCANNER API ERROR:", endpoint, r.status_code, r.text[:300])
        return None

    try:
        payload = r.json()
    except ValueError:
        print("SCANNER API ERROR: invalid JSON:", endpoint)
        return None

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if payload.get("errors"):
            print("SCANNER API ERROR:", endpoint, payload.get("errors"))
            return None
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    print("SCANNER API ERROR: unexpected response type", endpoint, type(payload).__name__)
    return None

def _db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_scanner_db():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_scanner_runs (
            run_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scanner_fixture_stats (
            fixture_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scanner_team_season_history (
            team_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (team_id, season)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scanner_team_history_cache (
            team_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            league_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (team_id, season, league_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scanner_sport_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def already_ran(run_key):
    init_scanner_db()
    conn = _db()
    row = conn.execute(
        "SELECT 1 FROM daily_scanner_runs WHERE run_key=?",
        (run_key,),
    ).fetchone()
    conn.close()
    return row is not None


def mark_ran(run_key):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO daily_scanner_runs(run_key, created_at) VALUES (?, ?)",
        (run_key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _norm(s):
    return " ".join(str(s or "").strip().lower().replace("_", " ").split())


def _safe_float(v):
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace("%", "").strip()
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_match(m):
    if not isinstance(m, dict):
        return None
    home = m.get("homeTeam") or {}
    away = m.get("awayTeam") or {}
    league = m.get("league") or {}
    country = m.get("country") or {}
    state = m.get("state") or {}
    score = state.get("score") or {}
    current = score.get("current")
    hs = aw = 0
    if isinstance(current, str) and "-" in current:
        try:
            hs, aw = [int(x.strip()) for x in current.split("-", 1)]
        except Exception:
            pass
    elif isinstance(current, dict):
        hs = int(current.get("home") or current.get("homeTeam") or 0)
        aw = int(current.get("away") or current.get("awayTeam") or 0)
    desc = str(state.get("description") or "")
    up = desc.upper()
    if any(x in up for x in ("FIRST HALF", "SECOND HALF", "IN PROGRESS", "HALF TIME", "BREAK", "EXTRA TIME", "PENALT")):
        short = "LIVE"
    elif "FINISHED" in up or "FINAL" in up:
        short = "FT"
    elif "POSTPONED" in up:
        short = "PST"
    elif "CANCEL" in up:
        short = "CANC"
    else:
        short = "NS"
    return {
        "fixture": {"id": m.get("id"), "date": m.get("date"), "status": {"short": short, "long": desc, "elapsed": state.get("clock")}},
        "league": {"id": league.get("id"), "name": league.get("name"), "season": league.get("season"), "country": country.get("name") or "", "type": league.get("type")},
        "teams": {"home": {"id": home.get("id"), "name": home.get("name"), "logo": home.get("logo")}, "away": {"id": away.get("id"), "name": away.get("name"), "logo": away.get("logo")}},
        "goals": {"home": hs, "away": aw},
    }


def get_fixtures_for_window(start_bg, end_bg):
    days = []
    d = start_bg.date()
    while d <= end_bg.date():
        days.append(d)
        d += timedelta(days=1)
    all_matches, seen = [], set()
    for day in days:
        rows = _api("matches", {"date": day.isoformat(), "timezone": "Europe/Sofia", "limit": 100})
        for raw in rows if isinstance(rows, list) else []:
            m = _normalize_match(raw)
            if not m:
                continue
            fid = (m.get("fixture") or {}).get("id")
            dt_raw = (m.get("fixture") or {}).get("date")
            if not fid or fid in seen or not dt_raw:
                continue
            try:
                dt_utc = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
                dt_bg = dt_utc.astimezone(TZ)
            except Exception:
                continue
            if dt_bg < start_bg or dt_bg >= end_bg or dt_utc <= datetime.now(timezone.utc):
                continue
            # GLOBAL BLOCK: Russia + Belarus are never scanned.
            if _is_blocked_fixture(m):
                continue
            seen.add(fid)
            all_matches.append(m)
    all_matches.sort(key=lambda x: (x.get("fixture") or {}).get("date", ""))
    return all_matches

def get_team_history(team_id, season, league_id=None):
    team_id, season, league_id = int(team_id), int(season), int(league_id or 0)
    key = (team_id, season, league_id)
    if key in _SCAN_HISTORY:
        return _SCAN_HISTORY[key]

    import json

    def clean(rows):
        out, seen = [], set()
        for f in rows if isinstance(rows, list) else []:
            fid = (f.get("fixture") or {}).get("id")
            lg = f.get("league") or {}
            status = (f.get("fixture") or {}).get("status", {}).get("short", "")
            if not fid or fid in seen or int(lg.get("season") or 0) != season or status not in {"FT", "AET", "PEN"}:
                continue
            name = str(lg.get("name") or "").casefold()
            typ = str(lg.get("type") or "").casefold()
            if typ == "friendly" or "friend" in name:
                continue
            if league_id and int(lg.get("id") or 0) != league_id:
                continue
            seen.add(fid); out.append(f)
        out.sort(key=lambda f: (f.get("fixture") or {}).get("date", ""), reverse=True)
        return out[:12]

    import json
    conn = _db()
    row = conn.execute(
        "SELECT data FROM scanner_team_history_cache WHERE team_id=? AND season=? AND league_id=?",
        (team_id, season, league_id),
    ).fetchone()
    conn.close()
    if row:
        try:
            cached = clean(json.loads(row[0]))
            _SCAN_HISTORY[key] = cached
            print("HISTORY CACHE:", team_id, "matches=", len(cached))
            return cached
        except Exception:
            pass

    def fetch_team_matches(extra):
        rows = _api("matches", {**extra, "limit": 100})
        return [m for x in (rows if isinstance(rows, list) else []) if (m := _normalize_match(x))]

    primary = []
    if league_id:
        primary = fetch_team_matches({"leagueId": league_id, "season": season, "homeTeamId": team_id})
        primary += fetch_team_matches({"leagueId": league_id, "season": season, "awayTeamId": team_id})
    primary = clean(primary)

    if len(primary) < 3:
        fallback = fetch_team_matches({"season": season, "homeTeamId": team_id})
        fallback += fetch_team_matches({"season": season, "awayTeamId": team_id})
        primary = clean(primary + fallback)

    try:
        conn = _db()
        conn.execute(
            "INSERT OR REPLACE INTO scanner_team_history_cache(team_id, season, league_id, data, updated_at) VALUES (?, ?, ?, ?, ?)",
            (team_id, season, league_id, json.dumps(primary), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit(); conn.close()
    except Exception as exc:
        print("HISTORY CACHE WRITE ERROR:", repr(exc))

    _SCAN_HISTORY[key] = primary
    print("HISTORY:", team_id, "matches=", len(primary))
    return primary


def _read_cached_stat(fixture_id):
    conn = _db()
    row = conn.execute(
        "SELECT data FROM scanner_fixture_stats WHERE fixture_id=?",
        (fixture_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        import json
        return json.loads(row[0])
    except Exception:
        return None


def _write_cached_stat(fixture_id, data):
    import json
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO scanner_fixture_stats(fixture_id, data, updated_at) VALUES (?, ?, ?)",
        (fixture_id, json.dumps(data), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _fixture_market_values(fixture):
    """
    Extract per-team match statistics from the enriched /fixtures?ids response.
    API-Football documents that the ids form can return fixture data enriched
    with statistics; statistics themselves are only used when present.
    """
    fid=(fixture.get("fixture") or {}).get("id")
    out={}
    for block in fixture.get("statistics") or []:
        tid=(block.get("team") or {}).get("id")
        if not tid:
            continue
        vals={}
        for item in block.get("statistics") or []:
            typ=(item.get("type") or "").strip().lower()
            val=item.get("value")
            if isinstance(val,str):
                val=val.replace("%","").strip()
            try:
                val=float(val) if val is not None else None
            except (TypeError,ValueError):
                val=None
            if val is not None:
                vals[typ]=val

        if vals:
            out[int(tid)]=vals

    # Goals are always available in the fixture itself.
    home_id=(fixture.get("teams") or {}).get("home",{}).get("id")
    away_id=(fixture.get("teams") or {}).get("away",{}).get("id")
    goals=fixture.get("goals") or {}
    if home_id:
        out.setdefault(int(home_id),{})["goals_scored"]=float(goals.get("home") or 0)
        out.setdefault(int(home_id),{})["goals_conceded"]=float(goals.get("away") or 0)
    if away_id:
        out.setdefault(int(away_id),{})["goals_scored"]=float(goals.get("away") or 0)
        out.setdefault(int(away_id),{})["goals_conceded"]=float(goals.get("home") or 0)

    return fid,out


def load_historical_statistics(all_histories):
    fixtures_by_id = {}
    for history in all_histories:
        for f in history:
            fid = (f.get("fixture") or {}).get("id")
            if fid:
                fixtures_by_id[int(fid)] = f

    loaded = {}
    missing = []
    for fid, base_fixture in fixtures_by_id.items():
        cached = _read_cached_stat(fid)
        if cached is not None:
            loaded[fid] = cached
        else:
            missing.append((fid, base_fixture))

    print(f"SCANNER STAT CACHE: {len(loaded)} cached / {len(missing)} API")
    for fid, base_fixture in missing:
        rows = _api(f"statistics/{fid}", {})
        fake = dict(base_fixture)
        fake["statistics"] = rows if isinstance(rows, list) else []
        _, data = _fixture_market_values(fake)
        loaded[fid] = data
        _write_cached_stat(fid, data)

    _SCAN_FIXTURE_STATS.clear(); _SCAN_FIXTURE_STATS.update(loaded)
    return loaded


def build_profiles_from_histories(histories_by_key, stats_by_fixture):
    profiles={}

    for (team_id,league_id,season), history in histories_by_key.items():
        sums={
            "corners":0.0,
            "cards":0.0,
            "shots":0.0,
            "goals_scored":0.0,
            "goals_conceded":0.0,
        }
        counts={k:0 for k in sums}

        for f in history:
            fid=(f.get("fixture") or {}).get("id")
            data=stats_by_fixture.get(int(fid),{}).get(int(team_id),{})
            goals=(f.get("goals") or {})
            home_id=(f.get("teams") or {}).get("home",{}).get("id")

            if "goals_scored" not in data:
                data["goals_scored"]=float(
                    goals.get("home") if int(home_id or -1)==int(team_id)
                    else goals.get("away") or 0
                )
            if "goals_conceded" not in data:
                data["goals_conceded"]=float(
                    goals.get("away") if int(home_id or -1)==int(team_id)
                    else goals.get("home") or 0
                )

            mappings={
                "corners":"corner kicks",
                "shots":"total shots",
                "cards":"yellow cards",
                "goals_scored":"goals_scored",
                "goals_conceded":"goals_conceded",
            }

            for market,stat_name in mappings.items():
                value=data.get(stat_name)
                if value is None:
                    continue
                sums[market]+=float(value)
                counts[market]+=1

        result={}
        for market,total in sums.items():
            if counts[market]:
                result[market]=(total/counts[market],counts[market])
        profiles[(int(team_id),int(league_id),int(season))]=result

    return profiles


def _stat_number(obj, *names):
    if obj is None:
        return None
    if isinstance(obj,(int,float)):
        return float(obj)
    if isinstance(obj,str):
        try:
            return float(obj.replace("%","").replace(",","").strip())
        except ValueError:
            return None
    if isinstance(obj,dict):
        for name in names:
            if name in obj:
                value=_stat_number(obj[name])
                if value is not None:
                    return value
    return None


def build_team_profile(team_id, profile):
    return profile if isinstance(profile,dict) else {}




# Markets manually confirmed by the user as NOT offered on Betano.
# Keys are normalized "home - away" fixture names.
MANUAL_BETANO_MARKET_BLOCKS = {
    "cards": {
        "atletico escobar - defensores de vilelas",
        "al-hilal saudi fc - neom",
    },
    "shots_countries": {"romania", "norway"},
}

def _market_allowed_on_betano(result, market):
    home = _norm(result.get("home_name"))
    away = _norm(result.get("away_name"))
    fixture_key = f"{home} - {away}"
    if fixture_key in MANUAL_BETANO_MARKET_BLOCKS.get(market, set()):
        return False
    if market == "shots" and _norm(result.get("country")) in MANUAL_BETANO_MARKET_BLOCKS["shots_countries"]:
        return False
    return True

def filter_markets_by_bookmaker(result, bookmaker_markets=None):
    """
    bookmaker_markets:
        {fixture_id: {"goals": bool, "corners": bool, "cards": bool, "shots": bool}}

    Only a real bookmaker-market feed should populate this map. We deliberately
    do not infer Betano availability from league statistics.
    """
    if not bookmaker_markets:
        return result

    fid = result.get("fixture_id")
    available = bookmaker_markets.get(fid, {})
    result["markets"] = {
        market: value
        for market, value in result.get("markets", {}).items()
        if available.get(market) is True
    }
    return result



def _market_allowed_by_betano(match, market):
    """
    Known Betano availability rules supplied for this scanner:
      - Al-Hilal Saudi FC - NEOM: cards + corners remain allowed.
      - Romania: shots are not offered.
      - Norway 2. Division: shots are not offered.
      - Norway top division: shots remain allowed.
    Other markets are not removed unless explicitly known unavailable.
    """
    league = match.get("league") or {}
    country = str(league.get("country") or "").strip().casefold()
    league_name = str(league.get("name") or "").strip().casefold()

    if market == "shots":
        if country == "romania":
            return False
        if country == "norway" and (
            "2. division" in league_name
            or "2 division" in league_name
            or "2.division" in league_name
        ):
            return False

    return True



ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"

def _big(text):
    return f"{ANSI_BOLD}{text}{ANSI_RESET}"


def _signal_text(text):
    """Make scanner output substantially more prominent in terminal/Railway logs."""
    return f"\033[1m\033[4m{text}\033[0m"


def analyse_fixture(fixture, team_profiles):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]

    hp = build_team_profile(
        home["id"],
        team_profiles.get(home["id"], {}),
    )
    ap = build_team_profile(
        away["id"],
        team_profiles.get(away["id"], {}),
    )

    markets = {}

    for market in ("corners", "shots", "cards"):
        h = hp.get(market)
        a = ap.get(market)
        if h is not None and a is not None and h[1] >= 3 and a[1] >= 3:
            markets[market] = {
                "expected": h[0] + a[0],
                "home": h[0],
                "away": a[0],
                "sample": min(h[1], a[1]),
            }

    hs = hp.get("goals_scored")
    hc = hp.get("goals_conceded")
    ass = ap.get("goals_scored")
    ac = ap.get("goals_conceded")

    if (
        hs is not None and hc is not None and ass is not None and ac is not None
        and hs[1] >= 3 and hc[1] >= 3 and ass[1] >= 3 and ac[1] >= 3
    ):
        home_xg = (hs[0] + ac[0]) / 2
        away_xg = (ass[0] + hc[0]) / 2
        markets["goals"] = {
            "expected": home_xg + away_xg,
            "home": home_xg,
            "away": away_xg,
            "sample": min(hs[1], hc[1], ass[1], ac[1]),
        }

    return {
        "fixture_id": fixture["fixture"]["id"],
        "home_name": home["name"],
        "away_name": away["name"],
        "league": fixture.get("league", {}).get("name", ""),
        "country": fixture.get("league", {}).get("country", ""),
        "date": fixture["fixture"]["date"],
        "markets": markets,
    }


def _match_info(r):
    try:
        dt = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).astimezone(TZ)
        kickoff = dt.strftime("%H:%M")
    except Exception:
        kickoff = "?"
    league = r.get("league") or "-"
    country = r.get("country") or "-"
    return (
        f"   Лига: {league}\n"
        f"   Държава: {country}\n"
        f"   Начало: {kickoff} BG"
    )


def format_market(results, key, label, emoji):
    valid = [
        r for r in results
        if key in r["markets"] and _market_allowed_on_betano(r, key)
    ]

    high = sorted(
        valid,
        key=lambda r: r["markets"][key]["expected"],
        reverse=True,
    )[:5]
    low = sorted(
        valid,
        key=lambda r: r["markets"][key]["expected"],
    )[:5]

    lines = [f"{emoji} {label.upper()}", "🔥 НАД"]

    if high:
        for i, r in enumerate(high, 1):
            x = r["markets"][key]
            lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
            lines.append(
                f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}"
            )
            lines.append(_match_info(r))
            if i < len(high):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистически данни.")

    lines.extend(["", "❄️ ПОД"])

    if low:
        for i, r in enumerate(low, 1):
            x = r["markets"][key]
            lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
            lines.append(
                f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}"
            )
            lines.append(_match_info(r))
            if i < len(low):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистически данни.")

    return "\n".join(lines)


BLOCKED_COUNTRIES = {"belarus", "russia"}

def _is_blocked_fixture(match):
    country = str((match.get("league") or {}).get("country") or "").strip().casefold()
    return country in BLOCKED_COUNTRIES

def _is_cup_competition(league):
    name = str((league or {}).get("name") or "").casefold()
    typ = str((league or {}).get("type") or "").casefold()
    return "cup" in name or "copa" in name or "knockout" in typ

def _team_stats_competition(match, team_id):
    league = match.get("league") or {}
    lid = league.get("id")
    season = league.get("season")
    if not _is_cup_competition(league) and lid and season:
        return int(lid), int(season)

    # For cup fixtures, resolve the team's current league/season from Highlightly team statistics.
    from_date = f"{int(season) - 1 if season else datetime.now(TZ).year - 1}-07-01"
    rows = _api(f"teams/statistics/{int(team_id)}", {"fromDate": from_date, "timezone": "Europe/Sofia"})
    candidates = []
    for item in rows if isinstance(rows, list) else []:
        lgid = item.get("leagueId")
        sy = item.get("season")
        lname = str(item.get("leagueName") or "").casefold()
        if lgid and sy and "cup" not in lname and "copa" not in lname and "knockout" not in lname:
            candidates.append((int(lgid), int(sy)))
    if candidates:
        return candidates[0]
    return None

# =========================================================
# BETANO PREMATCH MARKET FILTER
# =========================================================

BETANO_BOOKMAKER_ID = 32


def get_betano_prematch_markets(fixture_id):
    result = {"match": False, "corners": False, "shots": False, "cards": False}
    rows = _api("odds", {"matchId": int(fixture_id), "bookmakerId": BETANO_BOOKMAKER_ID, "oddsType": "prematch", "limit": 5})
    for row in rows if isinstance(rows, list) else []:
        for market in row.get("odds", []) or []:
            if int(market.get("bookmakerId") or 0) != BETANO_BOOKMAKER_ID:
                continue
            result["match"] = True
            name = str(market.get("market") or "").casefold()
            if "corner" in name: result["corners"] = True
            if "shot" in name: result["shots"] = True
            if "card" in name or "booking" in name: result["cards"] = True
    print("BETANO FILTER RESULT:", fixture_id, result)
    return result

def filter_matches_by_betano_markets(matches):

    if not matches:
        return []

    betano_markets = {}

    # Do the Betano checks in parallel.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:

        futures = {
            pool.submit(
                get_betano_prematch_markets,
                int(m["fixture"]["id"])
            ): m
            for m in matches
        }

        for fut in as_completed(futures):

            match = futures[fut]
            fixture_id = int(
                match["fixture"]["id"]
            )

            try:
                betano_markets[fixture_id] = fut.result()

            except Exception as exc:
                print(
                    "BETANO FILTER ERROR:",
                    fixture_id,
                    repr(exc)
                )

                betano_markets[fixture_id] = {
                    "match": False,
                    "corners": False,
                    "shots": False,
                    "cards": False,
                }

    filtered = []

    for match in matches:

        fixture_id = int(
            match["fixture"]["id"]
        )

        available = betano_markets.get(
            fixture_id,
            {}
        )

        if not available.get("match"):
            print(
                "SCANNER SKIP — NO BETANO:",
                fixture_id
            )
            continue

        # Save availability on the fixture so
        # analyse_one() can use it later.
        match["_betano_markets"] = available

        print(
            "SCANNER BETANO:",
            fixture_id,
            available
        )

        filtered.append(match)

    print(
        "BETANO MATCH FILTER:",
        len(matches),
        "->",
        len(filtered)
    )

    return filtered


def run_daily_scanner(mode="day", reference_date=None, send_func=None):
    init_scanner_db()
    now_bg = datetime.now(TZ)
    ref = reference_date or now_bg.date()
    ref = ref if hasattr(ref, "year") else now_bg.date()

    # One football daily scan: sent at 10:30 BG.
    # Fixture window is always 12:00 BG -> next day 12:00 BG.
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    title = "10:30 ДНЕВЕН СКЕНЕР"

    matches = get_fixtures_for_window(start, end)
    print(_signal_text(f"SCANNER {mode.upper()}: {len(matches)} upcoming fixtures"))

    # Betano is a MARKET-AVAILABILITY filter, not a fixture filter.
    # A missing Betano odds response must NOT erase a statistically valid match.
    # Otherwise a temporary odds/API miss can turn a full scan into zero signals.
    betano_markets = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(get_betano_prematch_markets, int(m["fixture"]["id"])): m
            for m in matches
        }
        for fut in as_completed(futures):
            m = futures[fut]
            fid = int(m["fixture"]["id"])
            try:
                betano_markets[fid] = fut.result()
            except Exception as exc:
                print("BETANO AVAILABILITY ERROR:", fid, repr(exc))
                betano_markets[fid] = {}

            m["_betano_markets"] = betano_markets.get(fid, {})

    # GLOBAL BLOCK: Russia + Belarus are never scanned.
    matches = [m for m in matches if not _is_blocked_fixture(m)]

    # Resolve the correct statistics competition for every team.
    histories = {}
    unique_requests = {}
    for m in matches:
        for tid in (m["teams"]["home"]["id"], m["teams"]["away"]["id"]):
            source = _team_stats_competition(m, tid)
            if source:
                unique_requests[(int(tid), int(source[0]), int(source[1]))] = None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(get_team_history, tid, season, league_id):
                (tid, league_id, season)
            for tid, league_id, season in unique_requests
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                histories[key] = fut.result()
            except Exception as exc:
                print("SCANNER HISTORY ERROR:", key, repr(exc))
                histories[key] = []

    stats_by_fixture = load_historical_statistics(list(histories.values()))
    team_profiles = build_profiles_from_histories(histories, stats_by_fixture)

    print("SCANNER CURRENT-SEASON HISTORY:", sum(1 for v in histories.values() if v), "/", len(histories))
    print("SCANNER HISTORICAL FIXTURES WITH DATA:", sum(1 for v in stats_by_fixture.values() if v), "/", len(stats_by_fixture))
    print(_signal_text("SCANNER MARKET RULE: missing corner/card/shot stats are NOT treated as zero; each team needs >=3 actual observations."))

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        def analyse_one(m):
            home_id = int(m["teams"]["home"]["id"])
            away_id = int(m["teams"]["away"]["id"])
            hs = _team_stats_competition(m, home_id)
            aws = _team_stats_competition(m, away_id)
            profiles = {
                home_id: team_profiles.get((home_id, hs[0], hs[1]), {}) if hs else {},
                away_id: team_profiles.get((away_id, aws[0], aws[1]), {}) if aws else {},
            }
            result = analyse_fixture(m, profiles)
            betano = m.get("_betano_markets", {})
            # Never delete a statistical market because the odds endpoint
            # returned nothing. Delete it only when Betano explicitly confirms
            # that the market is unavailable.
            result["markets"] = {
                k: v for k, v in result.get("markets", {}).items()
                if k == "goals" or betano.get(k) is not False
            }
            return result

        futures = {pool.submit(analyse_one, m): m for m in matches}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                print("SCANNER MATCH ERROR:", repr(exc))

    results.sort(key=lambda x: x["date"])
    lines = [
        "📊 DAILY STATISTICAL SCANNER — СИГНАЛИ",
        now_bg.strftime("%d.%m.%Y"),
        f"\n{title}",
        f"Мачове в прозореца: {len(matches)}",
        f"Мачове с поне един валиден пазар: {sum(1 for r in results if r['markets'])}",
        "История: всички завършени мачове от текущия сезон",
        "",
    ]
    lines.append(format_market(results, "corners", "КОРНЕРИ", "🚩"))
    lines.append("")
    lines.append(format_market(results, "cards", "КАРТОНИ", "🟨"))
    lines.append("")
    lines.append(format_market(results, "shots", "УДАРИ", "🎯"))
    lines.append("")
    lines.append(format_market(results, "goals", "ГОЛОВЕ", "⚽"))
    lines.append(f"\n⏱ Scan time: {time.time() - _START:.1f}s")

    message = "\n".join(lines)
    print(message)

    # Telegram has a 4096-character message limit.  The complete
    # 7-sport report can legitimately exceed that limit, so send it
    # in ordered chunks instead of losing the whole report with HTTP 400.
    if send_func:
        _send_sport_report_chunks(message, send_func)

    return message


def _send_sport_report_chunks(message, send_func, max_chars=3700):
    """Send the full report in Telegram-safe chunks, preferably by sport section."""
    if len(message) <= max_chars:
        send_func(message)
        return

    # Split on the visual separator first so sport sections stay intact.
    sections = message.split("\n────────────────────\n")
    chunks = []
    current = ""

    for section in sections:
        section = section.strip()
        if not section:
            continue

        candidate = section if not current else current + "\n\n────────────────────\n\n" + section
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # A single section should normally fit. If it does not, split
        # safely by lines without cutting a line in half.
        if len(section) > max_chars:
            part = ""
            for line in section.splitlines():
                candidate = line if not part else part + "\n" + line
                if len(candidate) <= max_chars:
                    part = candidate
                else:
                    if part:
                        chunks.append(part)
                    part = line
            if part:
                current = part
        else:
            current = section

    if current:
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        if total > 1:
            chunk = f"📊 SPORT DAILY STATISTICAL SCANNER ({i}/{total})\n\n" + chunk
        print(f"SPORT TELEGRAM CHUNK {i}/{total}: {len(chunk)} chars")
        send_func(chunk)

_START = time.time()


# =========================================================
# SPORT DAILY STATISTICAL SCANNER — HIGHLIGHTLY SPORT ULTRA
# =========================================================
# Football LIVE/PREMATCH is intentionally NOT called here.
# Sport statistics use Highlightly Sport Ultra only.
# =========================================================

SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_HOST = "sport-highlights-api.p.rapidapi.com"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100
SPORT_HISTORY_FROM = f"{datetime.now(TZ).year}-07-01"

# Global exclusion: Russia and Belarus are blocked for every sport/league.
BLOCKED_COUNTRIES = {"russia", "belarus"}

def _sport_is_blocked_country(match):
    _league = match.get("league") or {}
    if isinstance(_league, dict):
        _country = _league.get("country") or _league.get("countryName") or ""
        if isinstance(_country, dict):
            _country = _country.get("name") or _country.get("countryName") or ""
    else:
        _country = ""
    if not _country:
        _country = match.get("country") or match.get("countryName") or ""
        if isinstance(_country, dict):
            _country = _country.get("name") or _country.get("countryName") or ""
    return str(_country).strip().casefold() in BLOCKED_COUNTRIES

SPORTS_CONFIG = {
    "basketball": {"name": "🏀 БАСКЕТБОЛ", "endpoint": "basketball/matches", "stats": "basketball/teams/statistics", "metric": "points"},
    "hockey": {"name": "🏒 ХОКЕЙ", "endpoint": "hockey/matches", "stats": "hockey/teams/statistics", "metric": "goals"},
    "american-football": {"name": "🏈 NFL / NCAA — Division I / Division II", "endpoint": "american-football/matches", "stats": "american-football/teams/statistics", "metric": "points"},
    "baseball": {"name": "⚾ БЕЙЗБОЛ", "endpoint": "baseball/matches", "stats": "baseball/teams/statistics", "metric": "runs"},
    "rugby": {"name": "🏉 РЪГБИ", "endpoint": "rugby/matches", "stats": "rugby/teams/statistics", "metric": "points"},
    "volleyball": {"name": "🏐 ВОЛЕЙБОЛ", "endpoint": "volleyball/matches", "stats": "volleyball/teams/statistics", "metric": "points"},
    "handball": {"name": "🤾 ХАНДБАЛ", "endpoint": "handball/matches", "stats": "handball/teams/statistics", "metric": "goals"},
}

_SPORT_API_CALLS = 0
_SPORT_STATS_CACHE = {}


def _sport_cache_get(key):
    import json
    try:
        conn = _db(); row = conn.execute("SELECT data FROM scanner_sport_cache WHERE cache_key=?", (key,)).fetchone(); conn.close()
        return json.loads(row[0]) if row else None
    except Exception:
        return None

def _sport_cache_put(key, data):
    import json
    try:
        conn = _db(); conn.execute("INSERT OR REPLACE INTO scanner_sport_cache(cache_key,data,updated_at) VALUES (?,?,?)", (key, json.dumps(data), datetime.now(timezone.utc).isoformat())); conn.commit(); conn.close()
    except Exception as exc:
        print("SPORT CACHE WRITE ERROR:", repr(exc))

def _sport_api_get(endpoint, params=None):
    """Single Sport API request. 429 locks Sport API; never retries it."""
    global _SPORT_API_CALLS
    if _quota_is_locked("sport"):
        raise APIQuotaExceeded("sport")

    _SPORT_API_CALLS += 1
    try:
        from config import HIGHLIGHTLY_API_KEY
        headers = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY}
        response = requests.get(
            f"{SPORT_API_BASE}/{endpoint}",
            headers=headers,
            params=params or {},
            timeout=20,
        )
    except requests.RequestException as exc:
        print("SPORT API REQUEST ERROR:", endpoint, repr(exc))
        return []

    remaining = response.headers.get("x-ratelimit-requests-remaining")
    limit = response.headers.get("x-ratelimit-requests-limit")
    print(f"SPORT API: {endpoint} status={response.status_code} remaining={remaining} limit={limit}")

    if response.status_code == 429:
        _set_quota_lock("sport")
        print("HIGHLIGHTLY SPORT API LIMIT: 429 — LOCKED; no further sport requests.")
        raise APIQuotaExceeded("sport")

    if response.status_code != 200:
        print("SPORT API ERROR:", endpoint, response.status_code, response.text[:300])
        return []

    try:
        payload = response.json()
    except ValueError:
        print("SPORT API ERROR: invalid JSON:", endpoint)
        return []

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    return []

def _sport_match_datetime(match):
    raw = match.get("date") or match.get("startTime") or match.get("startDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None


def _sport_team_id(team):
    if isinstance(team, dict):
        value = team.get("id") or team.get("teamId")
    else:
        value = team
    try:
        return int(value)
    except Exception:
        return None


def _sport_team_name(team):
    if not isinstance(team, dict):
        return str(team or "Unknown")
    return str(team.get("name") or team.get("displayName") or team.get("shortName") or "Unknown")


def _sport_match_names(match):
    home = match.get("homeTeam") or match.get("home") or {}
    away = match.get("awayTeam") or match.get("away") or {}
    return _sport_team_name(home), _sport_team_name(away)


def _sport_league_country(match):
    league = match.get("league") or {}
    if isinstance(league, dict):
        league_name = league.get("name") or league.get("leagueName") or ""
        country = league.get("country") or {}
        if isinstance(country, dict):
            country_name = country.get("name") or country.get("countryName") or ""
        else:
            country_name = str(country or "")
    else:
        league_name = str(league or "")
        country_name = ""

    # Highlightly can provide country at match level rather than inside league.
    if not country_name:
        country = match.get("country") or match.get("countryName") or ""
        if isinstance(country, dict):
            country_name = country.get("name") or country.get("countryName") or ""
        else:
            country_name = str(country or "")

    return str(league_name), str(country_name)


def _recursive_number(obj, names):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).casefold() in names:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
                if isinstance(value, str):
                    try:
                        return float(value.replace(",", "").strip())
                    except ValueError:
                        pass
        for value in obj.values():
            found = _recursive_number(value, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _recursive_number(value, names)
            if found is not None:
                return found
    return None


def _extract_team_average(stats_rows, metric):
    """Extract scored average from Highlightly's current-season team statistics."""
    if isinstance(stats_rows, dict):
        stats_rows = stats_rows.get("data", stats_rows)
        if isinstance(stats_rows, dict):
            stats_rows = [stats_rows]
    if not isinstance(stats_rows, list):
        return None

    metric_names = {
        "points": {"scored", "score", "scoredpoints", "pointsscored", "points_scored"},
        "goals": {"scored", "goals_scored", "goalsscored", "goals"},
        "runs": {"scored", "runs_scored", "runsscored", "runs"},
    }[metric]

    candidates = []
    for row in stats_rows:
        if not isinstance(row, dict):
            continue

        total = row.get("total", row)
        games = _recursive_number(total, {"played", "gamesplayed", "games", "matchesplayed"})
        scored = _recursive_number(total, metric_names)
        if scored is None:
            scored = _recursive_number(row, metric_names)

        if games and games > 0 and scored is not None:
            season = _recursive_number(row, {"season", "seasonid", "year"}) or 0
            candidates.append((season, games, scored, row))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    season, games, scored, raw = candidates[0]
    return {
        "average": scored / games,
        "games": int(games),
        "season": int(season) if season else None,
        "raw": raw,
    }


def _get_team_average(sport_key, team_id, metric):
    if not team_id:
        return None
    cache_key = (sport_key, int(team_id), metric)
    if cache_key in _SPORT_STATS_CACHE:
        return _SPORT_STATS_CACHE[cache_key]

    cfg = SPORTS_CONFIG[sport_key]
    persistent_key = f"teamavg:{sport_key}:{int(team_id)}:{metric}:{SPORT_HISTORY_FROM}"
    persistent = _sport_cache_get(persistent_key)
    if persistent is not None:
        _SPORT_STATS_CACHE[cache_key] = persistent
        return persistent
    rows = _sport_api_get(
        f"{cfg['stats']}/{int(team_id)}",
        {"fromDate": SPORT_HISTORY_FROM, "timezone": SPORT_API_TZ},
    )
    result = _extract_team_average(rows, metric)
    _SPORT_STATS_CACHE[cache_key] = result
    _sport_cache_put(persistent_key, result)

    if result:
        print(
            f"SPORT HISTORY: {sport_key} team={team_id} "
            f"average={result['average']:.2f} games={result['games']}"
        )
    else:
        print(f"SPORT HISTORY: {sport_key} team={team_id} NO DATA")
    return result


def _get_sport_fixtures(cfg, start, end):
    """Fetch the complete local-day window, with a safe fallback if timezone filtering returns empty."""
    all_rows = []
    dates = []
    d = start.date()
    while d <= end.date():
        dates.append(d)
        d += timedelta(days=1)

    for day in dates:
        params = {
            "date": day.isoformat(),
            "timezone": SPORT_API_TZ,
            "limit": SPORT_API_LIMIT,
        }
        cache_key = f"fixtures:{cfg['endpoint']}:{day.isoformat()}:{SPORT_API_TZ}:{SPORT_API_LIMIT}"
        rows = _sport_cache_get(cache_key)
        if rows is None:
            rows = _sport_api_get(cfg["endpoint"], params)
            _sport_cache_put(cache_key, rows if isinstance(rows, list) else [])
        else:
            print("SPORT FIXTURE CACHE:", cfg["endpoint"], day.isoformat(), "rows=", len(rows) if isinstance(rows, list) else 0)
        all_rows.extend(rows if isinstance(rows, list) else [])

    unique = {}
    for match in all_rows:
        if not isinstance(match, dict):
            continue
        dt = _sport_match_datetime(match)
        if dt is None or not (start <= dt < end):
            continue

        # GLOBAL BLOCK: Russia + Belarus are never scanned.
        if _sport_is_blocked_country(match):
            continue

        home = match.get("homeTeam") or match.get("home") or {}
        away = match.get("awayTeam") or match.get("away") or {}
        home_id = _sport_team_id(home)
        away_id = _sport_team_id(away)
        if not home_id or not away_id:
            continue

        mid = match.get("id") or match.get("matchId") or f"{home_id}-{away_id}-{dt.isoformat()}"
        unique[mid] = match

    return list(unique.values())


def _format_sport_entry(index, item):
    home, away = _sport_match_names(item["match"])
    league, country = _sport_league_country(item["match"])
    dt = item["datetime"]
    return (
        f"{index}. {home} - {away}\n"
        f"   {item['home_avg']:.2f} + {item['away_avg']:.2f} = {item['expected']:.2f}\n"
        f"   Лига: {league or '-'}\n"
        f"   Държава: {country or '-'}\n"
        f"   Дата: {dt.strftime('%d.%m.%Y')}\n"
        f"   Начало: {dt.strftime('%H:%M')} BG"
    )


def _build_sport_section(sport_name, candidates):
    if not candidates:
        return f"{sport_name}\nНяма достатъчно исторически статистически данни."

    # Top 5, but never invent entries when fewer are valid.
    top_over = sorted(candidates, key=lambda x: x["expected"], reverse=True)[:5]
    top_under = sorted(candidates, key=lambda x: x["expected"])[:5]

    lines = [sport_name, "", "🔥 НАД"]
    for i, item in enumerate(top_over, 1):
        lines.append(_format_sport_entry(i, item))
        if i < len(top_over):
            lines.append("")

    lines.extend(["", "❄️ ПОД"])
    for i, item in enumerate(top_under, 1):
        lines.append(_format_sport_entry(i, item))
        if i < len(top_under):
            lines.append("")

    return "\n".join(lines)


def run_sport_daily_scanner(send_func=None):
    """Build Top 5 Over/Under from real current-season team statistics."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = [
        "📊 DAILY STATISTICAL SCANNER — СИГНАЛИ",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: всички налични завършени мачове / текущосезонни team statistics от Sport Ultra",
        "",
    ]

    for sport_key, cfg in SPORTS_CONFIG.items():
        print(f"SPORT SCAN: {sport_key} — FIXTURES")
        fixtures = _get_sport_fixtures(cfg, start, end)
        candidates = []

        for match in fixtures:
            if _sport_is_blocked_country(match):
                continue

            # American football scope: NFL + NCAA Division I/II only.
            if sport_key == "american-football":
                league_name, _country_name = _sport_league_country(match)
                lname = str(league_name or "").casefold()
                allowed_american = (
                    "nfl" in lname
                    or "ncaa" in lname
                    or "division i" in lname
                    or "division ii" in lname
                    or "division 1" in lname
                    or "division 2" in lname
                )
                if not allowed_american:
                    continue

            home = match.get("homeTeam") or match.get("home") or {}
            away = match.get("awayTeam") or match.get("away") or {}
            home_id = _sport_team_id(home)
            away_id = _sport_team_id(away)
            if not home_id or not away_id:
                continue

            h = _get_team_average(sport_key, home_id, cfg["metric"])
            a = _get_team_average(sport_key, away_id, cfg["metric"])
            if not h or not a or h["games"] < 3 or a["games"] < 3:
                continue

            dt = _sport_match_datetime(match)
            if not dt:
                continue

            candidates.append({
                "match": match,
                "datetime": dt,
                "home_avg": h["average"],
                "away_avg": a["average"],
                "expected": h["average"] + a["average"],
            })

        lines.append(_build_sport_section(cfg["name"], candidates))
        lines.append("")
        lines.append("────────────────────")
        lines.append("")

        print(
            f"SPORT RESULT: {sport_key} fixtures={len(fixtures)} "
            f"valid={len(candidates)}"
        )

    lines.append(f"📡 API заявки: {_SPORT_API_CALLS}")
    lines.append(f"⏱ Scan time: {time.time() - started:.1f}s")

    message = "\n".join(lines)
    print(message)
    if send_func:
        # Telegram hard limit is 4096 characters. Keep a safety margin
        # for the numbered chunk header and send the complete report.
        _send_sport_report_chunks(message, send_func, max_chars=3700)
    return message


# =========================================================
# DAILY SCAN SCHEDULER — SPORT ONLY
# =========================================================


def run_due_scans(send_func):
    """Run the original football + sport scanners once daily, with persistent 429 locks."""
    init_scanner_db()
    _quota_lock_table()
    now = datetime.now(TZ)
    today = now.date()

    if now.hour > 10 or (now.hour == 10 and now.minute >= 30):
        football_key = f"football_daily:{today.isoformat()}"
        if not already_ran(football_key) and not _quota_is_locked("football"):
            print(_signal_text("FOOTBALL DAILY SCANNER STARTED"))
            try:
                run_daily_scanner(mode="day", reference_date=today, send_func=send_func)
                mark_ran(football_key)
                print(_signal_text("FOOTBALL DAILY SCANNER FINISHED"))
            except APIQuotaExceeded:
                print(_signal_text("FOOTBALL SKIPPED — DAILY API QUOTA LOCKED"))
            except Exception as exc:
                print(_signal_text(f"FOOTBALL DAILY SCANNER ERROR: {exc!r}"))

    if now.hour > 10 or (now.hour == 10 and now.minute >= 0):
        sport_key = f"sport_daily:{today.isoformat()}"
        if not already_ran(sport_key) and not _quota_is_locked("sport"):
            print(_signal_text("SPORT DAILY SCANNER STARTED"))
            try:
                run_sport_daily_scanner(send_func)
                mark_ran(sport_key)
                print(_signal_text("SPORT DAILY SCANNER FINISHED"))
            except APIQuotaExceeded:
                print(_signal_text("SPORT SKIPPED — DAILY API QUOTA LOCKED"))
            except Exception as exc:
                print(_signal_text(f"SPORT DAILY SCANNER ERROR: {exc!r}"))

    return True

# =========================================================
# STANDALONE ENTRYPOINT
# =========================================================

def _telegram_send(text):
    """
    Optional standalone Telegram delivery.
    Uses BOT_TOKEN / TELEGRAM_BOT_TOKEN and CHAT_ID only if explicitly
    available in config or environment. Otherwise stdout remains the
    authoritative output.
    """
    import os

    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")

    if not token or not chat_id:
        try:
            from config import CHAT_ID as CFG_CHAT_ID
            chat_id = chat_id or CFG_CHAT_ID
        except Exception:
            pass

        for name in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN"):
            if token:
                break
            try:
                from config import __dict__ as _cfg
                token = _cfg.get(name)
            except Exception:
                pass

    if not token or not chat_id:
        print("TELEGRAM: token/chat_id not configured; report kept in stdout.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Keep each message safely below Telegram's 4096-character hard limit.
    chunks = []
    for part in text.split("\n────────────────────\n"):
        part = part.strip()
        if not part:
            continue
        if len(part) <= 3700:
            chunks.append(part)
        else:
            buf = ""
            for line in part.splitlines():
                candidate = line if not buf else buf + "\n" + line
                if len(candidate) > 3700:
                    if buf:
                        chunks.append(buf)
                    buf = line[:3700]
                else:
                    buf = candidate
            if buf:
                chunks.append(buf)

    ok = True
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk},
                timeout=20,
            )
            if r.status_code != 200:
                ok = False
                print("TELEGRAM SEND ERROR:", r.status_code, r.text[:500])
        except Exception as exc:
            ok = False
            print("TELEGRAM SEND ERROR:", repr(exc))
    return ok


if __name__ == "__main__":
    print("=" * 60)
    print("📊 DAILY STATISTICAL SCANNER — START")
    print("BUILD: FINAL 2026-09-17")
    print("GLOBAL BLOCK: RUSSIA + BELARUS")
    print("SPORT AMERICAN FOOTBALL: NFL + NCAA DIVISION I / II")
    print("=" * 60)

    try:
        run_due_scans(_telegram_send)
    except Exception as exc:
        print("SCANNER FATAL ERROR:", repr(exc))
        raise
