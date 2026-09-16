# BUILD: HIGHLIGHTLY-FOOTBALL-API-SCANNER-FIX-1
# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs football once daily at/after 10:30 Bulgaria time.
# Fixture window: today 12:00 BG -> tomorrow 12:00 BG.
# Sport Statistics keeps its existing daily scan.
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

            if payload.get("errors"):
                print("SCANNER API ERROR:", endpoint, payload.get("errors"))
                return None

            return payload.get("data", [])

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
    """Get every upcoming football fixture in the exact BG window.

    Highlightly supports date + timezone on /matches.  We request both
    calendar dates touched by the 12:00 -> 12:00 window.  If a timezone-
    scoped request unexpectedly returns no rows, retry that date without the
    timezone parameter and perform the Sofia-time filtering locally.  This
    keeps the scanner from producing a false zero-fixture result when the API
    returns dates in UTC.
    """
    days = []
    d = start_bg.date()
    while d <= end_bg.date():
        days.append(d)
        d += timedelta(days=1)

    all_matches, seen = [], set()
    now_utc = datetime.now(timezone.utc)

    for day in days:
        rows = _api(
            "matches",
            {
                "date": day.isoformat(),
                "timezone": "Europe/Sofia",
                "limit": 100,
            },
        )

        # Defensive fallback: Highlightly documents date+timezone, but if the
        # localized request is empty, retry the same date in API default UTC.
        if not rows:
            print("SCANNER FIXTURE FALLBACK:", day.isoformat(), "UTC query")
            rows = _api(
                "matches",
                {
                    "date": day.isoformat(),
                    "limit": 100,
                },
            )

        print(
            "SCANNER FIXTURES:",
            day.isoformat(),
            "rows=", len(rows) if isinstance(rows, list) else 0,
        )

        for raw in rows if isinstance(rows, list) else []:
            m = _normalize_match(raw)
            if not m:
                continue

            fixture = m.get("fixture") or {}
            fid = fixture.get("id")
            dt_raw = fixture.get("date")
            if not fid or fid in seen or not dt_raw:
                continue

            try:
                dt_utc = datetime.fromisoformat(
                    str(dt_raw).replace("Z", "+00:00")
                )
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_bg = dt_utc.astimezone(TZ)
            except Exception:
                continue

            # Exact Sofia-local 12:00 -> next-day 12:00 window.
            if not (start_bg <= dt_bg < end_bg):
                continue

            # Never include matches already started/finished at scan time.
            if dt_utc <= now_utc:
                continue

            seen.add(fid)
            all_matches.append(m)

    all_matches.sort(
        key=lambda x: (x.get("fixture") or {}).get("date", "")
    )
    print(
        "SCANNER WINDOW RESULT:",
        start_bg.isoformat(),
        "->",
        end_bg.isoformat(),
        "fixtures=", len(all_matches),
    )
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
    )[:3]
    low = sorted(
        valid,
        key=lambda r: r["markets"][key]["expected"],
    )[:3]

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

    # Single football daily scan.  It is sent at/after 10:30 BG and
    # always analyses the next 24-hour fixture window: 12:00 -> 12:00.
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    title = "10:30 ДНЕВЕН СКЕНЕР"

    matches = get_fixtures_for_window(start, end)
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
            result["markets"] = {
                k: v for k, v in result.get("markets", {}).items()
                if k == "goals" or betano.get(k) is True
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
    if send_func:
        send_func(message)
    return message

_START = time.time()


# =========================================================
# SPORT DAILY SCANNER
# 1 API REQUEST PER SPORT / DAY
# =========================================================

SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100

SPORTS_CONFIG = {
    "basketball": {
        "name": "🏀 БАСКЕТБОЛ",
        "endpoint": "basketball/matches",
        "metric": "points",
    },
    "hockey": {
        "name": "🏒 ХОКЕЙ",
        "endpoint": "hockey/matches",
        "metric": "goals",
    },
    "american-football": {
        "name": "🏈 NFL / AMERICAN FOOTBALL",
        "endpoint": "american-football/matches",
        "metric": "points",
    },
    "baseball": {
        "name": "⚾ БЕЙЗБОЛ",
        "endpoint": "baseball/matches",
        "metric": "runs",
    },
    "rugby": {
        "name": "🏉 РЪГБИ",
        "endpoint": "rugby/matches",
        "metric": "points",
    },
    "volleyball": {
        "name": "🏐 ВОЛЕЙБОЛ",
        "endpoint": "volleyball/matches",
        "metric": "points",
    },
    "handball": {
        "name": "🤾 ХАНДБАЛ",
        "endpoint": "handball/matches",
        "metric": "goals",
    },
}


def _sport_api_get_once(endpoint, params):
    """
    EXACTLY ONE HTTP REQUEST.
    No retry.
    No pagination.
    No second request.
    """

    try:
        from config import HIGHLIGHTLY_API_KEY

        headers = {
            "x-rapidapi-key": HIGHLIGHTLY_API_KEY,
        }

        url = f"{SPORT_API_BASE}/{endpoint}"

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )

        if response.status_code != 200:
            print(
                "SPORT API ERROR:",
                endpoint,
                response.status_code,
                response.text[:300],
            )
            return []

        payload = response.json()

        if isinstance(payload, dict):
            data = payload.get("data", [])
        elif isinstance(payload, list):
            data = payload
        else:
            data = []

        if not isinstance(data, list):
            return []

        return data

    except Exception as exc:
        print(
            "SPORT API REQUEST ERROR:",
            endpoint,
            repr(exc),
        )
        return []


