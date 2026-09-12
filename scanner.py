# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs once at/after 10:00 and once at/after 20:00 Bulgaria time.
# 10:00: today's fixtures 10:00-23:59 BG
# 20:00: tomorrow's fixtures 00:00-10:00 BG
# =========================================================

import re
import sys
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import API_KEY, CHAT_ID
import threading
_START = time.time()
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")
DB_FILE = "v3_ai.db"
HISTORY_GAMES = None
MAX_WORKERS = 8
_SCAN_FIXTURE_STATS = {}
_SCAN_HISTORY = {}
_API_LOCK = threading.Lock()
_CONSOLE_LOCK = threading.Lock()
_LAST_API_CALL = 0.0
_API_MIN_INTERVAL = 0.12
_TEAM_CURRENT_LEAGUE_CACHE = {}


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

            return payload.get("response")

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


def get_fixtures_for_window(start_bg, end_bg):
    # API-Football's date parameter is the most efficient way to obtain the
    # full daily fixture list. We then filter by exact Sofia-local time.
    days = []
    d = start_bg.date()
    while d <= end_bg.date():
        days.append(d)
        d += timedelta(days=1)

    all_matches = []
    seen = set()
    for day in days:
        matches = _api("fixtures", {"date": day.isoformat()})
        for m in matches:
            fid = m.get("fixture", {}).get("id")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            dt_raw = m.get("fixture", {}).get("date")
            try:
                dt_utc = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
                dt_bg = dt_utc.astimezone(TZ)
            except Exception:
                continue

            status = (m.get("fixture", {}).get("status", {}) or {}).get("short", "")
            if status in {"FT", "AET", "PEN", "CANC", "PST", "ABD", "AWD", "WO"}:
                continue
            if dt_bg < start_bg or dt_bg >= end_bg:
                continue
            if dt_utc <= datetime.now(timezone.utc):
                continue
            all_matches.append(m)

    all_matches.sort(key=lambda x: x["fixture"]["date"])
    return all_matches


