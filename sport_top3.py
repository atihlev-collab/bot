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
SPORT_HISTORY_FROM = "2025-07-01"

SPORTS_CONFIG = {
    "basketball": {
        "name": "🏀 БАСКЕТБОЛ",
        "endpoint": "basketball/matches",
        "stats": "basketball/teams/statistics",
        "teams": "basketball/teams",
        "metric": "points"
    },
    "hockey": {
        "name": "🏒 ХОКЕЙ",
        "endpoint": "hockey/matches",
        "stats": "hockey/teams/statistics",
        "teams": "hockey/teams",
        "metric": "goals"
    },
    "american-football": {
        "name": "🏈 NFL / NCAA — Division I / Division II",
        "endpoint": "american-football/matches",
        "stats": "american-football/teams/statistics",
        "teams": "american-football/teams",
        "metric": "points"
    },
    "baseball": {
        "name": "⚾ БЕЙЗБОЛ",
        "endpoint": "baseball/matches",
        "stats": "baseball/teams/statistics",
        "teams": "baseball/teams",
        "metric": "runs"
    },
    "rugby": {
        "name": "🏉 РЪГБИ",
        "endpoint": "rugby/matches",
        "stats": "rugby/teams/statistics",
        "teams": "rugby/teams",
        "metric": "points"
    },
    "volleyball": {
        "name": "🏐 ВОЛЕЙБОЛ",
        "endpoint": "volleyball/matches",
        "stats": "volleyball/teams/statistics",
        "teams": "volleyball/teams",
        "metric": "points"
    },
    "handball": {
        "name": "🤾 ХАНДБАЛ",
        "endpoint": "handball/matches",
        "stats": "handball/teams/statistics",
        "teams": "handball/teams",
        "metric": "goals"
    },
}
_SPORT_API_CALLS = 0
_SPORT_STATS_CACHE = {}


def _sport_api_get(endpoint, params=None):
    """Call Highlightly Sport Ultra and return its data without inventing values."""
    global _SPORT_API_CALLS
    _SPORT_API_CALLS += 1

    try:
        from config import HIGHLIGHTLY_API_KEY
        headers = {
            "x-rapidapi-key": HIGHLIGHTLY_API_KEY,
            "x-rapidapi-host": SPORT_API_HOST,
        }
        response = requests.get(
            f"{SPORT_API_BASE}/{endpoint}",
            headers=headers,
            params=params or {},
            timeout=25,
        )
        print(
            f"SPORT API REQUEST {_SPORT_API_CALLS}: {endpoint} "
            f"params={params or {}} status={response.status_code}"
        )
        if response.status_code == 429:
            _set_quota_lock("sport")
            print("SPORT QUOTA LOCKED: Highlightly Sport returned HTTP 429")
            raise APIQuotaExceeded("Highlightly Sport daily quota exhausted")
        
        if response.status_code != 200:
            print("SPORT API ERROR:", response.text[:500])
            return []

        payload = response.json()
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload

        if data is None:
            return []
        return data
    except Exception as exc:
        print("SPORT API REQUEST ERROR:", endpoint, repr(exc))
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

    for key in (
        "displayName",
        "fullName",
        "longName",
        "teamDisplayName",
        "teamName",
        "name",
        "shortName",
    ):
        value = team.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    return "Unknown"


_SPORT_TEAM_NAMES = {}


