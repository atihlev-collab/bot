# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs exactly twice per day, only at the two scheduled signal times.
# 11:00: day football signal
# 21:00: night football signal
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
MAX_WORKERS = 4
_SCAN_FIXTURE_STATS = {}
_SCAN_HISTORY = {}
_API_LOCK = threading.Lock()
_CONSOLE_LOCK = threading.RLock()
_LAST_API_CALL = 0.0
_API_MIN_INTERVAL = 6.2


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS other_sports_daily_results (
            run_date TEXT NOT NULL,
            sport TEXT NOT NULL,
            report TEXT NOT NULL,
            fixtures INTEGER NOT NULL DEFAULT 0,
            valid INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_date, sport)
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

    response = _api("leagues", {"team": int(team_id), "current": "true"})
    if not isinstance(response, list):
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
        start = datetime(ref.year, ref.month, ref.day, 11, 0, tzinfo=TZ)
        end = datetime(ref.year, ref.month, ref.day + 1, 0, 0, tzinfo=TZ)
        title = "11:00 ДНЕВЕН СКЕНЕР"
    else:
        next_day = ref + timedelta(days=1)
        start = datetime(next_day.year, next_day.month, next_day.day, 0, 0, tzinfo=TZ)
        end = datetime(next_day.year, next_day.month, next_day.day, 11, 0, tzinfo=TZ)
        title = "21:00 НОЩЕН СКЕНЕР"

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
# OTHER SPORTS DAILY TOTALS SCANNER — API-SPORTS
# Same history flow as football.
# 10:00 BG: scan 12:00 today -> 12:00 tomorrow.
# =========================================================

OTHER_SPORTS = {
    "nba": {"label": "🏀 NBA", "base": "https://v2.nba.api-sports.io", "stats_endpoint": "games/statistics"},
    "basketball": {"label": "🏀 БАСКЕТБОЛ", "base": "https://v1.basketball.api-sports.io", "stats_endpoint": "games/statistics/teams"},
    "hockey": {"label": "🏒 ХОКЕЙ", "base": "https://v1.hockey.api-sports.io", "stats_endpoint": "games/statistics/teams"},
    "handball": {"label": "🤾 ХАНДБАЛ", "base": "https://v1.handball.api-sports.io", "stats_endpoint": "games/statistics/teams"},
    "rugby": {"label": "🏉 РЪГБИ", "base": "https://v1.rugby.api-sports.io", "stats_endpoint": "games/statistics/teams"},
    "american_football": {"label": "🏈 NFL / NCAA", "base": "https://v1.american-football.api-sports.io", "stats_endpoint": "games/statistics/teams"},
    "baseball": {"label": "⚾ БЕЙЗБОЛ", "base": "https://v1.baseball.api-sports.io", "stats_endpoint": "games/statistics/teams"},
}

_OTHER_API_LOCK = threading.Lock()
_OTHER_LAST_API_CALL = {sport: 0.0 for sport in OTHER_SPORTS}
# Keep a safe gap between calls to the same API product.  The other-sports
# scheduler deliberately runs one sport every 5 minutes, and this limiter
# prevents a single sport from bursting through its per-minute quota.
_OTHER_API_MIN_INTERVAL = 6.2
_OTHER_HISTORY_CACHE = {}
_OTHER_STATS_CACHE = {}


