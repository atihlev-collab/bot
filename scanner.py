# BUILD: HIGHLIGHTLY-FOOTBALL-API-SCANNER-FIX-1
# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs once per day at/after 10:00 Bulgaria time.
# Fixture window: 12:00 BG -> next day 12:00 BG.
# =========================================================

import re
import math
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
_UPCOMING_FIXTURES_CACHE = {}
_API_LOCK = threading.Lock()
_LAST_API_CALL = 0.0
_API_MIN_INTERVAL = 0.12

# ---------------------------------------------------------
# DAILY API QUOTA GUARD
# ---------------------------------------------------------

_QUOTA_LOCKS = {}
_QUOTA_LOCK_DATES = {}


class APIQuotaExceeded(Exception):
    """Raised when Highlightly reports that the daily quota is exhausted."""
    pass


def _quota_locked(scope="football"):
    """Return True when this scanner scope is locked for today's BG date."""
    today = datetime.now(TZ).date().isoformat()

    if _QUOTA_LOCK_DATES.get(scope) != today:
        _QUOTA_LOCKS[scope] = False
        _QUOTA_LOCK_DATES[scope] = today

    return bool(_QUOTA_LOCKS.get(scope, False))


def _set_quota_lock(scope="football"):
    today = datetime.now(TZ).date().isoformat()
    _QUOTA_LOCKS[scope] = True
    _QUOTA_LOCK_DATES[scope] = today


def _api(endpoint, params=None, timeout=25):
    global _LAST_API_CALL

    # Once Highlightly returns 429, stop ALL football requests for today.
    # Never retry a quota error: retries only burn more requests/time.
    if _quota_locked("football"):
        raise APIQuotaExceeded("Highlightly daily quota locked for today")

    if _quota_locked("football"):
        raise APIQuotaExceeded("Highlightly daily quota exhausted")

    for attempt in range(5):
        try:
            if _quota_locked("football"):
                raise APIQuotaExceeded("Highlightly daily quota locked for today")

            with _API_LOCK:
                wait = _API_MIN_INTERVAL - (time.monotonic() - _LAST_API_CALL)
                if wait > 0:
                    time.sleep(wait)
                _LAST_API_CALL = time.monotonic()

            r = requests.get(
                f"{BASE_URL.rstrip('/')}/{str(endpoint).lstrip('/')}",
                headers=HEADERS,
                params=params or {},
                timeout=timeout,
            )

            if r.status_code == 429:
                _set_quota_lock("football")
                print("SCANNER QUOTA LOCKED: Highlightly returned HTTP 429")
                raise APIQuotaExceeded("Highlightly daily quota exhausted")

            if 500 <= r.status_code < 600:
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(1.0 * (2 ** attempt), 8.0)

                time.sleep(delay)
                continue

            r.raise_for_status()
            payload = r.json()

            # Highlightly can return a bare list for some endpoints.
            # Normalize both list and object response shapes safely.
            if isinstance(payload, list):
                return payload

            if not isinstance(payload, dict):
                print("SCANNER API ERROR:", endpoint, "unexpected payload type", type(payload).__name__)
                return None

            if payload.get("errors"):
                print("SCANNER API ERROR:", endpoint, payload.get("errors"))
                return None

            data = payload.get("data", [])
            return data if isinstance(data, list) else []

        except APIQuotaExceeded:
            raise
        except APIQuotaExceeded:
            raise
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


def get_cached_upcoming_matches(start_bg, end_bg):
    """Return the exact morning fixture window for PREMATCH/Bet Builder."""
    key = (start_bg.isoformat(), end_bg.isoformat())
    cached = _UPCOMING_FIXTURES_CACHE.get(key)
    return list(cached) if cached else []