def _get_full_sport_team_name(sport_key, team):
    """Resolve full team name from match data or team endpoint."""

    if not isinstance(team, dict):
        return str(team or "Unknown")

    team_id = _sport_team_id(team)

    # 1. Вече имаме пълно име в самия match response
    for key in (
        "displayName",
        "fullName",
        "longName",
        "teamDisplayName",
        "teamName",
    ):
        value = team.get(key)
        if value is not None and str(value).strip():
            name = str(value).strip()
            if team_id:
                _SPORT_TEAM_NAMES[(sport_key, team_id)] = name
            return name

    # 2. Вече сме го намерили по-рано
    if team_id:
        cached = _SPORT_TEAM_NAMES.get((sport_key, team_id))
        if cached:
            return cached

    # 3. Вземаме пълното име от /teams/{id}
    if team_id:
        try:
            cfg = SPORTS_CONFIG.get(sport_key, {})
            endpoint = cfg.get("teams")

            if endpoint:
                rows = _sport_api_get(
                    f"{endpoint}/{int(team_id)}",
                    {}
                )

                candidates = rows if isinstance(rows, list) else [rows]

                for row in candidates:
                    if not isinstance(row, dict):
                        continue

                    for key in (
                        "displayName",
                        "fullName",
                        "longName",
                        "teamDisplayName",
                        "teamName",
                        "name",
                    ):
                        value = row.get(key)
                        if value is not None and str(value).strip():
                            name = str(value).strip()
                            _SPORT_TEAM_NAMES[(sport_key, team_id)] = name
                            return name

        except Exception as exc:
            print(
                "SPORT TEAM NAME ERROR:",
                sport_key,
                team_id,
                repr(exc)
            )

    # 4. Последен fallback
    return str(
        team.get("name")
        or team.get("shortName")
        or "Unknown"
    )


def _sport_match_names(match, sport_key=None):
    home = match.get("homeTeam") or match.get("home") or {}
    away = match.get("awayTeam") or match.get("away") or {}

    if sport_key:
        return (
            _get_full_sport_team_name(sport_key, home),
            _get_full_sport_team_name(sport_key, away),
        )

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
    rows = _sport_api_get(
        f"{cfg['stats']}/{int(team_id)}",
        {"fromDate": SPORT_HISTORY_FROM, "timezone": SPORT_API_TZ},
    )
    result = _extract_team_average(rows, metric)
    _SPORT_STATS_CACHE[cache_key] = result

    if result:
        print(
            f"SPORT HISTORY: {sport_key} team={team_id} "
            f"average={result['average']:.2f} games={result['games']}"
        )
    else:
        print(f"SPORT HISTORY: {sport_key} team={team_id} NO DATA")
    return result




def _volleyball_set_points(score):
    """Return total rally points for one team from completed volleyball set scores."""
    if not isinstance(score, dict):
        return None

    totals = {"home": 0, "away": 0}
    found = 0

    for key, value in score.items():
        name = str(key).casefold()
        if "set" not in name or name == "current":
            continue

        home_points = away_points = None
        if isinstance(value, str) and "-" in value:
            parts = value.split("-", 1)
            try:
                home_points = int(parts[0].strip())
                away_points = int(parts[1].strip())
            except (TypeError, ValueError):
                continue
        elif isinstance(value, dict):
            try:
                home_points = int(
                    value.get("home")
                    or value.get("homeTeam")
                    or value.get("homePoints")
                )
                away_points = int(
                    value.get("away")
                    or value.get("awayTeam")
                    or value.get("awayPoints")
                )
            except (TypeError, ValueError):
                continue

        if home_points is None or away_points is None:
            continue

        totals["home"] += home_points
        totals["away"] += away_points
        found += 1

    if found == 0:
        return None

    return totals["home"], totals["away"]


