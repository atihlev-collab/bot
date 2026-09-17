# BUILD: HIGHLIGHTLY-DAILY-STATISTICAL-SCANNER-FINAL-20260917
# =========================================================
# DAILY STATISTICAL SCANNER
# ========================================================
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


def _api(endpoint, params=None, timeout=25):
    global _LAST_API_CALL

    for attempt in range(5):
        try:
            with _API_LOCK:
                wait = _API_MIN_INTERVAL - (time.monotonic() - _LAST_API_CALL)
                if wait > 0:
                    time.sleep(wait)
                _LAST_API_CALL = time.monotonic()

            r = requests.get(
                f"{BASE_URL}/{endpoint}",
                headers=HEADERS,
                params=params or {},
                timeout=timeout,
            )

            if r.status_code == 429 or 500 <= r.status_code < 600:
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(1.0 * (2 ** attempt), 8.0)
                time.sleep(delay)
                continue

            r.raise_for_status()
            payload = r.json()

            # Highlightly returns some endpoints (notably /statistics/{matchId})
            # as a bare JSON list, while others return {"data": [...]}.
            # Never call .get() on a list.
            if isinstance(payload, list):
                return payload

            if isinstance(payload, dict):
                if payload.get("errors"):
                    print("SCANNER API ERROR:", endpoint, payload.get("errors"))
                    return None
                data = payload.get("data", [])
                return data

            print("SCANNER API ERROR:", endpoint, "unexpected response type", type(payload).__name__)
            return None

        except Exception as e:
            if attempt == 4:
                print("SCANNER REQUEST ERROR:", endpoint, repr(e))
                return None
            time.sleep(min(0.8 * (2 ** attempt), 6.0))

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

    def fetch_team_matches(extra):
        rows = _api("matches", {**extra, "limit": 100})
        return [_normalize_match(x) for x in (rows if isinstance(rows, list) else []) if _normalize_match(x)]

    primary = []
    if league_id:
        primary = fetch_team_matches({"leagueId": league_id, "season": season, "homeTeamId": team_id})
        primary += fetch_team_matches({"leagueId": league_id, "season": season, "awayTeamId": team_id})

    def clean(rows):
        out, seen = [], set()
        for f in rows:
            fid = (f.get("fixture") or {}).get("id")
            lg = f.get("league") or {}
            status = (f.get("fixture") or {}).get("status", {}).get("short", "")
            if not fid or fid in seen or int(lg.get("season") or 0) != season or status not in {"FT", "AET", "PEN"}:
                continue
            name = str(lg.get("name") or "").casefold()
            typ = str(lg.get("type") or "").casefold()
            if typ == "friendly" or "friend" in name:
                continue
            seen.add(fid); out.append(f)
        return out

    primary = clean(primary)
    if len(primary) < 3:
        fallback = fetch_team_matches({"season": season, "homeTeamId": team_id})
        fallback += fetch_team_matches({"season": season, "awayTeamId": team_id})
        primary = clean(primary + fallback)

    primary.sort(key=lambda f: (f.get("fixture") or {}).get("date", ""), reverse=True)
    primary = primary[:12]
    primary.sort(key=lambda f: (f.get("fixture") or {}).get("date", ""))
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
    """Extract per-team match statistics from Highlightly /statistics/{matchId}.

    Highlightly returns statistics as `{value, displayName}` records. Goals
    come from the match score and the other markets are only counted when
    Highlightly actually supplies the corresponding statistic. Missing data
    is never converted to zero.
    """
    fid=(fixture.get("fixture") or {}).get("id")
    out={}
    for block in fixture.get("statistics") or []:
        tid=(block.get("team") or {}).get("id")
        if not tid:
            continue
        vals={}
        for item in block.get("statistics") or []:
            # Highlightly Football API uses `displayName` for match-stat
            # labels (for example: Corners, Total shots, Yellow cards).
            # Older API-Football code used `type`; support both so the
            # football scanner can consume Highlightly data correctly.
            raw_name = item.get("displayName") or item.get("type") or item.get("name") or ""
            typ = _norm(raw_name)
            val = item.get("value")
            if isinstance(val, str):
                val = val.replace("%", "").strip()
            try:
                val = float(val) if val is not None else None
            except (TypeError, ValueError):
                val = None
            if val is not None:
                vals[typ] = val

                # Canonical aliases used by the scanner's market model.
                aliases = {
                    "corners": {"corners", "corner kicks", "corner"},
                    "shots": {"total shots", "total shot", "shots", "shots total"},
                    "cards": {"yellow cards", "yellow card", "yellow cards total"},
                }
                for canonical, names in aliases.items():
                    if typ in names:
                        vals[canonical] = val

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
    for fid, base_fixture in fixtures_by_id.items():
        rows = _api(f"statistics/{fid}", {})
        fake = dict(base_fixture)
        fake["statistics"] = rows if isinstance(rows, list) else []
        _, data = _fixture_market_values(fake)
        loaded[fid] = data
    _SCAN_FIXTURE_STATS.clear()
    _SCAN_FIXTURE_STATS.update(loaded)
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
# SPORT DAILY STATISTICAL SCANNER — SIMPLE HIGHLIGHTLY
# =========================================================
# Simple rule:
#   1) get today's real fixtures from Highlightly
#   2) get each team's last 5 finished games from Highlightly
#   3) calculate the real average scored metric
#   4) home average + away average = simple expected total
# No AI, no probabilities, no invented values.
# =========================================================

SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_HOST = "sport-highlights-api.p.rapidapi.com"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100

SPORTS_CONFIG = {
    "basketball": {"name": "🏀 БАСКЕТБОЛ", "metric": "points"},
    "hockey": {"name": "🏒 ХОКЕЙ", "metric": "goals"},
    "american-football": {"name": "🏈 NFL / NCAA — Division I / Division II", "metric": "points"},
    "baseball": {"name": "⚾ БЕЙЗБОЛ", "metric": "runs"},
    "rugby": {"name": "🏉 РЪГБИ", "metric": "points"},
    "volleyball": {"name": "🏐 ВОЛЕЙБОЛ", "metric": "points"},
    "handball": {"name": "🤾 ХАНДБАЛ", "metric": "goals"},
}

_SPORT_API_CALLS = 0
_SPORT_API_CALLS_BY_SPORT = {}
_SPORT_CACHE = {}
_SPORT_LOCK = threading.Lock()
_SPORT_LAST_CALL = 0.0
_SPORT_MIN_INTERVAL = 0.12


def _sport_get(sport, endpoint, params=None):
    global _SPORT_API_CALLS, _SPORT_LAST_CALL
    from config import HIGHLIGHTLY_API_KEY

    headers = {
        "x-rapidapi-key": HIGHLIGHTLY_API_KEY,
        "x-rapidapi-host": SPORT_API_HOST,
    }
    with _SPORT_LOCK:
        wait = _SPORT_MIN_INTERVAL - (time.monotonic() - _SPORT_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _SPORT_LAST_CALL = time.monotonic()

    _SPORT_API_CALLS += 1
    _SPORT_API_CALLS_BY_SPORT[sport] = _SPORT_API_CALLS_BY_SPORT.get(sport, 0) + 1
    url = f"{SPORT_API_BASE}/{sport}/{endpoint.lstrip('/')}"

    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=25)
        print(f"SPORT API: {sport}/{endpoint} status={r.status_code} params={params or {}}")
        if r.status_code != 200:
            print("SPORT API ERROR:", r.text[:300])
            return []
        payload = r.json()
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        return data if isinstance(data, list) else []
    except Exception as exc:
        print("SPORT API REQUEST ERROR:", sport, endpoint, repr(exc))
        return []


def _sport_dt(match):
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
    if isinstance(team, dict):
        return str(team.get("name") or team.get("displayName") or team.get("shortName") or "Unknown")
    return str(team or "Unknown")


def _sport_teams(match):
    return (
        match.get("homeTeam") or match.get("home") or {},
        match.get("awayTeam") or match.get("away") or {},
    )


def _sport_league_country(match):
    league = match.get("league") or {}
    if isinstance(league, dict):
        league_name = league.get("name") or league.get("leagueName") or ""
        league_id = league.get("id") or league.get("leagueId")
        season = league.get("season")
        country = league.get("country") or {}
        if isinstance(country, dict):
            country_name = country.get("name") or country.get("countryName") or ""
        else:
            country_name = str(country or "")
    else:
        league_name, league_id, season, country_name = str(league), None, None, ""
    if not country_name:
        country = match.get("country") or match.get("countryName") or ""
        country_name = country.get("name") if isinstance(country, dict) else str(country or "")
    return str(league_name), str(country_name), league_id, season


def _blocked_sport(match):
    _league, country, _lid, _season = _sport_league_country(match)
    return country.strip().casefold() in {"russia", "belarus"}


def _sport_score(match):
    """Return (home, away) from Highlightly score formats."""
    state = match.get("state") or {}
    score = state.get("score") or match.get("score") or {}
    current = score.get("current") if isinstance(score, dict) else None

    if isinstance(current, str):
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*[-:]\s*(-?\d+(?:\.\d+)?)", current)
        if m:
            return float(m.group(1)), float(m.group(2))

    def number(v):
        if isinstance(v, list):
            return number(v[0]) if v else None
        if isinstance(v, dict):
            for k in ("current", "value", "score"):
                if k in v:
                    n = number(v[k])
                    if n is not None:
                        return n
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if isinstance(current, dict):
        h = number(current.get("home") or current.get("homeTeam"))
        a = number(current.get("away") or current.get("awayTeam"))
        if h is not None and a is not None:
            return h, a

    if isinstance(score, dict):
        h = number(score.get("home") or score.get("homeTeam"))
        a = number(score.get("away") or score.get("awayTeam"))
        if h is not None and a is not None:
            return h, a
    return None