def get_fixtures_for_window(start_bg, end_bg):
    """Fetch EVERY fixture in the BG window, not only the first 100 per date.

    Highlightly documents ``limit`` + ``offset`` pagination for /matches.
    Germany, Netherlands and other busy football dates can exceed one page,
    so stopping after the first 100 silently drops fixtures.
    """
    days = []
    d = start_bg.date()
    while d <= end_bg.date():
        days.append(d)
        d += timedelta(days=1)

    all_matches, seen = [], set()
    page_size = 100

    for day in days:
        offset = 0
        while True:
            rows = _api(
                "matches",
                {
                    "date": day.isoformat(),
                    "timezone": "Europe/Sofia",
                    "limit": page_size,
                    "offset": offset,
                },
            )

            if not isinstance(rows, list) or not rows:
                break

            for raw in rows:
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
                seen.add(fid)
                all_matches.append(m)

            # A short page is the documented end of the result set.
            if len(rows) < page_size:
                break

            offset += page_size

            # Hard safety guard against a broken API returning the same full page forever.
            if offset > 5000:
                print("SCANNER FIXTURE PAGINATION SAFETY STOP:", day.isoformat())
                break

    all_matches.sort(key=lambda x: (x.get("fixture") or {}).get("date", ""))
    print("SCANNER FIXTURES COMPLETE:", len(all_matches), "window", start_bg, "->", end_bg)
    return all_matches