def _sport_match_datetime(match):
    """
    Convert API match date to Bulgaria time.
    """

    raw = (
        match.get("date")
        or match.get("startTime")
        or match.get("startDate")
    )

    if not raw:
        return None

    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(TZ)

    except Exception:
        return None


def _sport_team_name(team):
    if not isinstance(team, dict):
        return str(team or "")

    return (
        team.get("name")
        or team.get("displayName")
        or team.get("shortName")
        or "Unknown"
    )


def _sport_match_names(match):
    home = match.get("homeTeam") or match.get("home") or {}
    away = match.get("awayTeam") or match.get("away") or {}

    return (
        _sport_team_name(home),
        _sport_team_name(away),
    )


def _sport_match_score(match):
    """
    Read score directly from the match response.
    Supports several Highlightly-style score layouts.
    """

    score = match.get("score") or match.get("scores") or {}

    if not isinstance(score, dict):
        return None, None

    home = (
        score.get("home")
        or score.get("homeScore")
        or score.get("homePoints")
    )

    away = (
        score.get("away")
        or score.get("awayScore")
        or score.get("awayPoints")
    )

    # Nested score objects
    if isinstance(home, dict):
        home = (
            home.get("current")
            or home.get("display")
            or home.get("total")
            or home.get("points")
        )

    if isinstance(away, dict):
        away = (
            away.get("current")
            or away.get("display")
            or away.get("total")
            or away.get("points")
        )

    try:
        home = float(home)
        away = float(away)
    except Exception:
        return None, None

    return home, away


def _sport_metric(match, metric):
    """
    Total match score used for ranking.
    """

    home, away = _sport_match_score(match)

    if home is None or away is None:
        return None

    return home + away


def _sport_league_country(match):
    league = match.get("league") or {}

    if isinstance(league, dict):
        league_name = (
            league.get("name")
            or league.get("leagueName")
            or ""
        )

        country = league.get("country") or {}

        if isinstance(country, dict):
            country_name = (
                country.get("name")
                or country.get("countryName")
                or ""
            )
        else:
            country_name = str(country or "")
    else:
        league_name = str(league or "")
        country_name = ""

    return league_name, country_name


def _format_sport_entry(index, item):
    match = item["match"]
    value = item["value"]
    dt = item["datetime"]

    home, away = _sport_match_names(match)
    league, country = _sport_league_country(match)

    return (
        f"{index}. {home} - {away}\n"
        f"   {value:.1f}\n"
        f"   Лига: {league or '-'}\n"
        f"   Държава: {country or '-'}\n"
        f"   Начало: {dt.strftime('%H:%M')} BG"
    )