def _other_api(sport, endpoint, params=None, timeout=25):
    cfg = OTHER_SPORTS[sport]
    for attempt in range(5):
        try:
            with _OTHER_API_LOCK:
                last = _OTHER_LAST_API_CALL.get(sport, 0.0)
                wait = _OTHER_API_MIN_INTERVAL - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
                _OTHER_LAST_API_CALL[sport] = time.monotonic()

            r = requests.get(f"{cfg['base']}/{endpoint}", headers=HEADERS,
                             params=params or {}, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(5.0 * (2 ** attempt), 60.0)
                print(f"OTHER SPORTS RATE LIMIT [{sport}] {endpoint}: sleeping {delay:.1f}s")
                time.sleep(delay)
                continue
            r.raise_for_status()
            payload = r.json()
            if payload.get("errors"):
                print(f"OTHER SPORTS API ERROR [{sport}]:", endpoint, payload.get("errors"))
                return None
            return payload.get("response") or []
        except Exception as exc:
            if attempt == 4:
                print(f"OTHER SPORTS REQUEST ERROR [{sport}]: {endpoint} {exc!r}")
                return None
            time.sleep(min(2.0 * (2 ** attempt), 30.0))
    return None


def _other_game_id(g):
    return (g or {}).get("id") or ((g or {}).get("game") or {}).get("id")


def _other_game_date(g):
    game = g.get("game") or {}
    raw = g.get("date") or game.get("date")
    if isinstance(raw, dict):
        raw = raw.get("date") or raw.get("time")
    return raw


def _other_game_time_bg(g):
    raw = _other_game_date(g)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None


def _other_game_teams(g):
    teams = g.get("teams") or {}
    return teams.get("home") or {}, teams.get("away") or {}


def _other_score(g, side):
    scores = g.get("scores") or g.get("score") or {}
    value = scores.get(side)
    if isinstance(value, dict):
        for key in ("total", "points", "goals", "runs", "score"):
            n = _safe_float(value.get(key))
            if n is not None:
                return n
    return _safe_float(value)


def _other_game_status(g):
    status = g.get("status") or (g.get("game") or {}).get("status") or {}
    if isinstance(status, dict):
        return str(status.get("short") or status.get("long") or "").upper().strip()
    return str(status).upper().strip()


def _other_is_finished(g):
    status = _other_game_status(g)
    if status in {"CANC", "POST", "PST", "ABD", "ABAN", "SUSP", "DELAY", "NS", "TBD", "NOT_STARTED"}:
        return False
    if status in {"FT", "FINAL", "FINISHED", "END", "AOT", "AET", "OT", "PEN", "AFTER_OT", "AFTER_PEN"}:
        return True
    return _other_score(g, "home") is not None and _other_score(g, "away") is not None


def _other_season(g):
    league = g.get("league") or {}
    if league.get("season") is not None:
        return league.get("season")
    game = g.get("game") or {}
    if game.get("season") is not None:
        return game.get("season")
    return g.get("season")


def _other_league_id(g):
    value = (g.get("league") or {}).get("id")
    return str(value) if value not in (None, "") else ""


def _other_is_official(g):
    league = g.get("league") or {}
    typ = str(league.get("type") or "").casefold()
    name = str(league.get("name") or "").casefold()
    return typ != "friendly" and "friend" not in name and "exhibition" not in name


def _other_history_clean(games, season):
    out, seen = [], set()
    for g in games or []:
        gid = _other_game_id(g)
        if not gid or gid in seen or not _other_is_finished(g):
            continue
        if str(_other_season(g)) != str(season) or not _other_is_official(g):
            continue
        home, away = _other_game_teams(g)
        if not home.get("id") or not away.get("id"):
            continue
        if _other_score(g, "home") is None or _other_score(g, "away") is None:
            continue
        seen.add(gid)
        out.append(g)
    out.sort(key=lambda x: _other_game_time_bg(x) or datetime.min.replace(tzinfo=TZ), reverse=True)
    return out[:12]


def get_other_team_history(sport, team_id, season, league_id=None):
    """Football-style history with ONE API call per team/season.

    We fetch the team's current-season games once, then locally prefer the
    requested tournament. If it has fewer than 3 official completed games,
    we fill from other official competitions in the SAME season. Friendlies
    and previous seasons are never used.
    """
    base_key = (sport, int(team_id), str(season))
    key = (sport, int(team_id), str(season), str(league_id or ""))
    if key in _OTHER_HISTORY_CACHE:
        return _OTHER_HISTORY_CACHE[key]

    if base_key not in _OTHER_TEAM_HISTORY_CACHE:
        all_games = _other_api(sport, "games", {
            "team": int(team_id),
            "season": season,
        })
        _OTHER_TEAM_HISTORY_CACHE[base_key] = _other_history_clean(all_games, season)

    current = list(_OTHER_TEAM_HISTORY_CACHE.get(base_key, []))
    if league_id:
        same = [g for g in current if _other_league_id(g) == str(league_id)]
        other = [g for g in current if _other_league_id(g) != str(league_id)]
        selected = (same + other)[:12]
        print("HISTORY:", sport, team_id, "competition_matches=", len(same),
              "-> current competition" if len(same) >= 3 else
              "-> current-season official matches")
    else:
        selected = current[:12]

    selected.sort(key=lambda x: _other_game_time_bg(x) or datetime.min.replace(tzinfo=TZ))
    _OTHER_HISTORY_CACHE[key] = selected
    return selected


def _other_stat_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("%", "").replace(",", "").strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for k in ("total", "value", "points", "goals", "runs", "score"):
            n = _other_stat_number(value.get(k))
            if n is not None:
                return n
    return None


def _other_stats_for_games(sport, game_ids):
    """Load real team box-score statistics for historical games, batched to save quota."""
    missing = [int(x) for x in game_ids if int(x) not in _OTHER_STATS_CACHE]
    if not missing:
        return {int(x): _OTHER_STATS_CACHE[int(x)] for x in game_ids}
    endpoint = OTHER_SPORTS[sport]["stats_endpoint"]
    loaded = {}
    for i in range(0, len(missing), 20):
        batch = missing[i:i+20]
        params = {"id": "-".join(map(str, batch))}
        response = _other_api(sport, endpoint, params)
        if not isinstance(response, list):
            continue
        for row in response:
            gid = (row.get("game") or {}).get("id") or row.get("game_id") or row.get("id")
            if not gid:
                continue
            tid = (row.get("team") or {}).get("id")
            if not tid:
                continue
            values = {}
            for k, v in row.items():
                if k in {"game", "team", "player", "type"}:
                    continue
                n = _other_stat_number(v)
                if n is not None:
                    values[k] = n
            loaded.setdefault(int(gid), {})[int(tid)] = values
    _OTHER_STATS_CACHE.update(loaded)
    return {int(x): _OTHER_STATS_CACHE.get(int(x), {}) for x in game_ids}


def _other_team_avg(team_id, history, stats_by_game):
    values = []
    for g in history or []:
        gid = _other_game_id(g)
        home, away = _other_game_teams(g)
        stats = stats_by_game.get(int(gid), {}).get(int(team_id), {})
        # Prefer the actual team box score if API-Sports supplied one.
        score = None
        for key in ("points", "goals", "runs", "score", "total"):
            score = _other_stat_number(stats.get(key))
            if score is not None:
                break
        if score is None:
            score = _other_score(g, "home" if int(home.get("id") or -1) == int(team_id) else "away")
        if score is not None:
            values.append(score)
    return (sum(values) / len(values), len(values)) if values else None


def _other_fixture_expected(g, histories, stats_by_game):
    home, away = _other_game_teams(g)
    season = _other_season(g)
    league_id = _other_league_id(g)
    hp_hist = histories.get((int(home.get("id")), str(season), league_id), [])
    ap_hist = histories.get((int(away.get("id")), str(season), league_id), [])
    hp = _other_team_avg(home.get("id"), hp_hist, stats_by_game)
    ap = _other_team_avg(away.get("id"), ap_hist, stats_by_game)
    if not hp or not ap or hp[1] < 3 or ap[1] < 3:
        return None
    return {"expected": hp[0] + ap[0], "home": hp[0], "away": ap[0], "sample": min(hp[1], ap[1])}


def _other_fixture_result(sport, g, expected):
    home, away = _other_game_teams(g)
    league = g.get("league") or {}
    return {"fixture_id": _other_game_id(g), "home_name": home.get("name") or "HOME",
            "away_name": away.get("name") or "AWAY", "league": league.get("name") or "-",
            "country": league.get("country") or "-", "date": _other_game_date(g), "expected": expected}


def _format_other_sport(sport, results):
    # Same presentation as the working football scanner:
    # TOP 3 highest expected totals and TOP 3 lowest expected totals.
    label = OTHER_SPORTS[sport]["label"]
    valid = [
        r for r in results
        if r.get("expected")
        and _safe_float(r["expected"].get("expected")) is not None
    ]

    high = sorted(
        valid,
        key=lambda r: (r["expected"]["expected"], r["expected"].get("sample", 0)),
        reverse=True,
    )[:3]
    low = sorted(
        valid,
        key=lambda r: (r["expected"]["expected"], -r["expected"].get("sample", 0)),
    )[:3]

    lines = [label, "🔥 НАД"]

    if high:
        for i, r in enumerate(high, 1):
            x = r["expected"]
            dt = _other_game_time_bg({"date": r["date"]})
            kickoff = dt.strftime("%H:%M") if dt else "?"
            lines += [
                f"{i}. {r['home_name']} - {r['away_name']}",
                f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}",
                f"   Лига: {r['league']}",
                f"   Държава: {r['country']}",
                f"   Начало: {kickoff} BG",
            ]
            if i < len(high):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистически данни.")

    lines += ["", "❄️ ПОД"]

    if low:
        for i, r in enumerate(low, 1):
            x = r["expected"]
            dt = _other_game_time_bg({"date": r["date"]})
            kickoff = dt.strftime("%H:%M") if dt else "?"
            lines += [
                f"{i}. {r['home_name']} - {r['away_name']}",
                f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}",
                f"   Лига: {r['league']}",
                f"   Държава: {r['country']}",
                f"   Начало: {kickoff} BG",
            ]
            if i < len(low):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистически данни.")

    return "\n".join(lines)