def _get_volleyball_team_average(team_id, league_id=None, season=None):
    """
    Volleyball uses TOTAL RALLY POINTS, not sets won.

    Highlightly's volleyball team-statistics endpoint exposes a "points"
    field that is not the same thing as total rally points scored in all
    sets. Therefore the scanner derives the average from completed
    current-season matches and their set-by-set scores.
    """
    if not team_id:
        return None

    cache_key = ("volleyball_total_points", int(team_id), int(league_id or 0), int(season or 0))
    if cache_key in _SPORT_STATS_CACHE:
        return _SPORT_STATS_CACHE[cache_key]

    total_points = 0
    games = 0
    seen = set()

    base = {"season": int(season)} if season else {}
    if league_id:
        base["leagueId"] = int(league_id)

    for side_key in ("homeTeamId", "awayTeamId"):
        offset = 0
        while True:
            params = dict(base)
            params[side_key] = int(team_id)
            params["limit"] = SPORT_API_LIMIT
            params["offset"] = offset

            rows = _sport_api_get("volleyball/matches", params)
            if not isinstance(rows, list) or not rows:
                break

            for match in rows:
                if not isinstance(match, dict):
                    continue

                mid = match.get("id") or match.get("matchId")
                if mid is not None and mid in seen:
                    continue

                dt = _sport_match_datetime(match)
                if dt is None or dt >= datetime.now(TZ):
                    continue

                league = match.get("league") or {}
                if isinstance(league, dict):
                    if league_id and league.get("id") and int(league.get("id")) != int(league_id):
                        continue
                    if season and league.get("season") and int(league.get("season")) != int(season):
                        continue

                state = match.get("state") or {}
                description = str(state.get("description") or "").casefold()
                if description and not any(
                    marker in description
                    for marker in ("finished", "final", "ended", "completed")
                ):
                    continue

                score = state.get("score") or {}
                points = _volleyball_set_points(score)
                if points is None:
                    continue

                home = match.get("homeTeam") or match.get("home") or {}
                away = match.get("awayTeam") or match.get("away") or {}
                home_id = _sport_team_id(home)
                away_id = _sport_team_id(away)

                if home_id == int(team_id):
                    scored = points[0]
                elif away_id == int(team_id):
                    scored = points[1]
                else:
                    continue

                if mid is not None:
                    seen.add(mid)

                total_points += scored
                games += 1

            if len(rows) < SPORT_API_LIMIT:
                break
            offset += SPORT_API_LIMIT
            if offset > 2000:
                break

    result = None
    if games >= 1:
        result = {
            "average": total_points / games,
            "games": games,
            "season": int(season) if season else None,
            "raw": {"total_rally_points": total_points},
        }

    _SPORT_STATS_CACHE[cache_key] = result

    if result:
        print(
            f"SPORT HISTORY: volleyball team={team_id} "
            f"total-points-average={result['average']:.2f} games={result['games']}"
        )
    else:
        print(f"SPORT HISTORY: volleyball team={team_id} TOTAL POINTS NO DATA")

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
        rows = _sport_api_get(cfg["endpoint"], params)

        # Some Sport Ultra responses can be empty when timezone is combined with date.
        # Retry the same date without timezone; results are still filtered locally in BG time.
        if not isinstance(rows, list) or not rows:
            fallback = _sport_api_get(
                cfg["endpoint"],
                {"date": day.isoformat(), "limit": SPORT_API_LIMIT},
            )
            if isinstance(fallback, list):
                rows = fallback

        all_rows.extend(rows if isinstance(rows, list) else [])

    unique = {}
    for match in all_rows:
        if not isinstance(match, dict):
            continue
        dt = _sport_match_datetime(match)
        if dt is None or not (start <= dt < end):
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


def _poisson_cdf(k, lam):
    if k < 0 or lam < 0:
        return 0.0
    term = math.exp(-lam)
    total = term
    for i in range(1, int(k) + 1):
        term *= lam / i
        total += term
    return max(0.0, min(1.0, total))


def _sport_probability(expected, side):
    """Probability for a total-points/goals market from the statistical mean."""
    if expected is None or expected <= 0:
        return None, None
    if side == "over":
        line = max(0.5, math.floor(expected * 2 - 1) / 2)
        prob = 1.0 - _poisson_cdf(math.floor(line), expected)
        market = f"OVER {line:.1f}"
    else:
        line = max(0.5, math.ceil(expected * 2 - 1) / 2)
        prob = _poisson_cdf(math.ceil(line - 1e-9) - 1, expected)
        market = f"UNDER {line:.1f}"
    return market, round(prob * 100, 1)