def _persist_team_season_history(team_id, season, league_id, matches):
    """Persist scanner history so PREMATCH/Builder can reuse it without API calls."""
    try:
        conn = _db()
        row = conn.execute(
            "SELECT data FROM scanner_team_season_history WHERE team_id=? AND season=?",
            (int(team_id), int(season)),
        ).fetchone()
        payload = {}
        if row:
            try:
                obj = json.loads(row[0])
                if isinstance(obj, dict):
                    payload = obj
            except Exception:
                payload = {}
        payload[str(int(league_id or 0))] = matches
        conn.execute(
            "INSERT OR REPLACE INTO scanner_team_season_history(team_id, season, data, updated_at) VALUES (?, ?, ?, ?)",
            (int(team_id), int(season), json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print("SCANNER HISTORY CACHE WRITE ERROR:", team_id, season, repr(exc))


def get_team_history(team_id, season, league_id=None):
    team_id, season, league_id = int(team_id), int(season), int(league_id or 0)
    key = (team_id, season, league_id)
    if key in _SCAN_HISTORY:
        return _SCAN_HISTORY[key]

    def fetch_team_matches(extra):
        """Fetch all available pages for the team's current-season history."""
        all_rows = []
        offset = 0
        page_size = 100

        while True:
            rows = _api("matches", {**extra, "limit": page_size, "offset": offset})
            if not isinstance(rows, list) or not rows:
                break

            all_rows.extend(rows)
            if len(rows) < page_size:
                break

            offset += page_size

            # Safety guard against a broken API repeatedly returning the same page.
            if offset > 1000:
                break

        normalized = []
        for raw in all_rows:
            match = _normalize_match(raw)
            if match:
                normalized.append(match)
        return normalized

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

    # Keep every completed fixture available for the requested season.
    # Do not silently truncate the season to the latest 12 matches.
    primary.sort(key=lambda f: (f.get("fixture") or {}).get("date", ""))
    _SCAN_HISTORY[key] = primary
    _persist_team_season_history(team_id, season, league_id, primary)
    print("HISTORY:", team_id, "matches=", len(primary), "cached=", len(primary))
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
    """Extract real per-team match statistics from Highlightly.

    Highlightly's Football Statistics endpoint returns statistic labels as
    ``displayName`` (and in some payloads ``name``), not only ``type``.
    The old parser looked exclusively at ``type`` and therefore discarded
    corners, shots and cards while goals still worked because goals come
    directly from the fixture score.

    Missing statistics are kept missing; they are never converted to zero.
    """
    fid = (fixture.get("fixture") or {}).get("id")
    out = {}

    for block in fixture.get("statistics") or []:
        if not isinstance(block, dict):
            continue

        team = block.get("team") or {}
        tid = team.get("id") if isinstance(team, dict) else block.get("teamId")
        if not tid:
            continue

        vals = {}
        for item in block.get("statistics") or []:
            if not isinstance(item, dict):
                continue

            raw_type = (
                item.get("displayName")
                or item.get("name")
                or item.get("type")
                or item.get("statType")
                or ""
            )
            typ = " ".join(str(raw_type).strip().casefold().replace("_", " ").split())

            val = item.get("value")
            if isinstance(val, str):
                val = val.replace("%", "").strip()

            try:
                val = float(val) if val is not None else None
            except (TypeError, ValueError):
                val = None

            if val is not None:
                vals[typ] = val

        if vals:
            out[int(tid)] = vals

    # Goals are always available in the fixture itself.
    home_id = (fixture.get("teams") or {}).get("home", {}).get("id")
    away_id = (fixture.get("teams") or {}).get("away", {}).get("id")
    goals = fixture.get("goals") or {}

    if home_id:
        out.setdefault(int(home_id), {})["goals_scored"] = float(goals.get("home") or 0)
        out.setdefault(int(home_id), {})["goals_conceded"] = float(goals.get("away") or 0)
    if away_id:
        out.setdefault(int(away_id), {})["goals_scored"] = float(goals.get("away") or 0)
        out.setdefault(int(away_id), {})["goals_conceded"] = float(goals.get("home") or 0)

    return fid, out


def load_historical_statistics(all_histories):
    """Load historical match statistics with persistent caching and fallback."""
    fixtures_by_id = {}
    for history in all_histories:
        for f in history:
            fid = (f.get("fixture") or {}).get("id")
            if fid:
                fixtures_by_id[int(fid)] = f

    loaded, cached, fetched, fallback = {}, 0, 0, 0
    stat_keys = ("corner kicks", "corners", "corner kicks won", "total shots", "shots", "yellow cards", "yellow card", "cards")
    for fid, base_fixture in fixtures_by_id.items():
        cached_data = _read_cached_stat(fid)
        if isinstance(cached_data, dict) and cached_data:
            has_real_stat = any(
                any(k in team_data for k in stat_keys)
                for team_data in cached_data.values()
                if isinstance(team_data, dict)
            )
            if has_real_stat:
                loaded[fid] = cached_data
                cached += 1
                continue
            # Old cache entries may contain only goals. They are not enough
            # for corners/shots/cards, so refresh them once.

        rows = _api(f"statistics/{fid}", {})
        data = {}
        if isinstance(rows, list):
            fake = dict(base_fixture); fake["statistics"] = rows
            _, data = _fixture_market_values(fake)

        if not any(any(k in td for k in stat_keys) for td in data.values()):
            detail = _api(f"matches/{fid}", {})
            if isinstance(detail, list) and detail and isinstance(detail[0], dict):
                fake = dict(base_fixture); fake.update(detail[0])
                if isinstance(detail[0].get("statistics"), list):
                    fake["statistics"] = detail[0]["statistics"]
                _, detail_data = _fixture_market_values(fake)
                if detail_data:
                    data = detail_data; fallback += 1

        if data:
            _write_cached_stat(fid, data); fetched += 1
        loaded[fid] = data

    _SCAN_FIXTURE_STATS.clear(); _SCAN_FIXTURE_STATS.update(loaded)
    print("SCANNER STATS LOAD:", f"fixtures={len(fixtures_by_id)}", f"cached={cached}", f"fetched={fetched}", f"detail_fallback={fallback}", f"with_data={sum(1 for v in loaded.values() if v)}")
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

            # Highlightly labels are normalized in _fixture_market_values().
            # Accept the documented names plus common provider variants.
            mappings={
                "corners": ("corner kicks", "corners", "corner kicks won"),
                "shots": ("total shots", "shots"),
                "cards": ("yellow cards", "yellow card", "cards"),
                "goals_scored": ("goals_scored",),
                "goals_conceded": ("goals_conceded",),
            }

            for market, stat_names in mappings.items():
                value = None
                for stat_name in stat_names:
                    if stat_name in data:
                        value = data.get(stat_name)
                        break
                if value is None:
                    continue
                sums[market] += float(value)
                counts[market] += 1

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

    def _display_team_name(team):
        if not isinstance(team, dict):
            return ""
        for key in ("displayName", "fullName", "longName", "teamDisplayName", "teamName", "name", "shortName"):
            value = team.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    return {
        "fixture_id": fixture["fixture"]["id"],
        "home_name": _display_team_name(home),
        "away_name": _display_team_name(away),
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


def format_market(results, key, label, emoji, max_items=None):
    valid = [
        r for r in results
        if key in r["markets"] and _market_allowed_on_betano(r, key)
    ]

    if max_items is None:
        max_items = 5 if key == "goals" else 3
    high = sorted(
        valid,
        key=lambda r: r["markets"][key]["expected"],
        reverse=True,
    )[:max_items]
    low = sorted(
        valid,
        key=lambda r: r["markets"][key]["expected"],
    )[:max_items]

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
    """Check Betano availability without falsely rejecting fixtures.

    Highlightly's football odds endpoint supports Total Goals and several
    result markets, but it does not expose corner/card/shot markets as
    documented odds markets. Therefore exact Betano availability can be
    verified for goals, while corners/cards/shots are gated by the presence
    of a real Betano prematch feed for the fixture.
    """
    result = {
        "match": False,
        "goals": False,
        "corners": False,
        "shots": False,
        "cards": False,
    }
    try:
        rows = _api(
            "odds",
            {
                "matchId": int(fixture_id),
                "bookmakerName": "Betano",
                "oddsType": "prematch",
                "limit": 5,
                "offset": 0,
            },
        )
    except Exception as exc:
        print("BETANO FILTER ERROR:", fixture_id, repr(exc))
        return result

    market_names = []
    for row in rows if isinstance(rows, list) else []:
        for market in row.get("odds", []) or []:
            bookmaker_name = str(market.get("bookmakerName") or "").casefold()
            bookmaker_id = market.get("bookmakerId")
            # The query already asks for Betano, but verify the returned
            # bookmaker so another provider can never be accepted by mistake.
            if bookmaker_name and "betano" not in bookmaker_name:
                continue
            if not bookmaker_name and bookmaker_id is not None and int(bookmaker_id or 0) != BETANO_BOOKMAKER_ID:
                continue

            result["match"] = True
            name = str(
                market.get("market")
                or market.get("name")
                or market.get("marketName")
                or ""
            ).casefold()
            if name:
                market_names.append(name)

            if (
                "total goals" in name
                or "total goal" in name
                or "over/under goals" in name
            ):
                result["goals"] = True
            if "corner" in name:
                result["corners"] = True
            if "shot" in name:
                result["shots"] = True
            if "card" in name or "booking" in name:
                result["cards"] = True

    print("BETANO FILTER RESULT:", fixture_id, result, "MARKETS=", market_names)
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

    # One football daily scan: sent at 10:00 BG.
    # Fixture window is always 12:00 BG -> next day 12:00 BG.
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    title = "10:00 ДНЕВЕН СКЕНЕР"

    run_key = f"football_daily:{ref.isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SCANNER SKIP: already ran {ref.isoformat()}"))
        return ""
    if _quota_locked("football"):
        print(_signal_text("SCANNER SKIP: football quota locked for today"))
        mark_ran(run_key)
        return ""

    try:
        matches = get_fixtures_for_window(start, end)
        # Publish the exact morning fixture set for PREMATCH / Bet Builder.
        # They reuse this list and never issue a second fixture-list request.
        _UPCOMING_FIXTURES_CACHE.clear()
        _UPCOMING_FIXTURES_CACHE[(start.isoformat(), end.isoformat())] = list(matches)
        print("SCANNER MORNING FIXTURE CACHE SET:", len(matches), start, "->", end)
    except APIQuotaExceeded:
        mark_ran(run_key)
        raise
    print(_signal_text(f"SCANNER {mode.upper()}: {len(matches)} upcoming fixtures"))

    # REAL BETANO MATCH FILTER
    matches = filter_matches_by_betano_markets(matches)

    # Exclude blocked countries.
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

    # Diagnostic coverage: tells us immediately whether Highlightly statistics
    # are being parsed instead of silently producing empty markets.
    stat_counts = {"corners": 0, "shots": 0, "cards": 0}
    for fixture_data in stats_by_fixture.values():
        for team_data in fixture_data.values():
            if "corner kicks" in team_data or "corners" in team_data:
                stat_counts["corners"] += 1
            if "total shots" in team_data or "shots" in team_data:
                stat_counts["shots"] += 1
            if "yellow cards" in team_data or "yellow card" in team_data or "cards" in team_data:
                stat_counts["cards"] += 1
    print("SCANNER STAT COVERAGE:", stat_counts)
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
            # Highlightly exposes exact Betano Total Goals availability.
            # For corners/cards/shots, its football odds endpoint does not
            # expose those market types, so requiring betano[k] would wrongly
            # turn valid statistical markets into zero candidates. Keep the
            # fixture-level Betano gate for those statistics.
            gated = {}
            for k, v in result.get("markets", {}).items():
                if k == "goals":
                    if betano.get("goals") is True:
                        gated[k] = v
                elif betano.get("match") is True:
                    gated[k] = v
            result["markets"] = gated
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

