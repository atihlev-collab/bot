# SPORT TOP 3 — HIGHLIGHTLY SPORT ULTRA
# Standalone Sport Daily scanner. Football scanner is intentionally separate.
# Fixture window: 12:00 BG -> next day 12:00 BG.

import math
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from config import HIGHLIGHTLY_API_KEY
except Exception:
    HIGHLIGHTLY_API_KEY = os.getenv("HIGHLIGHTLY_API_KEY")

try:
    from config import BETANO_ODDS_API_KEY as _CONFIG_BETANO_ODDS_API_KEY
except Exception:
    _CONFIG_BETANO_ODDS_API_KEY = None

BETANO_ODDS_API_KEY = (
    _CONFIG_BETANO_ODDS_API_KEY
    or os.getenv("BETANO_ODDS_API_KEY")
    or os.getenv("ODDS_API_KEY")
)

TZ = ZoneInfo("Europe/Sofia")
DB_FILE = "v3_ai.db"
SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_HOST = "sport-highlights-api.p.rapidapi.com"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100
SPORT_HISTORY_FROM = "2025-07-01"

_QUOTA_LOCKS = {}
_QUOTA_LOCK_DATES = {}

class APIQuotaExceeded(Exception):
    pass

def _quota_locked(scope="sport"):
    today = datetime.now(TZ).date().isoformat()
    if _QUOTA_LOCK_DATES.get(scope) != today:
        _QUOTA_LOCKS[scope] = False
        _QUOTA_LOCK_DATES[scope] = today
    return bool(_QUOTA_LOCKS.get(scope, False))

def _set_quota_lock(scope="sport"):
    today = datetime.now(TZ).date().isoformat()
    _QUOTA_LOCKS[scope] = True
    _QUOTA_LOCK_DATES[scope] = today

def _db():
    return sqlite3.connect(DB_FILE, timeout=30)