def get_team_history(team_id, season, league_id=None):
    """
    Official current-season history.

    1. First use the requested competition.
    2. If fewer than 3 official matches exist there,
       fall back to the team's other official matches
       from the current season.
    3. Friendly matches are excluded.
    4. Only FT/AET/PEN matches are accepted.
    """

    team_id = int(team_id)
    season = int(season)
    league_id = int(league_id or 0)

    key = (team_id, season, league_id)

    if key in _SCAN_HISTORY:
        return _SCAN_HISTORY[key]

    # ---------------------------------------------------------
    # 1. FIRST: CURRENT COMPETITION
    # ---------------------------------------------------------

    primary = []

    if league_id:
        fixtures = _api(
            "fixtures",
            {
                "team": team_id,
                "league": league_id,
                "season": season,
            },
        )

        if not isinstance(fixtures, list):
            fixtures = []

        seen = set()

        for f in fixtures:

            fixture = f.get("fixture") or {}
            league = f.get("league") or {}
            status = (fixture.get("status") or {}).get("short", "")

            fid = fixture.get("id")

            if not fid or fid in seen:
                continue

            if int(league.get("id") or 0) != league_id:
                continue

            if int(league.get("season") or 0) != season:
                continue

            if status not in {"FT", "AET", "PEN"}:
                continue

            seen.add(fid)
            primary.append(f)

    primary.sort(
        key=lambda f: (f.get("fixture") or {}).get("date", ""),
        reverse=True,
    )

    # ---------------------------------------------------------
    # 2. IF WE HAVE 3+ MATCHES IN THE COMPETITION
    #    USE THEM
    # ---------------------------------------------------------

    if len(primary) >= 3:

        primary.sort(
            key=lambda f: (f.get("fixture") or {}).get("date", "")
        )

        _SCAN_HISTORY[key] = primary

        return primary

    # ---------------------------------------------------------
    # 3. FALLBACK:
    #    ALL OFFICIAL CURRENT-SEASON MATCHES
    # ---------------------------------------------------------

    print(
        "HISTORY FALLBACK:",
        team_id,
        "competition_matches=",
        len(primary),
        "-> current-season official matches",
    )

    all_fixtures = _api(
        "fixtures",
        {
            "team": team_id,
            "season": season,
        },
    )

    if not isinstance(all_fixtures, list):
        all_fixtures = []

    clean = []
    seen = set()

    for f in all_fixtures:

        fixture = f.get("fixture") or {}
        league = f.get("league") or {}
        status = (fixture.get("status") or {}).get("short", "")

        fid = fixture.get("id")

        if not fid or fid in seen:
            continue

        # CURRENT SEASON ONLY
        if int(league.get("season") or 0) != season:
            continue

        # OFFICIAL COMPLETED MATCHES ONLY
        if status not in {"FT", "AET", "PEN"}:
            continue

        # EXCLUDE FRIENDLIES
        league_type = str(league.get("type") or "").casefold()
        league_name = str(league.get("name") or "").casefold()

        if league_type == "friendly":
            continue

        if "friend" in league_name:
            continue

        seen.add(fid)
        clean.append(f)

    # Most recent first
    clean.sort(
        key=lambda f: (f.get("fixture") or {}).get("date", ""),
        reverse=True,
    )

    # Keep the most recent official current-season matches.
    # We want enough data for the statistical profiles.
    clean = clean[:12]

    # Oldest -> newest for calculations
    clean.sort(
        key=lambda f: (f.get("fixture") or {}).get("date", "")
    )

    _SCAN_HISTORY[key] = clean

    print(
        "HISTORY FALLBACK RESULT:",
        team_id,
        "matches=",
        len(clean),
    )

    return clean


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
    """
    Load statistics for unique current-season fixtures in batches of up to 20.
    This avoids one request per historical match while using the actual
    fixture-level statistics endpoint/data.
    """
    fixtures_by_id={}
    for history in all_histories:
        for f in history:
            fid=(f.get("fixture") or {}).get("id")
            if fid:
                fixtures_by_id[int(fid)]=f

    ids=list(fixtures_by_id)
    loaded={}
    for i in range(0,len(ids),20):
        batch=ids[i:i+20]
        response=_api("fixtures", {"ids":"-".join(map(str,batch))})
        if not isinstance(response,list):
            continue
        for f in response:
            fid, data=_fixture_market_values(f)
            if fid:
                loaded[int(fid)]=data

    # Merge fixture goals even if enriched statistics are absent.
    for fid,f in fixtures_by_id.items():
        base=loaded.setdefault(fid,{})
        fid2,goal_data=_fixture_market_values(f)
        for tid,vals in goal_data.items():
            base.setdefault(tid,{}).update({
                k:v for k,v in vals.items()
                if k in ("goals_scored","goals_conceded")
            })

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
    if not _is_cup_competition(league):
        return int(league["id"]), int(league["season"])

    tid = int(team_id)
    cached = _TEAM_CURRENT_LEAGUE_CACHE.get(tid)
    if cached is not None:
        return cached

    response = _api("leagues", {"team": tid, "current": "true"})
    if not isinstance(response, list):
        _TEAM_CURRENT_LEAGUE_CACHE[tid] = None
        return None

    candidates = []
    for item in response:
        lg = item.get("league") or {}
        if str(lg.get("type") or "").casefold() != "league":
            continue
        if "cup" in str(lg.get("name") or "").casefold():
            continue
        for season in item.get("seasons") or []:
            if season.get("current"):
                candidates.append((lg, season))
                break

    if not candidates:
        return None
    lg, season = candidates[0]
    return int(lg["id"]), int(season["year"])