def _finished(match):
    state = match.get("state") or {}
    desc = str(state.get("description") or "").casefold()
    return any(x in desc for x in ("finished", "final", "ended")) and _sport_score(match) is not None


def _get_sport_fixtures(sport, start, end):
    rows = []
    d = start.date()
    while d <= end.date():
        rows.extend(_sport_get(sport, "matches", {
            "date": d.isoformat(),
            "timezone": SPORT_API_TZ,
            "limit": SPORT_API_LIMIT,
        }))
        d += timedelta(days=1)

    out = {}
    for m in rows:
        dt = _sport_dt(m)
        if not dt or not (start <= dt < end) or _blocked_sport(m):
            continue
        h, a = _sport_teams(m)
        if not _sport_team_id(h) or not _sport_team_id(a):
            continue
        mid = m.get("id") or f"{_sport_team_id(h)}-{_sport_team_id(a)}-{dt.isoformat()}"
        out[mid] = m
    return list(out.values())


def _american_allowed(match):
    league, _country, _lid, _season = _sport_league_country(match)
    s = league.casefold()
    return any(x in s for x in ("nfl", "ncaa", "division i", "division ii", "division 1", "division 2"))


def _team_last_five(sport, team_id, wanted_season=None):
    key = (sport, int(team_id), wanted_season)
    if key in _SPORT_CACHE:
        return _SPORT_CACHE[key]
    rows = _sport_get(sport, "last-five-games", {"teamId": int(team_id)})
    games = []
    for m in rows:
        if not isinstance(m, dict) or not _finished(m):
            continue
        if _blocked_sport(m):
            continue
        _lg, _country, _lid, season = _sport_league_country(m)
        if wanted_season is not None and season is not None:
            try:
                if int(season) != int(wanted_season):
                    continue
            except Exception:
                pass
        games.append(m)
    _SPORT_CACHE[key] = games[:5]
    return _SPORT_CACHE[key]


def _team_average(sport, team_id, metric, season):
    games = _team_last_five(sport, team_id, season)
    vals = []
    for m in games:
        h, a = _sport_teams(m)
        hs = _sport_team_id(h)
        aa = _sport_team_id(a)
        score = _sport_score(m)
        if score is None:
            continue
        sh, sa = score
        if int(hs or 0) == int(team_id):
            vals.append(sh)
        elif int(aa or 0) == int(team_id):
            vals.append(sa)
    if len(vals) < 3:
        return None
    return sum(vals) / len(vals), len(vals)


def _format_sport_item(i, item):
    h, a = _sport_teams(item["match"])
    league, country, _lid, _season = _sport_league_country(item["match"])
    dt = item["datetime"]
    return (
        f"{i}. {_sport_team_name(h)} - {_sport_team_name(a)}\n"
        f"   {item['home_avg']:.2f} + {item['away_avg']:.2f} = {item['expected']:.2f}\n"
        f"   Лига: {league or '-'}\n"
        f"   Държава: {country or '-'}\n"
        f"   Начало: {dt.strftime('%H:%M')} BG"
    )


def _sport_section(name, candidates):
    lines = [name, "", "🔥 НАД"]
    if not candidates:
        lines.append("Няма достатъчно исторически статистически данни.")
    else:
        for i, x in enumerate(sorted(candidates, key=lambda z: z["expected"], reverse=True)[:5], 1):
            lines.append(_format_sport_item(i, x))
            if i < min(5, len(candidates)): lines.append("")
    lines.extend(["", "❄️ ПОД"])
    if not candidates:
        lines.append("Няма достатъчно исторически статистически данни.")
    else:
        low = sorted(candidates, key=lambda z: z["expected"])[:5]
        for i, x in enumerate(low, 1):
            lines.append(_format_sport_item(i, x))
            if i < len(low): lines.append("")
    return "\n".join(lines)