def _run_one_other_sport(sport, reference_date):
    """Collect and calculate one sport only; no Telegram send here."""
    ref = reference_date or datetime.now(TZ).date()
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(hours=24)

    games = get_other_sport_fixtures(sport, start, end)
    print(f"OTHER SPORTS API-Sports [{sport}]: fixtures={len(games)}")
    if not games:
        report = f"{OTHER_SPORTS[sport]['label']}\nНяма срещи в 24-часовия прозорец."
        return report, 0, 0

    histories = {}
    unique = {}
    for g in games:
        season = _other_season(g)
        home, away = _other_game_teams(g)
        league_id = _other_league_id(g)
        if season is None:
            continue
        for team in (home, away):
            if team.get("id"):
                unique[(int(team["id"]), str(season), league_id)] = None

    for team_id, season, league_id in unique:
        try:
            histories[(team_id, str(season), league_id)] = get_other_team_history(
                sport, team_id, season, league_id
            )
        except Exception as exc:
            print("OTHER SPORTS HISTORY ERROR:", sport, team_id, repr(exc))
            histories[(team_id, str(season), league_id)] = []

    all_hist_games = {
        int(_other_game_id(g)): g
        for h in histories.values()
        for g in h
        if _other_game_id(g)
    }
    stats_by_game = _other_stats_for_games(sport, list(all_hist_games))
    print(
        f"OTHER SPORTS HISTORY STATS [{sport}]: "
        f"games={len(all_hist_games)} "
        f"with_data={sum(1 for v in stats_by_game.values() if v)}"
    )

    results = []
    for g in games:
        try:
            x = _other_fixture_expected(g, histories, stats_by_game)
            if x:
                results.append(_other_fixture_result(sport, g, x))
        except Exception as exc:
            print("OTHER SPORTS MATCH ERROR:", sport, repr(exc))

    report = _format_other_sport(sport, results)
    print(f"OTHER SPORTS API-Sports [{sport}]: valid={len(results)}")
    return report, len(games), len(results)