def run_daily_scanner(mode="day", reference_date=None, send_func=None):
    init_scanner_db()
    now_bg = datetime.now(TZ)
    ref = reference_date or now_bg.date()
    ref = ref if hasattr(ref, "year") else now_bg.date()

    if mode == "day":
        start = datetime(ref.year, ref.month, ref.day, 10, 0, tzinfo=TZ)
        end = datetime(ref.year, ref.month, ref.day + 1, 0, 0, tzinfo=TZ)
        title = "10:00 ДНЕВЕН СКЕНЕР"
    else:
        next_day = ref + timedelta(days=1)
        start = datetime(next_day.year, next_day.month, next_day.day, 0, 0, tzinfo=TZ)
        end = datetime(next_day.year, next_day.month, next_day.day, 10, 0, tzinfo=TZ)
        title = "20:00 НОЩЕН СКЕНЕР"

    matches = get_fixtures_for_window(start, end)
    print(_signal_text(f"SCANNER {mode.upper()}: {len(matches)} upcoming fixtures"))


    # Exclude leagues unavailable on Betano.
    matches = [m for m in matches if not _is_blocked_fixture(m)]

    # Resolve the correct statistics competition for every team.
    # League match -> that league/current season.
    # Cup match -> team's current domestic league/current season.
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

    print(
        "SCANNER CURRENT-SEASON HISTORY:",
        sum(1 for v in histories.values() if v), "/", len(histories)
    )
    print(
        "SCANNER HISTORICAL FIXTURES WITH DATA:",
        sum(1 for v in stats_by_fixture.values() if v), "/", len(stats_by_fixture)
    )
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
            result["markets"] = {
                k: v for k, v in result.get("markets", {}).items()
                if _market_allowed_by_betano(m, k)
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
    # Goals have no Betano filter.
    lines.append(format_market(results, "goals", "ГОЛОВЕ", "⚽"))
    lines.append(f"\n⏱ Scan time: {time.time() - _START:.1f}s")

    message = "\n".join(lines)
    # Do not dump the complete Telegram message into Railway stdout.
    # live_loop/other threads also write to stdout and can interleave lines
    # in the middle of this large message. Telegram receives the message
    # as one complete payload through send_func().
    with _CONSOLE_LOCK:
        print(f"SCANNER {mode.upper()} MESSAGE READY — {len(results)} matches")
    if send_func:
        send_func(message)
    return message



# =========================================================
# OTHER SPORTS — SEPARATE SPORT BLOCKS
# =========================================================
# Each sport has its own source adapter.
# NBA / Basketball -> API-Sports
# Hockey / Handball / Rugby / American Football / Baseball -> FlashLive
#
# FlashLive is the current-data provider documented for Flashscore-style
# coverage. It requires a RapidAPI key (free tier is available).
# No secondary score-site fallback is used in this block.
# =========================================================

import os
import json

OTHER_SPORT_BLOCKS = {
    "nba": {
        "label": "🏀 NBA",
        "source": "api_sports",
        "api_base": "https://v2.nba.api-sports.io",
    },
    "basketball": {
        "label": "🏀 БАСКЕТБОЛ",
        "source": "api_sports",
        "api_base": "https://v1.basketball.api-sports.io",
    },
    "hockey": {
        "label": "🏒 ХОКЕЙ",
        "source": "flashlive",
        "flashlive_sport_id": 4,
    },
    "handball": {
        "label": "🤾 ХАНДБАЛ",
        "source": "flashlive",
        "flashlive_sport_id": 7,
    },
    "rugby": {
        "label": "🏉 РЪГБИ",
        "source": "flashlive",
        "flashlive_sport_id": 8,
    },
    "american_football": {
        "label": "🏈 NFL / NCAA",
        "source": "flashlive",
        "flashlive_sport_id": 5,
    },
    "baseball": {
        "label": "⚾ БЕЙЗБОЛ",
        "source": "flashlive",
        "flashlive_sport_id": 6,
    },
}

try:
    import config as _scanner_config
    FLASHLIVE_API_KEY = (
        getattr(_scanner_config, "FLASHLIVE_API_KEY", None)
        or os.getenv("FLASHLIVE_API_KEY")
        or os.getenv("RAPIDAPI_KEY")
    )
except Exception:
    FLASHLIVE_API_KEY = os.getenv("FLASHLIVE_API_KEY") or os.getenv("RAPIDAPI_KEY")

FLASHLIVE_BASE = "https://flashlive-sports.p.rapidapi.com"
FLASHLIVE_HEADERS = {
    "x-rapidapi-key": FLASHLIVE_API_KEY or "",
    "x-rapidapi-host": "flashlive-sports.p.rapidapi.com",
}
_OTHER_BLOCK_CACHE = {}
_OTHER_BLOCK_LOCK = threading.Lock()


def _flashlive_get(endpoint, params):
    if not FLASHLIVE_API_KEY:
        raise RuntimeError(
            "FLASHLIVE_API_KEY is missing. Add FLASHLIVE_API_KEY to config.py "
            "or the environment before running non-API-Sports sports."
        )
    r = requests.get(
        f"{FLASHLIVE_BASE}{endpoint}",
        headers=FLASHLIVE_HEADERS,
        params=params,
        timeout=25,
    )
    if r.status_code == 429:
        raise RuntimeError("FlashLive rate limit (429)")
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and payload.get("detail"):
        raise RuntimeError(str(payload["detail"]))
    return payload.get("DATA") if isinstance(payload, dict) else payload


def _flashlive_events_for_day(sport_id, day_offset, timezone_hours=3):
    data = _flashlive_get(
        "/v1/events/list",
        {
            "locale": "en_INT",
            "sport_id": int(sport_id),
            "timezone": str(timezone_hours),
            "indent_days": int(day_offset),
        },
    )
    return data if isinstance(data, list) else []


def _flashlive_flat_events(sport_id, start_bg, end_bg):
    out = []
    seen = set()
    for offset in (0, 1):
        for tournament in _flashlive_events_for_day(sport_id, offset):
            if not isinstance(tournament, dict):
                continue
            tournament_name = tournament.get("NAME") or "-"
            stage_id = tournament.get("TOURNAMENT_STAGE_ID")
            country = tournament.get("COUNTRY_NAME") or "-"
            for e in tournament.get("EVENTS") or []:
                eid = e.get("EVENT_ID")
                if not eid or eid in seen:
                    continue
                try:
                    dt = datetime.fromtimestamp(float(e["START_TIME"]), tz=timezone.utc).astimezone(TZ)
                except Exception:
                    continue
                if not (start_bg <= dt < end_bg):
                    continue
                if str(e.get("STAGE") or "").upper() in {
                    "FINISHED", "CANCELLED", "POSTPONED", "ABANDONED"
                }:
                    continue
                seen.add(eid)
                e = dict(e)
                e["_tournament_name"] = tournament_name
                e["_country_name"] = country
                e["_stage_id"] = stage_id
                out.append(e)
    out.sort(key=lambda x: x.get("START_TIME", 0))
    return out


def _flashlive_results_for_stage(stage_id):
    if not stage_id:
        return []
    key = ("stage", str(stage_id))
    with _OTHER_BLOCK_LOCK:
        if key in _OTHER_BLOCK_CACHE:
            return _OTHER_BLOCK_CACHE[key]

    page = 1
    results = []
    # Page 1 is intentionally enough for the football-style rule:
    # use the most recent completed games first. More pages are only loaded
    # when fewer than three usable games are found.
    while page <= 3 and len(results) < 12:
        data = _flashlive_get(
            "/v1/tournaments/results",
            {
                "locale": "en_INT",
                "tournament_stage_id": str(stage_id),
                "page": page,
            },
        )
        if not isinstance(data, list) or not data:
            break
        found = 0
        for group in data:
            for e in group.get("EVENTS") or [] if isinstance(group, dict) else []:
                if str(e.get("STAGE") or "").upper() != "FINISHED":
                    continue
                if e.get("EVENT_ID"):
                    results.append(e)
                    found += 1
        if found == 0:
            break
        page += 1

    results = results[:12]
    with _OTHER_BLOCK_LOCK:
        _OTHER_BLOCK_CACHE[key] = results
    return results


def _flashlive_team_results(sport_id, team_id):
    key = ("team", int(sport_id), str(team_id))
    with _OTHER_BLOCK_LOCK:
        if key in _OTHER_BLOCK_CACHE:
            return _OTHER_BLOCK_CACHE[key]

    data = _flashlive_get(
        "/v1/teams/results",
        {
            "locale": "en_INT",
            "sport_id": int(sport_id),
            "team_id": str(team_id),
            "page": 1,
        },
    )
    results = []
    if isinstance(data, list):
        for group in data:
            for e in group.get("EVENTS") or [] if isinstance(group, dict) else []:
                if str(e.get("STAGE") or "").upper() != "FINISHED":
                    continue
                if e.get("EVENT_ID"):
                    results.append(e)
    results = results[:12]
    with _OTHER_BLOCK_LOCK:
        _OTHER_BLOCK_CACHE[key] = results
    return results


def _fl_score(e, home=True):
    key = "HOME_SCORE_CURRENT" if home else "AWAY_SCORE_CURRENT"
    try:
        return float(e.get(key))
    except (TypeError, ValueError):
        return None


def _fl_team_history(sport_id, team_id, stage_id):
    primary = _flashlive_results_for_stage(stage_id)
    team_id_s = str(team_id)

    def belongs(e):
        return str(e.get("HOME_PARTICIPANT_ID") or e.get("HOME_TEAM_ID") or "") == team_id_s \
            or str(e.get("AWAY_PARTICIPANT_ID") or e.get("AWAY_TEAM_ID") or "") == team_id_s

    primary_team = [e for e in primary if belongs(e)]
    if len(primary_team) >= 3:
        return primary_team[:12]

    # Football-style fallback: current official team results, no friendlies.
    fallback = _flashlive_team_results(sport_id, team_id)
    return fallback[:12]


def _fl_fixture_record(e):
    return {
        "fixture_id": e.get("EVENT_ID"),
        "home_name": e.get("HOME_NAME") or "HOME",
        "away_name": e.get("AWAY_NAME") or "AWAY",
        "league": e.get("_tournament_name") or e.get("TOURNAMENT_NAME") or "-",
        "country": e.get("_country_name") or "-",
        "date": datetime.fromtimestamp(
            float(e["START_TIME"]), tz=timezone.utc
        ).astimezone(TZ).isoformat(),
    }


def _fl_team_avg(team_id, history):
    vals = []
    tid = str(team_id)
    for e in history:
        home_id = str(e.get("HOME_PARTICIPANT_ID") or e.get("HOME_TEAM_ID") or "")
        away_id = str(e.get("AWAY_PARTICIPANT_ID") or e.get("AWAY_TEAM_ID") or "")
        if home_id == tid:
            score = _fl_score(e, True)
        elif away_id == tid:
            score = _fl_score(e, False)
        else:
            # Some FlashLive responses omit team IDs. Match by name is not
            # safe enough for the statistical calculation.
            continue
        if score is not None:
            vals.append(score)
    return (sum(vals) / len(vals), len(vals)) if vals else None


def _run_flashlive_block(sport, reference_date):
    cfg = OTHER_SPORT_BLOCKS[sport]
    start = datetime(
        reference_date.year, reference_date.month, reference_date.day, 12, 0, tzinfo=TZ
    )
    end = start + timedelta(hours=24)

    games = _flashlive_flat_events(cfg["flashlive_sport_id"], start, end)
    results = []

    for g in games:
        stage_id = g.get("_stage_id")
        home_id = g.get("HOME_PARTICIPANT_ID") or g.get("HOME_TEAM_ID")
        away_id = g.get("AWAY_PARTICIPANT_ID") or g.get("AWAY_TEAM_ID")
        if not home_id or not away_id:
            # If event list does not expose IDs, it cannot safely be matched
            # to team history. Do not invent a match.
            continue

        home_hist = _fl_team_history(cfg["flashlive_sport_id"], home_id, stage_id)
        away_hist = _fl_team_history(cfg["flashlive_sport_id"], away_id, stage_id)

        hp = _fl_team_avg(home_id, home_hist)
        ap = _fl_team_avg(away_id, away_hist)

        if not hp or not ap or hp[1] < 3 or ap[1] < 3:
            continue

        record = _fl_fixture_record(g)
        record["expected"] = {
            "home": hp[0],
            "away": ap[0],
            "expected": hp[0] + ap[0],
            "sample": min(hp[1], ap[1]),
        }
        results.append(record)

    return games, results


def _api_sports_other_block(sport, reference_date):
    """
    API-Sports block for NBA/Basketball only.
    It deliberately uses fixture scores as the statistical value, so there
    is no second historical box-score endpoint and no request per historical
    match.
    """
    cfg = OTHER_SPORT_BLOCKS[sport]
    start = datetime(
        reference_date.year, reference_date.month, reference_date.day, 12, 0, tzinfo=TZ
    )
    end = start + timedelta(hours=24)

    # NBA/Basketball endpoints accept date-based fixture discovery.
    matches = []
    for day in (start.date(), end.date()):
        response = _other_direct_api(
            cfg["api_base"], "games", {"date": day.isoformat()}
        )
        for g in response or []:
            gid = (g.get("id") or (g.get("game") or {}).get("id"))
            if not gid:
                continue
            raw = g.get("date") or (g.get("game") or {}).get("date")
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(TZ)
            except Exception:
                continue
            if not (start <= dt < end):
                continue
            status = str((g.get("status") or {}).get("short") or "").upper()
            if status in {"FT", "FINAL", "FINISHED", "CANC", "POST", "PST"}:
                continue
            matches.append(g)

    # Unique teams, then football-style current competition -> season fallback.
    histories = {}
    for g in matches:
        league = g.get("league") or {}
        season = league.get("season")
        league_id = league.get("id")
        for team in ((g.get("teams") or {}).get("home") or {}, (g.get("teams") or {}).get("away") or {}):
            tid = team.get("id")
            if not tid or season is None:
                continue
            key = (int(tid), str(season), str(league_id or ""))
            if key in histories:
                continue

            primary = _other_direct_api(
                cfg["api_base"], "games",
                {"team": int(tid), "league": int(league_id), "season": season}
            ) if league_id else []

            finished = [
                x for x in (primary or [])
                if str((x.get("status") or {}).get("short") or "").upper()
                in {"FT", "FINAL", "FINISHED", "AOT", "OT", "AET"}
            ]

            if len(finished) < 3:
                finished = _other_direct_api(
                    cfg["api_base"], "games",
                    {"team": int(tid), "season": season}
                ) or []
                finished = [
                    x for x in finished
                    if str((x.get("status") or {}).get("short") or "").upper()
                    in {"FT", "FINAL", "FINISHED", "AOT", "OT", "AET"}
                ]

            finished.sort(
                key=lambda x: str(x.get("date") or (x.get("game") or {}).get("date") or "")
            )
            histories[key] = finished[-12:]

    results = []
    for g in matches:
        league = g.get("league") or {}
        season = league.get("season")
        league_id = str(league.get("id") or "")
        home = (g.get("teams") or {}).get("home") or {}
        away = (g.get("teams") or {}).get("away") or {}
        hh = histories.get((int(home.get("id")), str(season), league_id), [])
        ah = histories.get((int(away.get("id")), str(season), league_id), [])

        def avg(team_id, hist):
            vals = []
            for x in hist:
                th = (x.get("teams") or {}).get("home") or {}
                ta = (x.get("teams") or {}).get("away") or {}
                sc = x.get("scores") or x.get("score") or {}
                if int(th.get("id") or -1) == int(team_id):
                    val = sc.get("home")
                elif int(ta.get("id") or -1) == int(team_id):
                    val = sc.get("away")
                else:
                    continue
                if isinstance(val, dict):
                    val = val.get("total") or val.get("points") or val.get("goals") or val.get("runs")
                try:
                    vals.append(float(val))
                except (TypeError, ValueError):
                    pass
            return (sum(vals) / len(vals), len(vals)) if vals else None

        hp = avg(home.get("id"), hh)
        ap = avg(away.get("id"), ah)
        if not hp or not ap or hp[1] < 3 or ap[1] < 3:
            continue

        results.append({
            "fixture_id": g.get("id") or (g.get("game") or {}).get("id"),
            "home_name": home.get("name") or "HOME",
            "away_name": away.get("name") or "AWAY",
            "league": league.get("name") or "-",
            "country": league.get("country") or "-",
            "date": g.get("date") or (g.get("game") or {}).get("date"),
            "expected": {
                "home": hp[0],
                "away": ap[0],
                "expected": hp[0] + ap[0],
                "sample": min(hp[1], ap[1]),
            },
        })

    return matches, results


def _other_direct_api(base, endpoint, params=None):
    # One shared rate limiter for API-Sports other-sport blocks.
    global _OTHER_LAST_API_CALL
    for attempt in range(3):
        try:
            with _OTHER_API_LOCK:
                wait = _OTHER_API_MIN_INTERVAL - (time.monotonic() - _OTHER_LAST_API_CALL)
                if wait > 0:
                    time.sleep(wait)
                _OTHER_LAST_API_CALL = time.monotonic()

            r = requests.get(
                f"{base}/{endpoint}",
                headers=HEADERS,
                params=params or {},
                timeout=25,
            )
            if r.status_code == 429:
                time.sleep(min(2 ** attempt, 8))
                continue
            r.raise_for_status()
            payload = r.json()
            if payload.get("errors"):
                print("OTHER SPORTS API ERROR:", payload.get("errors"))
                return []
            return payload.get("response") or []
        except Exception as exc:
            if attempt == 2:
                print("OTHER SPORTS REQUEST ERROR:", repr(exc))
                return []
            time.sleep(0.8 * (attempt + 1))
    return []


def _format_other_block(label, results):
    high = sorted(results, key=lambda r: r["expected"]["expected"], reverse=True)[:3]
    low = sorted(results, key=lambda r: r["expected"]["expected"])[:3]

    lines = [label, "🔥 TOP 3 НАД"]
    for i, r in enumerate(high, 1):
        x = r["expected"]
        dt = _other_game_time_bg({"date": r["date"]})
        lines += [
            f"{i}. {r['home_name']} - {r['away_name']}",
            f"   Очаквано: {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}",
            f"   {r['league']} | {r['country']} | {dt.strftime('%d.%m %H:%M') if dt else '?'} BG",
        ]
        if i < len(high):
            lines.append("")

    if not high:
        lines.append("Няма достатъчно статистика.")

    lines += ["", "❄️ TOP 3 ПОД"]

    for i, r in enumerate(low, 1):
        x = r["expected"]
        dt = _other_game_time_bg({"date": r["date"]})
        lines += [
            f"{i}. {r['home_name']} - {r['away_name']}",
            f"   Очаквано: {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}",
            f"   {r['league']} | {r['country']} | {dt.strftime('%d.%m %H:%M') if dt else '?'} BG",
        ]
        if i < len(low):
            lines.append("")

    if not low:
        lines.append("Няма достатъчно статистика.")

    return "\n".join(lines)


def run_other_sports_scanner(reference_date=None, send_func=None):
    """
    One daily aggregation. Each sport runs through its own block/source.
    There is no secondary score-site fallback and no per-historical-game statistics endpoint.
    """
    ref = reference_date or datetime.now(TZ).date()
    lines = [
        "🌍 DAILY OTHER SPORTS SCANNER",
        ref.strftime("%d.%m.%Y"),
        "10:00 BG → срещи 12:00 днес до 12:00 утре",
        "Източник: API-Sports + FlashLive | история: текущ турнир, после официални текущи резултати",
        "",
    ]

    total_fixtures = 0
    total_valid = 0

    for sport, cfg in OTHER_SPORT_BLOCKS.items():
        try:
            if cfg["source"] == "flashlive":
                fixtures, results = _run_flashlive_block(sport, ref)
            else:
                fixtures, results = _api_sports_other_block(sport, ref)

            total_fixtures += len(fixtures)
            total_valid += len(results)

            if not fixtures:
                lines += [cfg["label"], "Няма срещи в 24-часовия прозорец.", ""]
            else:
                lines += [_format_other_block(cfg["label"], results), ""]

            print(
                f"OTHER SPORTS BLOCK [{sport}] source={cfg['source']} "
                f"fixtures={len(fixtures)} valid={len(results)}"
            )
        except Exception as exc:
            print(f"OTHER SPORTS BLOCK ERROR [{sport}]:", repr(exc))
            lines += [cfg["label"], "Грешка при зареждането на данните.", ""]

    lines.append(f"Мачове: {total_fixtures} | Валидни статистически сигнали: {total_valid}")
    message = "\n".join(lines)

    with _CONSOLE_LOCK:
        print(
            f"OTHER SPORTS MESSAGE READY — "
            f"{total_valid} valid signals / {total_fixtures} fixtures"
        )

    if send_func:
        send_func(message)

    return message


def run_due_scans(send_func):
    """Run football and Other Sports daily scans once, persisted in SQLite."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # FOOTBALL: unchanged.
    if 10 <= now.hour < 20:
        key = f"day:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 10:00 STARTED"))
            run_daily_scanner("day", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 10:00 FINISHED"))

    # OTHER SPORTS: once daily at/after 10:00 BG.
    other_key = f"other_sports:{today.isoformat()}"
    if now.hour >= 10 and not already_ran(other_key):
        print(_signal_text("OTHER SPORTS DAILY STARTED"))
        try:
            run_other_sports_scanner(today, send_func)
            mark_ran(other_key)
        except Exception as exc:
            print(_signal_text(f"OTHER SPORTS DAILY ERROR: {exc!r}"))
        print(_signal_text("OTHER SPORTS DAILY FINISHED"))

    # 20:00 football scan remains unchanged.
    if now.hour >= 20:
        key = f"night:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 20:00 STARTED"))
            run_daily_scanner("night", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 20:00 FINISHED"))

# === EXPLICIT OTHER-SPORT BLOCKS ===

# === EXPLICIT OTHER-SPORT BLOCKS ===
def nba_scanner(reference_date=None):
    return _run_one_other_sport("nba", reference_date or datetime.now(TZ).date())

def basketball_scanner(reference_date=None):
    return _run_one_other_sport("basketball", reference_date or datetime.now(TZ).date())

def hockey_scanner(reference_date=None):
    return _run_one_other_sport("hockey", reference_date or datetime.now(TZ).date())

def handball_scanner(reference_date=None):
    return _run_one_other_sport("handball", reference_date or datetime.now(TZ).date())

def rugby_scanner(reference_date=None):
    return _run_one_other_sport("rugby", reference_date or datetime.now(TZ).date())

def american_football_scanner(reference_date=None):
    return _run_one_other_sport("american_football", reference_date or datetime.now(TZ).date())

def baseball_scanner(reference_date=None):
    return _run_one_other_sport("baseball", reference_date or datetime.now(TZ).date())


# === STAGGERED OTHER-SPORTS DAILY SCHEDULER ===
OTHER_SPORT_CHECK_INTERVAL_MINUTES = 5

_OTHER_SPORT_ORDER = (
    "nba", "basketball", "hockey", "handball",
    "rugby", "american_football", "baseball",
)

def _run_other_sport_block_and_save(sport, ref):
    """Exactly one sport per cycle; no parallel fan-out."""
    report, fixtures, valid = _run_one_other_sport(sport, ref)
    _save_other_sport_result(ref, sport, report, fixtures, valid)
    return report, fixtures, valid

def run_other_sports_staggered(reference_date=None, send_func=None):
    """
    Collect sports sequentially with a 5-minute gap.
    The daily report is sent only after all blocks finish.
    """
    ref = reference_date or datetime.now(TZ).date()

    for index, sport in enumerate(_OTHER_SPORT_ORDER):
        if index:
            time.sleep(OTHER_SPORT_CHECK_INTERVAL_MINUTES * 60)

        try:
            _run_other_sport_block_and_save(sport, ref)
        except Exception as exc:
            print("OTHER SPORTS BLOCK ERROR:", sport, repr(exc))
            label = OTHER_SPORTS[sport]["label"]
            report = f"{label}\nДанните не бяха събрани."
            _save_other_sport_result(ref, sport, report, 0, 0)

    if send_func:
        return _send_other_sports_daily_report(ref, send_func)
    return _get_other_sport_results(ref)

