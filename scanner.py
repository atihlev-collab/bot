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

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
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
    with _CONSOLE_LOCK:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    if send_func:
        send_func(message)
    return message


# =========================================================
# OTHER SPORTS DAILY TOTALS SCANNER
# Football above is intentionally untouched.
# At 10:00 BG, scan 12:00 today -> 12:00 tomorrow.
# =========================================================

OTHER_SPORTS = {
    "basketball": {"label": "🏀 БАСКЕТБОЛ", "base": "https://v1.basketball.api-sports.io"},
    "hockey": {"label": "🏒 ХОКЕЙ", "base": "https://v1.hockey.api-sports.io"},
    "handball": {"label": "🤾 ХАНДБАЛ", "base": "https://v1.handball.api-sports.io"},
    "rugby": {"label": "🏉 РЪГБИ", "base": "https://v1.rugby.api-sports.io"},
    "american_football": {"label": "🏈 NFL / NCAA", "base": "https://v1.american-football.api-sports.io"},
    "baseball": {"label": "⚾ БЕЙЗБОЛ", "base": "https://v1.baseball.api-sports.io"},
}

_OTHER_API_LOCK = threading.Lock()
_OTHER_LAST_API_CALL = 0.0
_OTHER_API_MIN_INTERVAL = 0.12
_OTHER_HISTORY_CACHE = {}
_CONSOLE_LOCK = threading.Lock()