def _sport_score_pair(match, metric):
    """Extract completed-match home/away score without inventing values."""
    if not isinstance(match, dict):
        return None
    state = match.get("state") or {}
    score = state.get("score") or match.get("score") or {}
    if not isinstance(score, dict):
        return None

    candidates = [score]
    for key in ("current", "final", "fullTime", "fulltime", "total"):
        value = score.get(key)
        if isinstance(value, dict):
            candidates.insert(0, value)

    metric_names = {
        "goals": ("goals", "score", "points"),
        "points": ("points", "score", "goals"),
        "runs": ("runs", "score", "points"),
    }.get(metric, ("score", "points", "goals"))

    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        for hkey in ("home", "homeScore", "homePoints", "homeGoals", "homeRuns"):
            for akey in ("away", "awayScore", "awayPoints", "awayGoals", "awayRuns"):
                hv, av = obj.get(hkey), obj.get(akey)
                if isinstance(hv, (int, float)) and isinstance(av, (int, float)):
                    return float(hv), float(av)
        # Some responses nest the score by metric.
        for key in metric_names:
            value = obj.get(key)
            if isinstance(value, dict):
                hv = value.get("home") or value.get("homeScore") or value.get("homePoints")
                av = value.get("away") or value.get("awayScore") or value.get("awayPoints")
                if isinstance(hv, (int, float)) and isinstance(av, (int, float)):
                    return float(hv), float(av)
    return None


def _sport_team_context(sport_key, team_id, league_id, season, metric, last_n=5):
    """Current-season form/context for one team, based only on completed matches."""
    if not team_id:
        return None
    cache_key = ("context", sport_key, int(team_id), int(league_id or 0), int(season or 0), metric, last_n)
    cached = _SPORT_STATS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    cfg = SPORTS_CONFIG[sport_key]
    rows = []
    offset = 0
    while True:
        params = {"teamId": int(team_id), "limit": SPORT_API_LIMIT, "offset": offset}
        if season:
            params["season"] = int(season)
        if league_id:
            params["leagueId"] = int(league_id)
        batch = _sport_api_get(cfg["endpoint"], params)
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < SPORT_API_LIMIT:
            break
        offset += SPORT_API_LIMIT
        if offset > 2000:
            break

    completed = []
    for match in rows:
        if not isinstance(match, dict):
            continue
        dt = _sport_match_datetime(match)
        if dt is None or dt >= datetime.now(TZ):
            continue
        league = match.get("league") or {}
        if isinstance(league, dict):
            if league_id and league.get("id") and int(league.get("id")) != int(league_id):
                continue
            if season and league.get("season") and int(league.get("season")) != int(season):
                continue
        pair = _sport_score_pair(match, metric)
        if pair is None:
            continue
        home = match.get("homeTeam") or match.get("home") or {}
        away = match.get("awayTeam") or match.get("away") or {}
        hid, aid = _sport_team_id(home), _sport_team_id(away)
        if hid == int(team_id):
            scored, conceded = pair
            is_home = True
        elif aid == int(team_id):
            scored, conceded = pair[1], pair[0]
            is_home = False
        else:
            continue
        if scored > conceded:
            result, points = "W", 3
        elif scored < conceded:
            result, points = "L", 0
        else:
            result, points = "D", 1
        completed.append({"dt": dt, "scored": scored, "conceded": conceded, "result": result, "points": points, "home": is_home})

    completed.sort(key=lambda x: x["dt"], reverse=True)
    last = completed[:last_n]
    result = {
        "games": len(completed),
        "form": "".join(x["result"] for x in reversed(last)),
        "form_points": sum(x["points"] for x in last),
        "last_games": len(last),
        "last_scored": (sum(x["scored"] for x in last) / len(last)) if last else None,
        "last_conceded": (sum(x["conceded"] for x in last) / len(last)) if last else None,
        "home_points": sum(x["points"] for x in completed if x["home"]),
        "away_points": sum(x["points"] for x in completed if not x["home"]),
        "all_points": sum(x["points"] for x in completed),
    }
    _SPORT_STATS_CACHE[cache_key] = result
    return result