def _save_other_sport_result(run_date, sport, report, fixtures, valid):
    conn = _db()
    conn.execute(
        """INSERT OR REPLACE INTO other_sports_daily_results
           (run_date, sport, report, fixtures, valid, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_date.isoformat(), sport, report, int(fixtures), int(valid),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _get_other_sport_results(run_date):
    conn = _db()
    rows = conn.execute(
        "SELECT sport, report, fixtures, valid FROM other_sports_daily_results "
        "WHERE run_date=?",
        (run_date.isoformat(),),
    ).fetchall()
    conn.close()
    return {row[0]: {"report": row[1], "fixtures": row[2], "valid": row[3]} for row in rows}


def _send_other_sports_daily_report(reference_date, send_func):
    results = _get_other_sport_results(reference_date)
    lines = [
        "🌍 DAILY OTHER SPORTS SCANNER",
        reference_date.strftime("%d.%m.%Y"),
        "10:00 BG → срещи 12:00 днес до 12:00 утре",
        "Източник: API-Sports | история: текущ сезон; първо същият турнир, после други официални турнири",
        "",
    ]
    total_fixtures = 0
    total_valid = 0
    for sport in OTHER_SPORTS:
        item = results.get(sport)
        if item:
            lines += [item["report"], ""]
            total_fixtures += int(item["fixtures"])
            total_valid += int(item["valid"])
        else:
            lines += [OTHER_SPORTS[sport]["label"], "Данните не бяха събрани преди крайния час.", ""]
    lines.append(f"Мачове: {total_fixtures} | Валидни статистически сигнали: {total_valid}")
    message = "\n".join(lines)
    with _CONSOLE_LOCK:
        print(f"OTHER SPORTS MESSAGE READY — {total_valid} valid signals / {total_fixtures} fixtures")
    if send_func:
        send_func(message)
    return message


# 09:30→10:00: one API product every five minutes.
OTHER_SPORT_SCHEDULE = (
    (9, 30, "nba"),
    (9, 35, "basketball"),
    (9, 40, "hockey"),
    (9, 45, "handball"),
    (9, 50, "rugby"),
    (9, 55, "american_football"),
)
# Baseball gets the final collection slot immediately before the 10:00 report.
OTHER_SPORT_FINAL_SLOT = (9, 58, "baseball")


def run_other_sports_scanner(reference_date=None, send_func=None):
    """Manual/full run: collect all sports sequentially, then optionally send."""
    ref = reference_date or datetime.now(TZ).date()
    for sport in OTHER_SPORTS:
        try:
            report, fixtures, valid = _run_one_other_sport(sport, ref)
            _save_other_sport_result(ref, sport, report, fixtures, valid)
        except Exception as exc:
            print("OTHER SPORTS SCANNER ERROR:", sport, repr(exc))
            _save_other_sport_result(
                ref, sport,
                f"{OTHER_SPORTS[sport]['label']}\nГрешка при зареждането на данните.",
                0, 0,
            )
    if send_func:
        return _send_other_sports_daily_report(ref, send_func)
    return _get_other_sport_results(ref)


def _run_staggered_other_sports(reference_date, send_func, now):
    """Run at most one sport per 5-minute slot and send once at 10:00."""
    ref = reference_date
    minute_of_day = now.hour * 60 + now.minute

    for hour, minute, sport in OTHER_SPORT_SCHEDULE + (OTHER_SPORT_FINAL_SLOT,):
        slot = hour * 60 + minute
        if minute_of_day < slot:
            continue
        if already_ran(f"other_sports_slot:{ref.isoformat()}:{sport}"):
            continue
        print(_signal_text(f"OTHER SPORTS {sport.upper()} SLOT STARTED ({hour:02d}:{minute:02d})"))
        try:
            report, fixtures, valid = _run_one_other_sport(sport, ref)
            _save_other_sport_result(ref, sport, report, fixtures, valid)
            mark_ran(f"other_sports_slot:{ref.isoformat()}:{sport}")
            print(_signal_text(f"OTHER SPORTS {sport.upper()} SLOT FINISHED: valid={valid} fixtures={fixtures}"))
        except Exception as exc:
            print(_signal_text(f"OTHER SPORTS {sport.upper()} SLOT ERROR: {exc!r}"))
            _save_other_sport_result(
                ref, sport,
                f"{OTHER_SPORTS[sport]['label']}\nГрешка при зареждането на данните.",
                0, 0,
            )
            mark_ran(f"other_sports_slot:{ref.isoformat()}:{sport}")
        # Exactly one sport per scheduler invocation. This prevents catch-up
        # bursts from defeating the rate-limit protection.
        return


def run_due_scans(send_func):
    """Run football ONLY at 11:00 and 21:00 BG; no startup catch-up."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # FOOTBALL DAY: exact 11:00 BG, once per day.
    if now.hour == 11:
        key = f"day:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 11:00 STARTED"))
            try:
                run_daily_scanner("day", today, send_func)
                mark_ran(key)
                print(_signal_text("DAILY SCANNER 11:00 FINISHED"))
            except Exception as exc:
                print(_signal_text(f"DAILY SCANNER 11:00 ERROR: {exc!r}"))

    # OTHER SPORTS: collect one sport per scheduled slot before 10:00,
    # then send the complete report once at 10:00.
    if (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 10:
        _run_staggered_other_sports(today, send_func, now)

    if now.hour >= 10:
        other_key = f"other_sports:{today.isoformat()}"
        if not already_ran(other_key):
            print(_signal_text("OTHER SPORTS DAILY REPORT STARTED"))
            try:
                _send_other_sports_daily_report(today, send_func)
                mark_ran(other_key)
                print(_signal_text("OTHER SPORTS DAILY REPORT FINISHED"))
            except Exception as exc:
                print(_signal_text(f"OTHER SPORTS DAILY REPORT ERROR: {exc!r}"))

    # FOOTBALL NIGHT: exact 21:00 BG, once per day.
    if now.hour == 21:
        key = f"night:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 21:00 STARTED"))
            try:
                run_daily_scanner("night", today, send_func)
                mark_ran(key)
                print(_signal_text("DAILY SCANNER 21:00 FINISHED"))
            except Exception as exc:
                print(_signal_text(f"DAILY SCANNER 21:00 ERROR: {exc!r}"))

