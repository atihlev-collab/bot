# =========================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs once at/after 10:00 and once at/after 20:00 Bulgaria time.
# 10:00: today's fixtures 10:00-23:59 BG
# 20:00: tomorrow's fixtures 00:00-10:00 BG
# =========================================================

import re
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
        f"Мачове с поне един валиден пазар: {sum(1 for r in results if r["markets"])}",
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
    print(message)
    if send_func:
        send_func(message)
    return message


_START = time.time()


def run_due_scans(send_func):
    """Run the due daily scan(s) once, persisted in SQLite."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # 10:00 scan: once any time from 10:00 until 20:00.
    if now.hour >= 10 and now.hour < 20:
        key = f"day:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 10:00 STARTED"))
            run_daily_scanner("day", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 10:00 FINISHED"))

    # 20:00 scan: once any time from 20:00 until midnight.
    if now.hour >= 20:
        key = f"night:{today.isoformat()}"
        if not already_ran(key):
            print(_signal_text("DAILY SCANNER 20:00 STARTED"))
            run_daily_scanner("night", today, send_func)
            mark_ran(key)
            print(_signal_text("DAILY SCANNER 20:00 FINISHED"))