def _sport_winner_probability(home_avg, away_avg, home_ctx=None, away_ctx=None):
    """Winner model using attack, recent form, defence and home/away context."""
    if home_avg is None or away_avg is None or home_avg <= 0 or away_avg <= 0:
        return None

    # Base attack signal. Unlike the old model, this is only one component.
    total = home_avg + away_avg
    home_attack = home_avg / total

    # Recent form: last 5 completed matches.
    if home_ctx and away_ctx and home_ctx["last_games"] >= 3 and away_ctx["last_games"] >= 3:
        hf = home_ctx["form_points"] / (3 * home_ctx["last_games"])
        af = away_ctx["form_points"] / (3 * away_ctx["last_games"])
        form_edge = hf - af

        # Recent scoring/defence edge.
        h_recent_diff = (home_ctx["last_scored"] or 0) - (home_ctx["last_conceded"] or 0)
        a_recent_diff = (away_ctx["last_scored"] or 0) - (away_ctx["last_conceded"] or 0)
        diff_scale = max(1.0, (home_avg + away_avg) / 4.0)
        recent_edge = max(-1.0, min(1.0, (h_recent_diff - a_recent_diff) / diff_scale))

        # Home advantage is based on the team's actual home/away record, not a fixed winner.
        h_home_rate = home_ctx["home_points"] / max(1, 3 * sum(1 for _ in range(1))) if False else None
        # Use the observed home/away points only as a small directional signal.
        home_split = home_ctx["home_points"] / max(1, home_ctx["games"])
        away_split = away_ctx["away_points"] / max(1, away_ctx["games"])
        split_edge = max(-1.0, min(1.0, (home_split - away_split) / 3.0))

        score = (home_attack - 0.5) * 0.45 + form_edge * 0.30 + recent_edge * 0.15 + split_edge * 0.10
    else:
        score = (home_attack - 0.5) * 0.45

    # Convert the edge into a bounded two-way model probability.
    home_p = max(0.05, min(0.95, 0.5 + score))
    away_p = 1.0 - home_p
    return round(home_p * 100, 1), round(away_p * 100, 1), 0.0


def _sport_market_candidates(home_avg, away_avg, home_ctx=None, away_ctx=None):
    """Return winner + total markets using the richer statistical winner model."""
    total = home_avg + away_avg
    markets = []

    win = _sport_winner_probability(home_avg, away_avg, home_ctx, away_ctx)
    if win:
        hp, ap, _ = win
        if hp >= ap:
            markets.append(("ПОБЕДИТЕЛ: HOME", hp))
        else:
            markets.append(("ПОБЕДИТЕЛ: AWAY", ap))

    over_market, over_prob = _sport_probability(total, "over")
    under_market, under_prob = _sport_probability(total, "under")
    if over_market and under_market:
        if over_prob >= under_prob:
            markets.append((over_market, over_prob))
        else:
            markets.append((under_market, under_prob))

    return markets[:2]


def _format_sport_entry(index, item):
    home, away = _sport_match_names(item["match"])
    league, country = _sport_league_country(item["match"])
    dt = item["datetime"]
    lines = [
        f"{index}. {home} - {away}",
        "   📊 СТАТИСТИКА",
        f"   {home}: средно {item['home_avg']:.2f} | мачове: {item['home_games']}",
        f"   {away}: средно {item['away_avg']:.2f} | мачове: {item['away_games']}",
        f"   📈 Очаквано общо: {item['expected']:.2f}",
    ]
    for market, probability in item["markets"]:
        lines.append(f"   🎯 Пазар: {market}")
        lines.append(f"   📊 Вероятност: {probability:.1f}%")
    lines.extend([
        f"   Лига: {league or '-'}",
        f"   Държава: {country or '-'}",
        f"   Дата: {dt.strftime('%d.%m.%Y')}",
        f"   Начало: {dt.strftime('%H:%M')} BG",
    ])
    return "\n".join(lines)