def init_scanner_db():
    conn=_db()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_scanner_runs (run_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    conn.commit(); conn.close()

def already_ran(run_key):
    init_scanner_db(); conn=_db()
    row=conn.execute("SELECT 1 FROM daily_scanner_runs WHERE run_key=?",(run_key,)).fetchone()
    conn.close(); return row is not None

def mark_ran(run_key):
    conn=_db()
    conn.execute("INSERT OR REPLACE INTO daily_scanner_runs(run_key, created_at) VALUES (?, ?)",(run_key,datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()

def _signal_text(text):
    return f"\033[1m\033[4m{text}\033[0m"

def _send_sport_report_chunks(message, send_func, max_chars=3700):
    if len(message) <= max_chars:
        send_func(message); return
    sections=message.split("\n────────────────────\n")
    chunks=[]; current=""
    for section in sections:
        section=section.strip()
        if not section: continue
        candidate=section if not current else current+"\n\n────────────────────\n\n"+section
        if len(candidate)<=max_chars:
            current=candidate; continue
        if current: chunks.append(current); current=""
        if len(section)>max_chars:
            part=""
            for line in section.splitlines():
                candidate=line if not part else part+"\n"+line
                if len(candidate)<=max_chars: part=candidate
                else:
                    if part: chunks.append(part)
                    part=line
            current=part
        else: current=section
    if current: chunks.append(current)
    total=len(chunks)
    for i,chunk in enumerate(chunks,1):
        if total>1: chunk=f"📊 SPORT DAILY STATISTICAL SCANNER ({i}/{total})\n\n"+chunk
        send_func(chunk)

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

# Hard limits: prevent one sport with many fixtures from exhausting the daily
# Sport API quota before the scanner reaches the other sports.
MAX_FIXTURES_TO_EVALUATE = 10
MAX_CONTEXT_CANDIDATES = 8

# V3: strict publication rules. We prefer an empty report to weak bets.
MIN_ODDS = 1.40
MIN_MODEL_PROB = 62.0
MIN_EDGE_PCT = 5.0
MIN_EV = 0.03
GLOBAL_MAX_PICKS = 5
MAX_PICKS_PER_SPORT = 1
BETANO_EVENTS_MATCH_MINUTES = 30
ODDS_API_BASE = "https://api.odds-api.io/v3"
BETANO_BOOKMAKER = os.getenv("BETANO_BOOKMAKER", "Betano")

# Empirical fallback standard deviations, used only when the current-season
# sample is too small to estimate dispersion safely.
SPORT_TOTAL_SD_FALLBACK = {
    "basketball": 16.0,
    "hockey": 2.0,
    "american-football": 12.0,
    "baseball": 3.0,
    "rugby": 12.0,
    "volleyball": 8.0,
    "handball": 8.0,
}



def _sport_api_get(endpoint, params=None):
    """Call Sport Ultra with a persistent daily quota guard."""
    global _SPORT_API_CALLS
    if _quota_locked("sport"):
        raise APIQuotaExceeded("Highlightly Sport daily quota locked for today")
    _SPORT_API_CALLS += 1
    try:
        response = requests.get(
            f"{SPORT_API_BASE}/{endpoint}",
            headers={"x-rapidapi-key": HIGHLIGHTLY_API_KEY, "x-rapidapi-host": SPORT_API_HOST},
            params=params or {}, timeout=10,
        )
        print(f"SPORT API REQUEST {_SPORT_API_CALLS}: {endpoint} params={params or {}} status={response.status_code}")
        if response.status_code == 429:
            _set_quota_lock("sport")
            print("SPORT QUOTA LOCKED: Highlightly Sport returned HTTP 429")
            raise APIQuotaExceeded("Highlightly Sport daily quota exhausted")
        if response.status_code != 200:
            print("SPORT API ERROR:", response.text[:500])
            return []
        payload=response.json()
        data=payload.get("data", []) if isinstance(payload,dict) else payload
        return data if isinstance(data,list) else []
    except APIQuotaExceeded:
        raise
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



def _norm_cdf(x, mean, sd):
    if sd is None or sd <= 0:
        return 0.5
    z=(x-mean)/(sd*math.sqrt(2.0))
    return 0.5*(1.0+math.erf(z))


def _norm_name(value):
    value=str(value or "").casefold()
    value=re.sub(r"[^a-z0-9а-яёąćęłńóśźżäöüß .&'/-]+", " ", value)
    value=value.replace("&", " and ")
    value=re.sub(r"\b(fc|cf|bc|bk|hc|sc|ac|club|team)\b", " ", value)
    value=re.sub(r"[^a-z0-9а-яёąćęłńóśźżäöüß]+", " ", value)
    return " ".join(value.split())


def _odds_api_get(path, params):
    if not BETANO_ODDS_API_KEY:
        raise RuntimeError("BETANO_ODDS_API_KEY is not configured")
    response=requests.get(
        f"{ODDS_API_BASE}/{path.lstrip('/')}",
        params={**params, "apiKey": BETANO_ODDS_API_KEY},
        timeout=20,
    )
    if response.status_code == 429:
        raise APIQuotaExceeded("Betano odds provider returned HTTP 429")
    response.raise_for_status()
    data=response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data.get("error")))
    return data


def _betano_sport_slug(sport_key):
    return {
        "basketball":"basketball",
        "hockey":"hockey",
        "american-football":"american-football",
        "baseball":"baseball",
        "rugby":"rugby",
        "volleyball":"volleyball",
        "handball":"handball",
    }.get(sport_key, sport_key)


def _betano_event_matches_fixture(event, fixture):
    if not isinstance(event,dict) or not isinstance(fixture,dict):
        return False
    fh,fa=_sport_match_names(fixture)
    eh=str(event.get("home") or "")
    ea=str(event.get("away") or "")
    if not eh or not ea:
        return False
    nh,na,neh,nea=_norm_name(fh),_norm_name(fa),_norm_name(eh),_norm_name(ea)
    names_ok=(nh==neh and na==nea) or (nh in neh or neh in nh) and (na in nea or nea in na)
    if not names_ok:
        return False
    fdt=_sport_match_datetime(fixture)
    raw=event.get("date")
    if not fdt or not raw:
        return False
    try:
        edt=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
        if edt.tzinfo is None: edt=edt.replace(tzinfo=timezone.utc)
        edt=edt.astimezone(TZ)
        return abs((edt-fdt).total_seconds()) <= BETANO_EVENTS_MATCH_MINUTES*60
    except Exception:
        return False


def _get_betano_events(sport_key):
    """Return only events that the Betano feed currently lists."""
    rows=_odds_api_get("events", {"sport":_betano_sport_slug(sport_key), "bookmaker":BETANO_BOOKMAKER})
    if isinstance(rows,dict):
        rows=rows.get("data") or rows.get("events") or []
    return rows if isinstance(rows,list) else []


def _get_betano_odds_multi(event_ids):
    out={}
    ids=[str(x) for x in event_ids if x is not None]
    for i in range(0,len(ids),10):
        batch=ids[i:i+10]
        rows=_odds_api_get("odds/multi", {"eventIds":",".join(batch), "bookmakers":BETANO_BOOKMAKER})
        if isinstance(rows,dict):
            rows=rows.get("data") or rows.get("events") or []
        if isinstance(rows,list):
            for row in rows:
                if isinstance(row,dict) and row.get("id") is not None:
                    out[str(row["id"])]=row
    return out


def _to_float(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _market_type(name):
    n=str(name or "").casefold()
    if any(x in n for x in ("moneyline","match winner","winner","ml")):
        return "moneyline"
    if any(x in n for x in ("spread","handicap")):
        return "spread"
    if any(x in n for x in ("total","totals","over/under","over under")):
        return "total"
    return None


def _extract_betano_markets(event):
    bookmakers=event.get("bookmakers") if isinstance(event,dict) else None
    if not isinstance(bookmakers,dict):
        return []
    raw=bookmakers.get(BETANO_BOOKMAKER)
    if not isinstance(raw,list):
        return []
    markets=[]
    for block in raw:
        if not isinstance(block,dict): continue
        kind=_market_type(block.get("name"))
        if not kind: continue
        odds=block.get("odds")
        if not isinstance(odds,list): continue
        for row in odds:
            if not isinstance(row,dict): continue
            if kind=="moneyline":
                h=_to_float(row.get("home")); a=_to_float(row.get("away"))
                if h and a: markets.append({"kind":kind,"side":"home","odds":h}) ; markets.append({"kind":kind,"side":"away","odds":a})
            elif kind=="total":
                line=_to_float(row.get("hdp") if row.get("hdp") is not None else row.get("line"))
                over=_to_float(row.get("over")); under=_to_float(row.get("under"))
                if line is not None:
                    if over: markets.append({"kind":kind,"side":"over","line":line,"odds":over})
                    if under: markets.append({"kind":kind,"side":"under","line":line,"odds":under})
            elif kind=="spread":
                line=_to_float(row.get("hdp") if row.get("hdp") is not None else row.get("line"))
                home=_to_float(row.get("home")); away=_to_float(row.get("away"))
                if line is not None:
                    if home: markets.append({"kind":kind,"side":"home","line":line,"odds":home})
                    if away: markets.append({"kind":kind,"side":"away","line":line,"odds":away})
    return markets


def _market_probability(market, sport_key, home_avg, away_avg, home_ctx, away_ctx):
    kind=market.get("kind"); side=market.get("side"); line=market.get("line")
    if kind=="moneyline":
        win=_sport_winner_probability(home_avg,away_avg,home_ctx,away_ctx)
        if not win: return None
        return win[0] if side=="home" else win[1]
    htot=home_ctx.get("last_total_mean") if home_ctx else None
    atot=away_ctx.get("last_total_mean") if away_ctx else None
    recent_total=((htot+atot)/2.0) if htot is not None and atot is not None else home_avg+away_avg
    expected_total=0.45*(home_avg+away_avg)+0.55*recent_total
    hdiff=home_ctx.get("last_diff_mean") if home_ctx else None
    adiff=away_ctx.get("last_diff_mean") if away_ctx else None
    expected_diff=home_avg-away_avg
    if hdiff is not None and adiff is not None:
        expected_diff=0.45*(home_avg-away_avg)+0.55*((hdiff-adiff)/2.0)
    fallback=SPORT_TOTAL_SD_FALLBACK.get(sport_key,10.0)
    hs=home_ctx.get("last_total_sd") if home_ctx else None
    a_s=away_ctx.get("last_total_sd") if away_ctx else None
    total_sd=max(fallback*0.65, statistics.mean([x for x in (hs,a_s) if x and x>0]) if any(x and x>0 for x in (hs,a_s)) else fallback)
    ds1=home_ctx.get("last_diff_sd") if home_ctx else None
    ds2=away_ctx.get("last_diff_sd") if away_ctx else None
    diff_sd=max(fallback*0.45, statistics.mean([x for x in (ds1,ds2) if x and x>0]) if any(x and x>0 for x in (ds1,ds2)) else fallback*0.75)
    if kind=="total" and line is not None:
        if side=="over": return (1.0-_norm_cdf(line,expected_total,total_sd))*100.0
        return _norm_cdf(line,expected_total,total_sd)*100.0
    if kind=="spread" and line is not None:
        # Home spread line is treated as home margin + line.
        threshold=-line if side=="home" else line
        if side=="home": return (1.0-_norm_cdf(threshold,expected_diff,diff_sd))*100.0
        return _norm_cdf(threshold,expected_diff,diff_sd)*100.0
    return None


def _select_betano_market(event, sport_key, item, home_ctx, away_ctx):
    markets=_extract_betano_markets(event)
    if not markets: return None
    best=None
    for m in markets:
        odds=m.get("odds")
        if odds is None or odds < MIN_ODDS: continue
        p=_market_probability(m,sport_key,item["home_avg"],item["away_avg"],home_ctx,away_ctx)
        if p is None: continue
        # Conservative calibration: shrink heuristic probabilities toward 50%.
        quality=min(1.0,min(home_ctx.get("last_games",0) if home_ctx else 0, away_ctx.get("last_games",0) if away_ctx else 0)/7.0)
        p=50.0+(p-50.0)*(0.70+0.30*quality)
        implied=100.0/odds
        edge=p-implied
        ev=(p/100.0)*odds-1.0
        if p < MIN_MODEL_PROB or edge < MIN_EDGE_PCT or ev < MIN_EV: continue
        score=edge + ev*100.0*0.35 + max(0.0,p-60.0)*0.25
        m=dict(m,model_probability=round(p,1),implied_probability=round(implied,1),edge=round(edge,1),ev=round(ev*100.0,1),score=score)
        if best is None or m["score"]>best["score"]: best=m
    return best


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


def _sport_team_context(sport_key, team_id, league_id, season, metric, last_n=7):
    """Current-season form/context for one team, based only on completed matches."""
    if not team_id:
        return None

    cache_key = (
        "context",
        sport_key,
        int(team_id),
        int(league_id or 0),
        int(season or 0),
        metric,
        last_n,
    )

    cached = _SPORT_STATS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    cfg = SPORTS_CONFIG[sport_key]

    rows = []

    # Highlightly does NOT accept teamId on /matches.
    # Query homeTeamId and awayTeamId separately.
    for team_key in ("homeTeamId", "awayTeamId"):
        offset = 0

        while True:
            params = {
                team_key: int(team_id),
                "limit": SPORT_API_LIMIT,
                "offset": offset,
            }

            if season:
                params["season"] = int(season)

            if league_id:
                params["leagueId"] = int(league_id)

            batch = _sport_api_get(
                cfg["endpoint"],
                params,
            )

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
            if (
                league_id
                and league.get("id")
                and int(league.get("id")) != int(league_id)
            ):
                continue

            if (
                season
                and league.get("season")
                and int(league.get("season")) != int(season)
            ):
                continue

        pair = _sport_score_pair(match, metric)

        if pair is None:
            continue

        home = (
            match.get("homeTeam")
            or match.get("home")
            or {}
        )

        away = (
            match.get("awayTeam")
            or match.get("away")
            or {}
        )

        hid = _sport_team_id(home)
        aid = _sport_team_id(away)

        if hid == int(team_id):
            scored, conceded = pair
            is_home = True

        elif aid == int(team_id):
            scored, conceded = pair[1], pair[0]
            is_home = False

        else:
            continue

        points, result_code = _sport_points_rule(sport_key, scored, conceded)

        completed.append({
            "dt": dt,
            "scored": scored,
            "conceded": conceded,
            "result": result_code,
            "points": points,
            "home": is_home,
        })

    # Remove duplicates because the same match can only belong
    # to one of home/away queries but defensive deduplication is useful.
    unique = {}

    for item in completed:
        key = (
            item["dt"],
            item["scored"],
            item["conceded"],
            item["home"],
        )
        unique[key] = item

    completed = list(unique.values())

    completed.sort(
        key=lambda x: x["dt"],
        reverse=True,
    )

    last = completed[:last_n]

    result = {
        "games": len(completed),
        "max_points": 3.0 if sport_key in {"handball","rugby","hockey"} else 1.0,
        "form_rate": (sum(x["points"] for x in last) / (len(last) * (3.0 if sport_key in {"handball","rugby","hockey"} else 1.0))) if last else 0.0,
        "home_form_rate": (sum(x["points"] for x in completed if x["home"] and x in completed[:0]) if False else 0.0),

        "form": "".join(
            x["result"]
            for x in reversed(last)
        ),

        "form_points": sum(
            x["points"]
            for x in last
        ),

        "last_games": len(last),

        "last_scored": (
            sum(x["scored"] for x in last) / len(last)
        ) if last else None,

        "last_conceded": (
            sum(x["conceded"] for x in last) / len(last)
        ) if last else None,

        "home_points": sum(
            x["points"]
            for x in completed
            if x["home"]
        ),

        "home_games": sum(
            1
            for x in completed
            if x["home"]
        ),

        "away_points": sum(
            x["points"]
            for x in completed
            if not x["home"]
        ),

        "away_games": sum(
            1
            for x in completed
            if not x["home"]
        ),

        "all_points": sum(
            x["points"]
            for x in completed
        ),
    }

    home_items=[x for x in completed if x["home"]][:10]
    away_items=[x for x in completed if not x["home"]][:10]
    maxp=3.0 if sport_key in {"handball","rugby","hockey"} else 1.0
    result["home_form_rate"]=(sum(x["points"] for x in home_items)/(len(home_items)*maxp)) if home_items else 0.0
    result["away_form_rate"]=(sum(x["points"] for x in away_items)/(len(away_items)*maxp)) if away_items else 0.0
    totals=[x["scored"]+x["conceded"] for x in last]
    diffs=[x["scored"]-x["conceded"] for x in last]
    result["last_total_mean"]=statistics.mean(totals) if totals else None
    result["last_total_sd"]=statistics.pstdev(totals) if len(totals)>=4 else None
    result["last_diff_mean"]=statistics.mean(diffs) if diffs else None
    result["last_diff_sd"]=statistics.pstdev(diffs) if len(diffs)>=4 else None

    _SPORT_STATS_CACHE[cache_key] = result

    return result

def _sport_points_rule(sport_key, scored, conceded):
    if scored > conceded:
        return 3 if sport_key in {"handball","rugby"} else 1, "W"
    if scored < conceded:
        return 0, "L"
    if sport_key in {"handball","rugby","hockey"}:
        return 1, "D"
    return 0.5, "D"


def _sport_winner_probability(home_avg, away_avg, home_ctx=None, away_ctx=None):
    """Conservative sport-specific winner estimate; never a forced pick."""
    if not home_ctx or not away_ctx or home_avg is None or away_avg is None:
        return None
    hn=int(home_ctx.get("last_games",0) or 0); an=int(away_ctx.get("last_games",0) or 0)
    hg=int(home_ctx.get("home_games",0) or 0); ag=int(away_ctx.get("away_games",0) or 0)
    if min(hn,an,hg,ag) < 5: return None
    hform=float(home_ctx.get("form_rate",0.0)); aform=float(away_ctx.get("form_rate",0.0))
    form_edge=max(-1.0,min(1.0,hform-aform))
    scale=max(1.0,(home_avg+away_avg)/2.0)
    attack_edge=max(-1.0,min(1.0,(home_avg-away_avg)/scale))
    hnet=float(home_ctx.get("last_scored") or 0)-float(home_ctx.get("last_conceded") or 0)
    anet=float(away_ctx.get("last_scored") or 0)-float(away_ctx.get("last_conceded") or 0)
    net_edge=max(-1.0,min(1.0,(hnet-anet)/scale))
    hs=float(home_ctx.get("home_form_rate",0.0)); aa=float(away_ctx.get("away_form_rate",0.0))
    split_edge=max(-1.0,min(1.0,hs-aa))
    score=0.35*form_edge+0.15*attack_edge+0.20*net_edge+0.30*split_edge
    quality=min(1.0,min(hn,an,hg,ag)/8.0)
    score*=0.60+0.40*quality
    home_p=max(5.0,min(95.0,50.0+score*45.0))
    away_p=100.0-home_p
    if max(home_p,away_p)<62.0 or abs(home_p-away_p)<14.0:
        return None
    return round(home_p,1),round(away_p,1),round(abs(home_p-away_p),1)


def _sport_market_candidates(home_avg, away_avg, home_ctx=None, away_ctx=None, betano_event=None, sport_key=None, item=None):
    if not betano_event or not sport_key or item is None:
        return []
    selected=_select_betano_market(betano_event,sport_key,item,home_ctx,away_ctx)
    return [selected] if selected else []

def _format_sport_stats_entry(index, item):
    home, away = _sport_match_names(item["match"])
    league, country = _sport_league_country(item["match"])
    dt = item["datetime"]
    return "\n".join([
        f"{index}. {home} - {away}",
        f"   {home}: средно {item['home_avg']:.2f} | мачове: {item['home_games']}",
        f"   {away}: средно {item['away_avg']:.2f} | мачове: {item['away_games']}",
        f"   Лига: {league or '-'}",
        f"   Държава: {country or '-'}",
        f"   Дата: {dt.strftime('%d.%m.%Y')}",
        f"   Начало: {dt.strftime('%H:%M')} BG",
    ])


def _format_sport_prediction_entry(index, item):
    home, away = _sport_match_names(item["match"])
    dt = item["datetime"]
    m=item.get("selected_market") or {}
    if m.get("kind")=="moneyline": label=f"ПОБЕДИТЕЛ: {home if m.get('side')=='home' else away}"
    elif m.get("kind")=="total": label=f"TOTAL {m.get('side','').upper()} {m.get('line')}"
    elif m.get("kind")=="spread": label=f"ХЕНДИКАП: {home if m.get('side')=='home' else away} {m.get('line'):+g}"
    else: label="MARKET"
    return "\n".join([
        f"{index}. {home} - {away}",
        f"   🎯 Пазар: {label}",
        f"   💰 Betano коеф.: {m.get('odds'):.2f}",
        f"   📊 Модел: {m.get('model_probability'):.1f}% | implied: {m.get('implied_probability'):.1f}%",
        f"   📈 Edge: +{m.get('edge'):.1f} п.п. | EV: +{m.get('ev'):.1f}%",
        f"   📅 {dt.strftime('%d.%m.%Y')} | {dt.strftime('%H:%M')} BG",
    ])


def _build_sport_top3_section(sport_name, candidates):
    prematch=[x for x in candidates if x.get("datetime") and x["datetime"]>datetime.now(TZ) and x.get("selected_market")]
    if not prematch:
        return f"🏁 {sport_name} — PREMATCH\nНяма достатъчно силен Betano пазар."
    ranked=sorted(prematch,key=lambda x:x["selected_market"].get("score",0.0),reverse=True)[:MAX_PICKS_PER_SPORT]
    lines=[f"🏁 {sport_name} — PREMATCH","","🎯 СИЛЕН СИГНАЛ"]
    for i,item in enumerate(ranked,1):
        lines.append(_format_sport_prediction_entry(i,item))
    return "\n".join(lines)


def run_sport_top3_daily_scanner(send_func=None):
    """Build and send Sport Top 3 safely, without one bad match stopping the report."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    run_key = f"sport_top3:{now_bg.date().isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT DAILY SCANNER ALREADY RAN: {now_bg.date().isoformat()}"), flush=True)
        return ""

    # Strict Betano gate: no bookmaker verification = no published prediction.
    if not BETANO_ODDS_API_KEY:
        message="\n".join([
            "📊 DAILY STATISTICAL SCANNER — SPORT V3",
            now_bg.strftime("%d.%m.%Y"),
            "",
            "🔒 STRICT BETANO GATE",
            "Няма BETANO_ODDS_API_KEY.",
            "Не се публикуват прогнози, докато Betano проверката не е активна.",
        ])
        print(message, flush=True)
        if send_func:
            try: send_func(message)
            except Exception as exc: print(f"SPORT TELEGRAM ERROR: {exc!r}", flush=True)
        return message

    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = [
        "📊 DAILY STATISTICAL SCANNER — PREMATCH СИГНАЛИ",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "🏁 РЕЖИМ: PREMATCH — само срещи, които още не са започнали",
        "",
        "Период:",
        f"{start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: спортно-специфична форма + home/away + scoring/defense + dispersion; Betano пазари са задължителен gate",
        "",
    ]

    # Process every sport independently. A single API/match failure must not
    # abort the complete daily report. Only the best GLOBAL_MAX_PICKS signals survive.
    global_published=[]
    for sport_key, cfg in SPORTS_CONFIG.items():
        print(f"SPORT SCAN: {sport_key} — FIXTURES", flush=True)
        candidates = []
        fixtures = []
        try:
            fixtures = _get_sport_fixtures(cfg, start, end)

            # Russia / Belarus + friendly matches are blocked globally.
            filtered_fixtures = []
            for match in fixtures:
                try:
                    league = match.get("league") or {}
                    country = ""
                    if isinstance(league, dict):
                        country = league.get("country") or league.get("countryName") or ""
                        if isinstance(country, dict):
                            country = country.get("name") or country.get("countryName") or ""
                    if not country:
                        country = match.get("country") or match.get("countryName") or ""
                        if isinstance(country, dict):
                            country = country.get("name") or country.get("countryName") or ""
                    country = str(country).strip().casefold()
                    league_name, _ = _sport_league_country(match)
                    league_name_cf = str(league_name or "").strip().casefold()
                    if "friendly" in league_name_cf or "club friendly" in league_name_cf:
                        print(f"SPORT BLOCKED FRIENDLY: {match.get('id')} — {league_name}", flush=True)
                        continue
                    if country in {"russia", "belarus"}:
                        print(f"SPORT BLOCKED COUNTRY: {match.get('id')} — {country}", flush=True)
                        continue
                    filtered_fixtures.append(match)
                except Exception as exc:
                    print(f"SPORT FILTER ERROR: {sport_key} {exc!r}", flush=True)
            fixtures = filtered_fixtures

            # PREMATCH FILTER MUST HAPPEN BEFORE THE FIXTURE CAP.
            # Otherwise the cap can consume the earliest games of the 12:00->12:00
            # window even when those games have already started, leaving the
            # actual future matches out of the evaluation set.
            now_scan = datetime.now(TZ)
            prematch_fixtures = []
            for match in fixtures:
                dt = _sport_match_datetime(match)
                if dt is None:
                    continue
                if dt > now_scan:
                    prematch_fixtures.append(match)
                else:
                    print(
                        f"SPORT PREMATCH SKIP: {sport_key} match={match.get('id')} "
                        f"start={dt.strftime('%Y-%m-%d %H:%M')} already_started",
                        flush=True,
                    )

            fixtures = prematch_fixtures
            print(
                f"SPORT PREMATCH FILTER: {sport_key} future={len(fixtures)} now={now_scan.strftime('%H:%M:%S')} BG",
                flush=True,
            )

            # Stage 1: HARD-CAP ONLY AFTER THE PREMATCH FILTER.
            # This guarantees that the cap is spent only on future matches.
            fixtures.sort(key=lambda m: (_sport_match_datetime(m) or end))

            if len(fixtures) > MAX_FIXTURES_TO_EVALUATE:
                # Keep the earliest fixtures in the 12:00 -> 12:00 window.
                # This is deterministic, bounded and avoids silently spending
                # the quota on the long tail of minor competitions.
                selected_fixtures = fixtures[:MAX_FIXTURES_TO_EVALUATE]
                print(
                    f"SPORT FIXTURE CAP: {sport_key} "
                    f"total={len(fixtures)} selected={len(selected_fixtures)} "
                    f"cap={MAX_FIXTURES_TO_EVALUATE}",
                    flush=True,
                )
            else:
                selected_fixtures = fixtures

            # Fetch the base team averages concurrently. This reduces wall time
            # without increasing the number of API requests beyond the cap.
            base_candidates = []
            team_jobs = []

            for match in selected_fixtures:
                try:
                    home = match.get("homeTeam") or match.get("home") or {}
                    away = match.get("awayTeam") or match.get("away") or {}
                    home_id = _sport_team_id(home)
                    away_id = _sport_team_id(away)
                    if not home_id or not away_id:
                        continue

                    league = match.get("league") or {}
                    league_id = league.get("id") if isinstance(league, dict) else None
                    season = league.get("season") if isinstance(league, dict) else None
                    team_jobs.append((match, home_id, away_id, league_id, season))
                except Exception as exc:
                    print(f"SPORT MATCH PREP ERROR: {sport_key} {exc!r}", flush=True)

            # Threading is intentionally limited. The hard fixture cap is the
            # quota protection; workers only make the bounded set finish faster.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {}
                for match, home_id, away_id, league_id, season in team_jobs:
                    if sport_key == "volleyball":
                        futures[pool.submit(
                            _get_volleyball_team_average, home_id, league_id, season
                        )] = ("home", match, home_id, away_id, league_id, season)
                        futures[pool.submit(
                            _get_volleyball_team_average, away_id, league_id, season
                        )] = ("away", match, home_id, away_id, league_id, season)
                    else:
                        futures[pool.submit(
                            _get_team_average, sport_key, home_id, cfg["metric"]
                        )] = ("home", match, home_id, away_id, league_id, season)
                        futures[pool.submit(
                            _get_team_average, sport_key, away_id, cfg["metric"]
                        )] = ("away", match, home_id, away_id, league_id, season)

                pair_results = {}
                for future in as_completed(futures):
                    side, match, home_id, away_id, league_id, season = futures[future]
                    key = id(match)
                    try:
                        pair_results.setdefault(key, {})[side] = future.result()
                        pair_results[key]["meta"] = (
                            match, home_id, away_id, league_id, season
                        )
                    except APIQuotaExceeded:
                        raise
                    except Exception as exc:
                        print(f"SPORT STATS ERROR: {sport_key} {exc!r}", flush=True)

            for result in pair_results.values():
                try:
                    match, home_id, away_id, league_id, season = result["meta"]
                    h = result.get("home")
                    a = result.get("away")
                    if not h or not a or h.get("games", 0) < 3 or a.get("games", 0) < 3:
                        continue
                    dt = _sport_match_datetime(match)
                    if not dt:
                        continue

                    expected = h["average"] + a["average"]
                    base_candidates.append({
                        "match": match,
                        "datetime": dt,
                        "home_avg": h["average"],
                        "away_avg": a["average"],
                        "home_games": h["games"],
                        "away_games": a["games"],
                        "expected": expected,
                        "league_id": league_id,
                        "season": season,
                    })
                except Exception as exc:
                    print(f"SPORT CANDIDATE ERROR: {sport_key} {exc!r}", flush=True)

            base_candidates.sort(
                key=lambda x: (x["expected"], abs(x["home_avg"] - x["away_avg"])),
                reverse=True,
            )

            # V3 HARD GATE: the fixture must exist in Betano first.
            # We never publish a prediction for a match/market that Betano does not expose.
            if not BETANO_ODDS_API_KEY:
                raise RuntimeError("BETANO_ODDS_API_KEY is missing — strict Betano gate is enabled")
            betano_events=_get_betano_events(sport_key)
            matched=[]
            for item in base_candidates[:MAX_CONTEXT_CANDIDATES]:
                match_event=next((e for e in betano_events if _betano_event_matches_fixture(e,item["match"])),None)
                if match_event:
                    item["betano_event"]=match_event
                    matched.append(item)
                else:
                    print(f"BETANO SKIP: {sport_key} {_sport_match_names(item['match'])} — event not found",flush=True)
            if not matched:
                print(f"BETANO NO MATCHES: {sport_key}",flush=True)
            odds_by_id=_get_betano_odds_multi([x["betano_event"].get("id") for x in matched if x["betano_event"].get("id") is not None])

            for item in matched:
                try:
                    home = item["match"].get("homeTeam") or item["match"].get("home") or {}
                    away = item["match"].get("awayTeam") or item["match"].get("away") or {}
                    home_id = _sport_team_id(home); away_id = _sport_team_id(away)
                    home_ctx = _sport_team_context(sport_key, home_id, item["league_id"], item["season"], cfg["metric"], last_n=7)
                    away_ctx = _sport_team_context(sport_key, away_id, item["league_id"], item["season"], cfg["metric"], last_n=7)
                    event=odds_by_id.get(str(item["betano_event"].get("id")))
                    market=_sport_market_candidates(item["home_avg"],item["away_avg"],home_ctx,away_ctx,event,sport_key,item)
                    if not market: continue
                    item["selected_market"]=market[0]
                    item["home_context"]=home_ctx; item["away_context"]=away_ctx
                    candidates.append(item)
                except APIQuotaExceeded: raise
                except Exception as exc:
                    print(f"SPORT V3 CONTEXT ERROR: {sport_key} {exc!r}",flush=True)
                    continue

            if candidates:
                best=max(candidates,key=lambda x:x.get("selected_market",{}).get("score",0.0))
                global_published.append((sport_key,cfg,best))
            print(f"SPORT PREMATCH RESULT: {sport_key} fixtures={len(fixtures)} valid_prematch={len(candidates)}", flush=True)

        except APIQuotaExceeded:
            # Quota exhaustion is a daily stop. Keep already-built sections and
            # add an explicit status instead of losing the Telegram report.
            print(f"SPORT QUOTA STOP: {sport_key}", flush=True)
            lines.append(f"{cfg['name']}\nAPI quota exhausted — remaining sports skipped.")
            lines.append("")
            lines.append("────────────────────")
            lines.append("")
            break
        except Exception as exc:
            print(f"SPORT SECTION ERROR: {sport_key}: {exc!r}", flush=True)
            lines.append(f"{cfg['name']}\nВременна API грешка — спортът е пропуснат, останалите продължават.")
            lines.append("")
            lines.append("────────────────────")
            lines.append("")
            continue

    if not BETANO_ODDS_API_KEY:
        lines.append("⚠️ BETANO FILTER: няма BETANO_ODDS_API_KEY — не са публикувани прогнози.")
    else:
        global_published=sorted(global_published,key=lambda x:x[2].get("selected_market",{}).get("score",0.0),reverse=True)[:GLOBAL_MAX_PICKS]
        lines=[
            "📊 DAILY STATISTICAL SCANNER — SPORT V3",
            now_bg.strftime("%d.%m.%Y"),
            "",
            "🔒 STRICT BETANO GATE — само срещи + пазари, налични в Betano",
            f"💰 Минимален коефициент: {MIN_ODDS:.2f}",
            "🎯 Публикуваме само силни +EV сигнали; няма принуден TOP 3.",
            "",
        ]
        if not global_published:
            lines.append("НЯМА ДОСТАТЪЧНО СИЛНИ СИГНАЛИ ЗА ДНЕС.")
        else:
            for idx,(sport_key,cfg,item) in enumerate(global_published,1):
                lines.append(f"{cfg['name']}")
                lines.append(_format_sport_prediction_entry(idx,item))
                if idx < len(global_published): lines.extend(["","────────────────────",""])

    lines.append(f"📡 API заявки: {_SPORT_API_CALLS}")
    lines.append(f"⏱ Scan time: {time.time() - started:.1f}s")
    message = "\n".join(lines)
    print(message, flush=True)

    # ALWAYS attempt Telegram before marking the run complete.
    if send_func:
        try:
            _send_sport_report_chunks(message, send_func, max_chars=3700)
            print("SPORT TELEGRAM REPORT SENT", flush=True)
        except Exception as exc:
            print(f"SPORT TELEGRAM ERROR: {exc!r}", flush=True)
            # Do not mark the run as complete when Telegram itself failed.
            return message

    mark_ran(run_key)
    print(f"SPORT DAILY COMPLETE | day={now_bg.date().isoformat()} | api_calls={_SPORT_API_CALLS}", flush=True)
    return message