def _build_sport_section(sport_name, metric, matches, start, end):
    valid = []

    for match in matches:
        dt = _sport_match_datetime(match)

        if dt is None:
            continue

        if not (start <= dt < end):
            continue

        value = _sport_metric(match, metric)

        if value is None:
            continue

        valid.append({
            "match": match,
            "datetime": dt,
            "value": value,
        })

    if not valid:
        return (
            f"{sport_name}\n"
            "Няма достатъчно завършени мачове със score данни."
        )

    valid.sort(key=lambda x: x["value"], reverse=True)

    top_over = valid[:3]
    top_under = sorted(valid, key=lambda x: x["value"])[:3]

    lines = [
        sport_name,
        "",
        "🔥 НАД",
    ]

    for i, item in enumerate(top_over, 1):
        lines.append(_format_sport_entry(i, item))
        lines.append("")

    lines.append("❄️ ПОД")

    for i, item in enumerate(top_under, 1):
        lines.append(_format_sport_entry(i, item))
        lines.append("")

    lines.append(f"Мачове със score: {len(valid)}")

    return "\n".join(lines)


def run_sport_daily_scanner(send_func=None):
    """
    Sport scanner.

    Runs once per day.
    One API request per sport.
    Window:
        12:00 BG today -> 12:00 BG tomorrow
    """

    now_bg = datetime.now(TZ)

    start = now_bg.replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )

    end = start + timedelta(days=1)

    lines = [
        "🏆 SPORT DAILY STATISTICAL SCANNER",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG"
        " → "
        f"{end.strftime('%d.%m.%Y %H:%M')} BG",
        "",
    ]

    total_api_calls = 0

    for sport_key, cfg in SPORTS_CONFIG.items():

        print(
            f"SPORT SCAN: {sport_key} | "
            f"1 API REQUEST"
        )

        matches = _sport_api_get_once(
            cfg["endpoint"],
            {
                "date": now_bg.strftime("%Y-%m-%d"),
                "timezone": SPORT_API_TZ,
                "limit": SPORT_API_LIMIT,
            },
        )

        total_api_calls += 1

        # No retry / no pagination.
        if len(matches) >= SPORT_API_LIMIT:
            print(
                f"SPORT SCAN WARNING: {sport_key} "
                f"returned limit={SPORT_API_LIMIT}; "
                "no additional request will be made."
            )

        section = _build_sport_section(
            cfg["name"],
            cfg["metric"],
            matches,
            start,
            end,
        )

        lines.append(section)
        lines.append("")
        lines.append("────────────────────")
        lines.append("")

    lines.append(
        f"📡 API заявки: {total_api_calls} "
        f"(1 на спорт)"
    )

    lines.append(
        f"⏱ Scan time: {time.time() - _START:.1f}s"
    )

    message = "\n".join(lines)

    print(message)

    if send_func:
        send_func(message)

    return message


_START = time.time()



def run_due_scans(send_func):
    """Run football once daily at/after 10:30 BG and keep the sport scanner daily."""
    init_scanner_db()

    now = datetime.now(TZ)
    today = now.date()

    # =====================================================
    # FOOTBALL — ONCE DAILY AT 10:30 BG
    # Window: today 12:00 BG -> tomorrow 12:00 BG
    # =====================================================
    if now.hour > 10 or (now.hour == 10 and now.minute >= 30):
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

    # =====================================================
    # SPORT STATISTICS — EXISTING WORKING SCANNER
    # Do not alter its logic; only keep its existing daily call here.
    # =====================================================
    sport_key = f"sport_test:{today.isoformat()}"

    if not already_ran(sport_key):
        print(_signal_text("SPORT DAILY SCANNER STARTED"))
        try:
            run_sport_daily_scanner(send_func)
            mark_ran(sport_key)
            print(_signal_text("SPORT DAILY SCANNER FINISHED"))
        except Exception as exc:
            print(_signal_text(f"SPORT DAILY SCANNER ERROR: {exc!r}"))

    return True