def _build_sport_section(sport_name, candidates):
    if not candidates:
        return f"{sport_name}\nНяма достатъчно исторически статистически данни."

    # Top 4, but never invent a fourth entry when fewer are valid.
    top_over = sorted(candidates, key=lambda x: x["expected"], reverse=True)[:4]
    top_under = sorted(candidates, key=lambda x: x["expected"])[:4]

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
    """Build Top 4 Over/Under from real current-season team statistics."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    run_key = f"sport_daily:{now_bg.date().isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT DAILY SCANNER ALREADY RAN: {now_bg.date().isoformat()}"))
        return ""
    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = [
        "📊 DAILY STATISTICAL SCANNER — СИГНАЛИ",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: всички налични текущо-сезонни team statistics; волейбол — общи rally points от завършените мачове",
        "",
    ]

    for sport_key, cfg in SPORTS_CONFIG.items():
        print(f"SPORT SCAN: {sport_key} — FIXTURES")
        fixtures = _get_sport_fixtures(cfg, start, end)


        # GLOBAL BLOCK — Russia / Belarus
        filtered_fixtures = []

        for match in fixtures:
            league = match.get("league") or {}

            country = ""

            if isinstance(league, dict):
                country = (
                    league.get("country")
                    or league.get("countryName")
                    or ""
                )

                if isinstance(country, dict):
                    country = (
                        country.get("name")
                        or country.get("countryName")
                        or ""
                    )

            if not country:
                country = (
                    match.get("country")
                    or match.get("countryName")
                    or ""
                )

                if isinstance(country, dict):
                    country = (
                        country.get("name")
                        or country.get("countryName")
                        or ""
                    )

            country = str(country).strip().casefold()

            if country in {"russia", "belarus"}:
                print(
                    f"SPORT BLOCKED COUNTRY: "
                    f"{match.get('id')} — {country}"
                )
                continue

            filtered_fixtures.append(match)

        fixtures = filtered_fixtures
        
        
        candidates = []

        for match in fixtures:
            home = match.get("homeTeam") or match.get("home") or {}
            away = match.get("awayTeam") or match.get("away") or {}
            home_id = _sport_team_id(home)
            away_id = _sport_team_id(away)
            if not home_id or not away_id:
                continue

            if sport_key == "volleyball":
                league = match.get("league") or {}
                league_id = league.get("id") if isinstance(league, dict) else None
                season = league.get("season") if isinstance(league, dict) else None
                h = _get_volleyball_team_average(home_id, league_id, season)
                a = _get_volleyball_team_average(away_id, league_id, season)
            else:
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
    mark_ran(run_key)
    return message

# =========================================================
# NEW SPORT TOP 3 — ADDITIONAL BLOCK
# =========================================================

def _build_sport_top3_section(sport_name, candidates):
    if not candidates:
        return f"{sport_name}\nНяма достатъчно исторически статистически данни."

    ranked = sorted(
        candidates,
        key=lambda x: max((p for _m, p in x["markets"]), default=0.0),
        reverse=True,
    )[:3]

    lines = [sport_name, "", "🏆 TOP 3"]
    for i, item in enumerate(ranked, 1):
        lines.append(_format_sport_entry(i, item))
        if i < len(ranked):
            lines.append("")
    return "\n".join(lines)

def run_sport_top3_daily_scanner(send_func=None):
    """Build Top 3 for every configured sport in the 12:00→12:00 window."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    run_key = f"sport_top3:{now_bg.date().isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT DAILY SCANNER ALREADY RAN: {now_bg.date().isoformat()}"))
        return ""
    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = [
        "📊 DAILY STATISTICAL SCANNER — СИГНАЛИ",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: всички налични текущо-сезонни team statistics; волейбол — общи rally points от завършените мачове",
        "",
    ]

    for sport_key, cfg in SPORTS_CONFIG.items():
        print(f"SPORT SCAN: {sport_key} — FIXTURES")
        fixtures = _get_sport_fixtures(cfg, start, end)


        # GLOBAL BLOCK — Russia / Belarus
        filtered_fixtures = []

        for match in fixtures:
            league = match.get("league") or {}

            country = ""

            if isinstance(league, dict):
                country = (
                    league.get("country")
                    or league.get("countryName")
                    or ""
                )

                if isinstance(country, dict):
                    country = (
                        country.get("name")
                        or country.get("countryName")
                        or ""
                    )

            if not country:
                country = (
                    match.get("country")
                    or match.get("countryName")
                    or ""
                )

                if isinstance(country, dict):
                    country = (
                        country.get("name")
                        or country.get("countryName")
                        or ""
                    )

            country = str(country).strip().casefold()
            league_name, _ = _sport_league_country(match)
            league_name_cf = str(league_name or "").strip().casefold()

            if "friendly" in league_name_cf or "club friendly" in league_name_cf:
                print(f"SPORT BLOCKED FRIENDLY: {match.get('id')} — {league_name}")
                continue

            if country in {"russia", "belarus"}:
                print(
                    f"SPORT BLOCKED COUNTRY: "
                    f"{match.get('id')} — {country}"
                )
                continue

            filtered_fixtures.append(match)

        fixtures = filtered_fixtures
        
        
        candidates = []

        for match in fixtures:
            home = match.get("homeTeam") or match.get("home") or {}
            away = match.get("awayTeam") or match.get("away") or {}
            home_id = _sport_team_id(home)
            away_id = _sport_team_id(away)
            if not home_id or not away_id:
                continue

            if sport_key == "volleyball":
                league = match.get("league") or {}
                league_id = league.get("id") if isinstance(league, dict) else None
                season = league.get("season") if isinstance(league, dict) else None
                h = _get_volleyball_team_average(home_id, league_id, season)
                a = _get_volleyball_team_average(away_id, league_id, season)
            else:
                h = _get_team_average(sport_key, home_id, cfg["metric"])
                a = _get_team_average(sport_key, away_id, cfg["metric"])
            if not h or not a or h["games"] < 3 or a["games"] < 3:
                continue

            dt = _sport_match_datetime(match)
            if not dt:
                continue

            expected = h["average"] + a["average"]

            league = match.get("league") or {}
            league_id = league.get("id") if isinstance(league, dict) else None
            season = league.get("season") if isinstance(league, dict) else None
            home_ctx = _sport_team_context(sport_key, home_id, league_id, season, cfg["metric"], last_n=5)
            away_ctx = _sport_team_context(sport_key, away_id, league_id, season, cfg["metric"], last_n=5)
            markets = _sport_market_candidates(
                h["average"], a["average"], home_ctx, away_ctx
            )
            if len(markets) < 2:
                continue

            candidates.append({
                "match": match,
                "datetime": dt,
                "home_avg": h["average"],
                "away_avg": a["average"],
                "home_games": h["games"],
                "away_games": a["games"],
                "expected": expected,
                "markets": markets,
                "home_context": home_ctx,
                "away_context": away_ctx,
            })

        lines.append(_build_sport_top3_section(cfg["name"], candidates))
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
    mark_ran(run_key)
    return message

# =========================================================
# DAILY SCAN SCHEDULER — SPORT ONLY
# =========================================================


def run_due_scans(send_func):
    """Run football and Sport Statistics once per day."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # Football: once per day at/after 10:00 BG.
    # The scanner uses the fixed 12:00 -> next-day 12:00 window.
    if now.hour > 10 or (now.hour == 10 and now.minute >= 0):
        football_key = f"football_daily:{today.isoformat()}"

        if not already_ran(football_key):
            print(_signal_text("FOOTBALL DAILY SCANNER STARTED"))
            try:
                run_daily_scanner(
                    mode="day",
                    reference_date=today,
                    send_func=send_func,
                )
                mark_ran(football_key)
                print(_signal_text("FOOTBALL DAILY SCANNER FINISHED"))
            except Exception as exc:
                print(_signal_text(f"FOOTBALL DAILY SCANNER ERROR: {exc!r}"))

    # Sport statistics: once per day at/after 10:00 BG.
    sport_key = f"sport_daily:{today.isoformat()}"
    if now.hour > 10 or (now.hour == 10 and now.minute >= 0):
        if not already_ran(sport_key):
            print(_signal_text("SPORT DAILY SCANNER STARTED"))
            try:
                run_sport_daily_scanner(send_func=send_func)
                mark_ran(sport_key)
                print(_signal_text("SPORT DAILY SCANNER FINISHED"))
            except Exception as exc:
                print(_signal_text(f"SPORT DAILY SCANNER ERROR: {exc!r}"))

    return True