def _other_api(sport, endpoint, params=None, timeout=25):
    global _OTHER_LAST_API_CALL
    cfg = OTHER_SPORTS[sport]
    for attempt in range(5):
        try:
            with _OTHER_API_LOCK:
                wait = _OTHER_API_MIN_INTERVAL - (time.monotonic() - _OTHER_LAST_API_CALL)
                if wait > 0:
                    time.sleep(wait)
                _OTHER_LAST_API_CALL = time.monotonic()
            r = requests.get(
                f"{cfg['base']}/{endpoint}",
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
                print(f"OTHER SPORTS API ERROR [{sport}]:", endpoint, payload.get("errors"))
                return None
            return payload.get("response")
        except Exception as exc:
            if attempt == 4:
                print(f"OTHER SPORTS REQUEST ERROR [{sport}]:", endpoint, repr(exc))
                return None
            time.sleep(min(0.8 * (2 ** attempt), 6.0))
    return None


def _other_game_id(g):
    if not isinstance(g, dict):
        return None
    return g.get("id") or (g.get("game") or {}).get("id")


def _other_game_date(g):
    if not isinstance(g, dict):
        return None
    raw = g.get("date")
    if isinstance(raw, dict):
        raw = raw.get("date") or raw.get("time")
    if raw:
        return raw
    game = g.get("game") or {}
    raw = game.get("date")
    if isinstance(raw, dict):
        raw = raw.get("date") or raw.get("time")
    return raw


def _other_game_teams(g):
    teams = g.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    return home, away


def _other_score(g, side):
    scores = g.get("scores") or {}
    x = scores.get(side)
    if isinstance(x, dict):
        # Different APIs use different names for the final team score.
        for key in ("total", "points", "goals", "runs", "score"):
            value = _safe_float(x.get(key))
            if value is not None:
                return value
    return _safe_float(x)


def _other_game_status(g):
    candidates = [g.get("status"), (g.get("game") or {}).get("status")]
    for status in candidates:
        if isinstance(status, dict):
            for key in ("short", "long", "name"):
                if status.get(key):
                    return str(status[key]).upper()
        elif status:
            return str(status).upper()
    return ""


def _other_is_finished(g):
    status = _other_game_status(g)
    if not status:
        # A completed game must have two final scores. This fallback is useful
        # for APIs whose status object is missing/empty in older responses.
        return _other_score(g, "home") is not None and _other_score(g, "away") is not None
    bad = ("CANCEL", "POSTPONE", "ABANDON", "SUSPEND", "DELAY")
    if any(x in status for x in bad):
        return False
    good = ("FT", "FINAL", "FINISHED", "ENDED", "END", "AFTER", "AOT", "AET", "OT", "PEN")
    return any(x in status for x in good)


def _other_game_time_bg(g):
    raw = _other_game_date(g)
    if not raw:
        return None
    try:
        raw = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None


def _other_games_for_date(sport, day):
    response = _other_api(sport, "games", {"date": day.isoformat()})
    return response if isinstance(response, list) else []


def get_other_sport_fixtures(sport, start_bg, end_bg):
    out = []
    seen = set()
    day = start_bg.date()
    while day <= end_bg.date():
        for g in _other_games_for_date(sport, day):
            gid = _other_game_id(g)
            if not gid or gid in seen:
                continue
            dt = _other_game_time_bg(g)
            if not dt or dt < start_bg or dt >= end_bg:
                continue
            if dt <= datetime.now(TZ):
                continue
            if _other_is_finished(g):
                continue
            home, away = _other_game_teams(g)
            if not home.get("id") or not away.get("id"):
                continue
            seen.add(gid)
            out.append(g)
        day += timedelta(days=1)
    out.sort(key=lambda x: _other_game_time_bg(x) or datetime.max.replace(tzinfo=TZ))
    return out


def _other_season(g):
    league = g.get("league") or {}
    season = league.get("season")
    if season is not None:
        return season
    # Some API responses expose the season through the nested game object.
    game = g.get("game") or {}
    return game.get("season")


def _other_history_clean(games):
    finished = []
    seen = set()
    for g in games or []:
        gid = _other_game_id(g)
        if not gid or gid in seen or not _other_is_finished(g):
            continue
        dt = _other_game_time_bg(g)
        home, away = _other_game_teams(g)
        if not dt or not home.get("id") or not away.get("id"):
            continue
        hs = _other_score(g, "home")
        aws = _other_score(g, "away")
        if hs is None or aws is None:
            continue
        seen.add(gid)
        finished.append((dt, g))
    finished.sort(key=lambda x: x[0], reverse=True)
    return [g for _, g in finished[:12]]


def _other_league(g):
    return g.get("league") or {}


def _other_league_id(g):
    league = _other_league(g)
    value = league.get("id")
    return str(value) if value not in (None, "") else ""


def _other_same_season(g, season):
    """Strictly match the fixture's current season; never use another season."""
    if season in (None, ""):
        return False
    value = _other_season(g)
    return str(value) == str(season)


def get_other_team_history(sport, team_id, season, league_id=None):
    """Get up to 12 completed official games from the CURRENT season only.

    Rule:
      1) Same tournament + current season first.
      2) If that is insufficient, other official tournaments + same current season.
      3) Never use a previous season.
    """
    key = (sport, int(team_id), str(season), str(league_id or ""))
    if key in _OTHER_HISTORY_CACHE:
        return _OTHER_HISTORY_CACHE[key]

    # First: same tournament, current season only.
    best = []
    if league_id:
        games = _other_api(sport, "games", {
            "team": int(team_id),
            "season": season,
            "league": league_id,
        })
        best = [g for g in _other_history_clean(games)
                if _other_same_season(g, season) and _other_league_id(g) == str(league_id)]

    # Fallback: other official tournaments, but STILL current season only.
    if len(best) < 3:
        games = _other_api(sport, "games", {
            "team": int(team_id),
            "season": season,
        })
        current = [g for g in _other_history_clean(games)
                   if _other_same_season(g, season)]
        # Same tournament results stay first; then other tournaments fill the sample.
        if league_id:
            same = [g for g in current if _other_league_id(g) == str(league_id)]
            other = [g for g in current if _other_league_id(g) != str(league_id)]
            best = (same + other)[:12]
        else:
            best = current[:12]

    # Last fallback: unfiltered team games, but reject every game that is not
    # explicitly from the fixture's current season. This is still NOT a
    # previous-season fallback.
    if len(best) < 3:
        games = _other_api(sport, "games", {"team": int(team_id)})
        current = [g for g in _other_history_clean(games)
                   if _other_same_season(g, season)]
        if league_id:
            same = [g for g in current if _other_league_id(g) == str(league_id)]
            other = [g for g in current if _other_league_id(g) != str(league_id)]
            best = (same + other)[:12]
        else:
            best = current[:12]

    _OTHER_HISTORY_CACHE[key] = best[:12]
    return _OTHER_HISTORY_CACHE[key]

def _other_team_avg(team_id, history):
    vals = []
    for g in history or []:
        home, away = _other_game_teams(g)
        hs = _other_score(g, "home")
        aws = _other_score(g, "away")
        if hs is None or aws is None:
            continue
        if int(home.get("id") or -1) == int(team_id):
            vals.append(hs)
        elif int(away.get("id") or -1) == int(team_id):
            vals.append(aws)
    if not vals:
        return None
    return sum(vals) / len(vals), len(vals)


def _other_fixture_expected(g, histories):
    home, away = _other_game_teams(g)
    hp = _other_team_avg(home.get("id"), histories.get(int(home.get("id"))))
    ap = _other_team_avg(away.get("id"), histories.get(int(away.get("id"))))
    if not hp or not ap or hp[1] < 3 or ap[1] < 3:
        return None
    return {
        "expected": hp[0] + ap[0],
        "home": hp[0],
        "away": ap[0],
        "sample": min(hp[1], ap[1]),
    }


def _other_fixture_result(sport, g, x):
    home, away = _other_game_teams(g)
    league = g.get("league") or {}
    return {
        "fixture_id": _other_game_id(g),
        "home_name": home.get("name") or "HOME",
        "away_name": away.get("name") or "AWAY",
        "league": league.get("name") or "-",
        "country": league.get("country") or "-",
        "date": _other_game_date(g),
        "expected": x,
    }


def _format_other_sport(sport, results):
    cfg = OTHER_SPORTS[sport]
    valid = [r for r in results if r.get("expected")]
    high = sorted(valid, key=lambda r: r["expected"]["expected"], reverse=True)[:3]
    low = sorted(valid, key=lambda r: r["expected"]["expected"])[:3]

    lines = [cfg["label"], "🔥 TOP 3 НАД"]
    for i, r in enumerate(high, 1):
        x = r["expected"]
        dt = _other_game_time_bg({"date": r["date"]})
        kickoff = dt.strftime("%d.%m %H:%M") if dt else "?"
        lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
        lines.append(f"   Очаквано: {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}")
        lines.append(f"   {r['league']} | {r['country']} | {kickoff} BG")
        if i < len(high):
            lines.append("")
    if not high:
        lines.append("Няма достатъчно статистика.")

    lines.extend(["", "❄️ TOP 3 ПОД"])
    for i, r in enumerate(low, 1):
        x = r["expected"]
        dt = _other_game_time_bg({"date": r["date"]})
        kickoff = dt.strftime("%d.%m %H:%M") if dt else "?"
        lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
        lines.append(f"   Очаквано: {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}")
        lines.append(f"   {r['league']} | {r['country']} | {kickoff} BG")
        if i < len(low):
            lines.append("")
    if not low:
        lines.append("Няма достатъчно статистика.")
    return "\n".join(lines)


def run_other_sports_scanner(reference_date=None, send_func=None):
    """10:00 BG: non-football fixtures from 12:00 today through 12:00 tomorrow."""
    ref = reference_date or datetime.now(TZ).date()
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(hours=24)
    lines = [
        "🌍 DAILY OTHER SPORTS SCANNER",
        ref.strftime("%d.%m.%Y"),
        "10:00 BG → срещи 12:00 днес до 12:00 утре",
        "История: текущ сезон; първо същият турнир, после други официални турнири",
        "",
    ]

    total_fixtures = 0
    total_valid = 0
    for sport in OTHER_SPORTS:
        try:
            games = get_other_sport_fixtures(sport, start, end)
            total_fixtures += len(games)
            if not games:
                lines.extend([OTHER_SPORTS[sport]["label"], "Няма срещи в 24-часовия прозорец.", ""])
                continue

            histories = {}
            unique = {}
            for g in games:
                season = _other_season(g)
                home, away = _other_game_teams(g)
                if home.get("id") and away.get("id"):
                    league_id = _other_league_id(g)
                    unique[(int(home["id"]), str(season), league_id)] = None
                    unique[(int(away["id"]), str(season), league_id)] = None

            for team_id, season, league_id in unique:
                try:
                    histories[team_id] = get_other_team_history(sport, team_id, season, league_id)
                except Exception as exc:
                    print("OTHER SPORTS HISTORY ERROR:", sport, team_id, repr(exc))
                    histories[team_id] = []

            results = []
            for g in games:
                try:
                    x = _other_fixture_expected(g, histories)
                    if x:
                        results.append(_other_fixture_result(sport, g, x))
                except Exception as exc:
                    print("OTHER SPORTS MATCH ERROR:", sport, repr(exc))

            total_valid += len(results)
            lines.append(_format_other_sport(sport, results))
            lines.append("")
        except Exception as exc:
            print("OTHER SPORTS SCANNER ERROR:", sport, repr(exc))
            lines.extend([OTHER_SPORTS[sport]["label"], "Грешка при зареждането на данните.", ""])

    lines.append(f"Мачове: {total_fixtures} | Валидни статистически сигнали: {total_valid}")
    message = "\n".join(lines)
    with _CONSOLE_LOCK:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    if send_func:
        send_func(message)
    return message


_START = time.time()


def run_due_scans(send_func):
    """Run the due daily scans once, persisted in SQLite."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # FOOTBALL: keep the existing daily behavior unchanged.
    if now.hour >= 10 and now.hour < 20:
        key = f"day:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 10:00 STARTED"))
            run_daily_scanner("day", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 10:00 FINISHED"))

    # OTHER SPORTS: collect statistics ONLY ONCE in the morning at 10:00 BG.
    # There are no other-sport API calls from this scheduler later in the day.
    # The daily key prevents a second collection on the same date.
    if now.hour == 10:
        other_key = f"other_sports:{today.isoformat()}"
        if not already_ran(other_key):
            print(_signal_text("OTHER SPORTS STATISTICS 10:00 STARTED"))
            try:
                run_other_sports_scanner(today, send_func)
                mark_ran(other_key)
            except Exception as exc:
                print(_signal_text(f"OTHER SPORTS STATISTICS ERROR: {exc!r}"))
            print(_signal_text("OTHER SPORTS STATISTICS 10:00 FINISHED"))

    # 20:00 football scan remains unchanged.
    if now.hour >= 20:
        key = f"night:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 20:00 STARTED"))
            run_daily_scanner("night", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 20:00 FINISHED"))
           
         