def run_sport_daily_scanner(send_func=None):
    global _SPORT_API_CALLS, _SPORT_API_CALLS_BY_SPORT, _SPORT_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_API_CALLS_BY_SPORT = {}
    _SPORT_CACHE = {}
    started = time.time()
    now = datetime.now(TZ)
    start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = [
        "📊 DAILY STATISTICAL SCANNER — СИГНАЛИ",
        now.strftime("%d.%m.%Y"),
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: последните 5 завършени мача от текущия сезон на Highlightly",
        "",
    ]

    for sport, cfg in SPORTS_CONFIG.items():
        fixtures = _get_sport_fixtures(sport, start, end)
        candidates = []
        for m in fixtures:
            if sport == "american-football" and not _american_allowed(m):
                continue
            h, a = _sport_teams(m)
            hid, aid = _sport_team_id(h), _sport_team_id(a)
            _lg, _country, _lid, season = _sport_league_country(m)
            if not hid or not aid:
                continue
            havg = _team_average(sport, hid, cfg["metric"], season)
            aavg = _team_average(sport, aid, cfg["metric"], season)
            if not havg or not aavg:
                continue
            dt = _sport_dt(m)
            if not dt:
                continue
            candidates.append({
                "match": m,
                "datetime": dt,
                "home_avg": havg[0],
                "away_avg": aavg[0],
                "expected": havg[0] + aavg[0],
            })
        print(f"SPORT RESULT: {sport} fixtures={len(fixtures)} valid={len(candidates)}")
        lines.append(_sport_section(cfg["name"], candidates))
        lines.extend(["", "────────────────────", ""])

    lines.append(f"📡 API заявки: {_SPORT_API_CALLS}")
    if _SPORT_API_CALLS_BY_SPORT:
        lines.append("📡 По спортове: " + ", ".join(f"{k}={v}" for k, v in _SPORT_API_CALLS_BY_SPORT.items()))
    lines.append(f"⏱ Scan time: {time.time() - started:.1f}s")
    message = "\n".join(lines)
    print(message)
    if send_func:
        _send_sport_report_chunks(message, send_func, max_chars=3700)
    return message


# =========================================================
# DAILY SCAN SCHEDULER — SPORT ONLY
# =========================================================

def run_due_scans(send_func):
    """Run football and sport statistics once per day."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    if now.hour > 10 or (now.hour == 10 and now.minute >= 30):
        football_key = f"football_daily:{today.isoformat()}"
        if not already_ran(football_key):
            print(_signal_text("FOOTBALL DAILY SCANNER STARTED"))
            try:
                run_daily_scanner(mode="day", reference_date=today, send_func=send_func)
                mark_ran(football_key)
                print(_signal_text("FOOTBALL DAILY SCANNER FINISHED"))
            except Exception as exc:
                print(_signal_text(f"FOOTBALL DAILY SCANNER ERROR: {exc!r}"))

    sport_key = f"sport_test:{today.isoformat()}"
    if (now.hour > 10 or (now.hour == 10 and now.minute >= 0)) and not already_ran(sport_key):
        print(_signal_text("SPORT DAILY SCANNER STARTED"))
        try:
            run_sport_daily_scanner(send_func)
            mark_ran(sport_key)
            print(_signal_text("SPORT DAILY SCANNER FINISHED"))
        except Exception as exc:
            print(_signal_text(f"SPORT DAILY SCANNER ERROR: {exc!r}"))
    return True


# =========================================================
# STANDALONE TELEGRAM ENTRYPOINT
# =========================================================

def _telegram_send(text):
    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        try:
            from config import CHAT_ID as CFG_CHAT_ID
            chat_id = chat_id or CFG_CHAT_ID
        except Exception:
            pass
        try:
            from config import __dict__ as _cfg
            token = token or _cfg.get("TELEGRAM_BOT_TOKEN") or _cfg.get("BOT_TOKEN") or _cfg.get("TELEGRAM_TOKEN")
        except Exception:
            pass
    if not token or not chat_id:
        print("TELEGRAM: token/chat_id not configured; report kept in stdout.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks=[]
    for part in text.split("\n────────────────────\n"):
        part=part.strip()
        if not part: continue
        if len(part)<=3700:
            chunks.append(part)
        else:
            buf=""
            for line in part.splitlines():
                candidate=line if not buf else buf+"\n"+line
                if len(candidate)>3700:
                    if buf: chunks.append(buf)
                    buf=line[:3700]
                else: buf=candidate
            if buf: chunks.append(buf)
    ok=True
    for chunk in chunks:
        try:
            r=requests.post(url,json={"chat_id":chat_id,"text":chunk},timeout=20)
            if r.status_code!=200:
                ok=False; print("TELEGRAM SEND ERROR:",r.status_code,r.text[:500])
        except Exception as exc:
            ok=False; print("TELEGRAM SEND ERROR:",repr(exc))
    return ok


if __name__ == "__main__":
    print("=" * 60)
    print("📊 DAILY STATISTICAL SCANNER — SIMPLE")
    print("BUILD: 2026-09-17 SIMPLE")
    print("GLOBAL BLOCK: RUSSIA + BELARUS")
    print("SPORT HISTORY: LAST 5 FINISHED GAMES")
    print("=" * 60)
    run_due_scans(_telegram_send)
