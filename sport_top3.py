# SPORT TOP 3 — HIGHLIGHTLY SPORT ULTRA
# Standalone Sport Daily scanner. Football scanner is intentionally separate.
# Fixture window: 12:00 BG -> next day 12:00 BG.

import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import HIGHLIGHTLY_API_KEY

TZ = ZoneInfo("Europe/Sofia")

# MANUAL TEST: set environment variable SPORT_MANUAL_TEST=1 to run once immediately.
# Normal daily scheduler remains restricted to 10:00-10:05 BG.
SPORT_MANUAL_TEST = os.getenv("SPORT_MANUAL_TEST", "0") == "1"
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
MAX_CONTEXT_CANDIDATES = 10


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

        if scored > conceded:
            result_code, points = "W", 3

        elif scored < conceded:
            result_code, points = "L", 0

        else:
            result_code, points = "D", 1

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

    _SPORT_STATS_CACHE[cache_key] = result

    return result

def _sport_winner_probability(home_avg, away_avg, home_ctx=None, away_ctx=None):
    """Return a winner only when independent signals agree strongly enough."""
    if home_avg is None or away_avg is None or home_avg <= 0 or away_avg <= 0:
        return None
    if not home_ctx or not away_ctx:
        return None
    hn=int(home_ctx.get("last_games",0) or 0); an=int(away_ctx.get("last_games",0) or 0)
    if hn < 5 or an < 5:
        return None

    # Recent form (30%): points earned in the last 7 completed games.
    hf=float(home_ctx.get("form_points",0) or 0)/(3.0*hn)
    af=float(away_ctx.get("form_points",0) or 0)/(3.0*an)
    form_edge=max(-1.0,min(1.0,hf-af))

    # Scoring strength (15%) and recent net performance (20%).
    avg_scale=max(1.0,(home_avg+away_avg)/2.0)
    attack_edge=max(-1.0,min(1.0,(home_avg-away_avg)/avg_scale))
    hnet=float(home_ctx.get("last_scored") or 0)-float(home_ctx.get("last_conceded") or 0)
    anet=float(away_ctx.get("last_scored") or 0)-float(away_ctx.get("last_conceded") or 0)
    net_edge=max(-1.0,min(1.0,(hnet-anet)/avg_scale))

    # Correct home/away split: denominator is home_games / away_games, not total games.
    hgames=int(home_ctx.get("home_games",0) or 0); agames=int(away_ctx.get("away_games",0) or 0)
    if hgames < 3 or agames < 3:
        return None
    hsplit=float(home_ctx.get("home_points",0) or 0)/(3.0*hgames)
    asplit=float(away_ctx.get("away_points",0) or 0)/(3.0*agames)
    split_edge=max(-1.0,min(1.0,hsplit-asplit))

    # Sample quality (15%): weak samples shrink the signal rather than creating a side.
    sample=min(hn,an,hgames,agames)
    quality=min(1.0,sample/7.0)
    score=(form_edge*0.30 + attack_edge*0.15 + net_edge*0.20 + split_edge*0.35)
    score *= (0.55 + 0.45*quality)

    home_p=max(5.0,min(95.0,50.0+score*45.0))
    away_p=100.0-home_p
    edge=abs(home_p-away_p)

    # No forced 50–59% winner. Totals can still be published independently.
    if max(home_p,away_p) < 60.0 or edge < 12.0:
        return None
    return round(home_p,1),round(away_p,1),round(edge,1)


def _sport_market_candidates(home_avg, away_avg, home_ctx=None, away_ctx=None):
    """Return only statistically supported winner/total markets."""
    if home_avg is None or away_avg is None:
        return []

    total = home_avg + away_avg
    markets = []

    # Winner is optional: weak winner evidence means NO winner market.
    win = _sport_winner_probability(home_avg, away_avg, home_ctx, away_ctx)
    if win:
        hp, ap, edge = win
        if hp >= ap:
            markets.append(("ПОБЕДИТЕЛ: HOME", hp))
        else:
            markets.append(("ПОБЕДИТЕЛ: AWAY", ap))

    # Total model remains separate from winner model.
    over_market, over_prob = _sport_probability(total, "over")
    under_market, under_prob = _sport_probability(total, "under")
    if over_market and under_market:
        if over_prob >= under_prob:
            markets.append((over_market, over_prob))
        else:
            markets.append((under_market, under_prob))

    return markets[:2]

def _format_sport_stats_entry(index, item):
    home, away = _sport_match_names(item["match"])
    league, country = _sport_league_country(item["match"])
    dt = item["datetime"]
    return "\n".join([
        f"{index}. {home} - {away}",
        f"   {home}: средно {item['home_avg']:.2f} | мачове: {item['home_games']}",
        f"   {away}: средно {item['away_avg']:.2f} | мачове: {item['away_games']}",
        f"   📊 Общо средно: {item['home_avg'] + item['away_avg']:.2f}",
        f"   Лига: {league or '-'}",
        f"   Държава: {country or '-'}",
        f"   Дата: {dt.strftime('%d.%m.%Y')}",
        f"   Начало: {dt.strftime('%H:%M')} BG",
    ])


def _format_sport_prediction_entry(index, item):
    home, away = _sport_match_names(item["match"])
    dt = item["datetime"]
    lines = [
        f"{index}. {home} - {away}",
        f"   📈 Очаквано общо: {item['expected']:.2f}",
    ]
    for market, probability in item["markets"]:
        lines.append(f"   🎯 Прогноза: {market}")
        lines.append(f"   📊 Вероятност: {probability:.1f}%")
    lines.extend([
        f"   📅 {dt.strftime('%d.%m.%Y')} | {dt.strftime('%H:%M')} BG",
    ])
    return "\n".join(lines)


def _build_sport_top3_section(sport_name, candidates):
    # Sport Daily is PREMATCH only: only fixtures that have not started yet
    # may enter the final report.
    now_bg = datetime.now(TZ)
    prematch = [
        item for item in candidates
        if item.get("datetime") and item["datetime"] > now_bg
    ]

    if not prematch:
        return f"🏁 {sport_name} — PREMATCH\nНяма достатъчно валидни бъдещи срещи."

    ranked = sorted(
        prematch,
        key=lambda x: max((p for _m, p in x["markets"]), default=0.0),
        reverse=True,
    )[:3]

    # Deliberately output BOTH layers: raw statistics first, predictions second.
    lines = [
        f"🏁 {sport_name} — PREMATCH",
        "",
        "📊 СТАТИСТИКА — TOP 5",
    ]
    for i, item in enumerate(ranked, 1):
        lines.append(_format_sport_stats_entry(i, item))
        if i < len(ranked):
            lines.append("")

    lines.extend(["", "🎯 ПРОГНОЗИ — TOP 5"])
    for i, item in enumerate(ranked, 1):
        lines.append(_format_sport_prediction_entry(i, item))
        if i < len(ranked):
            lines.append("")
    return "\n".join(lines)


def run_sport_top3_daily_scanner(send_func=None):
    """Build and send Sport Top 3 safely, without one bad match stopping the report."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)

    # NORMAL MODE: Sport API may only be queried during 10:00-10:05 BG.
    # MANUAL TEST MODE bypasses only this time gate so we can test immediately.
    if not SPORT_MANUAL_TEST and not (now_bg.hour == 10 and 0 <= now_bg.minute <= 5):
        print(
            f"SPORT SCAN BLOCKED: outside 10:00-10:05 BG launch window | now={now_bg:%Y-%m-%d %H:%M:%S}",
            flush=True,
        )
        return ""

    if SPORT_MANUAL_TEST:
        print(
            f"SPORT MANUAL TEST ACTIVE | API test allowed now={now_bg:%Y-%m-%d %H:%M:%S}",
            flush=True,
        )

    run_key = f"sport_top3:{now_bg.date().isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT DAILY SCANNER ALREADY RAN: {now_bg.date().isoformat()}"), flush=True)
        return ""

    # The report is generated at 10:00, but its fixture window is fixed to
    # 12:00 BG today -> 12:00 BG tomorrow (exactly 24 hours).
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
        "История: текущо-сезонни team statistics; волейбол — общи rally points от завършените мачове",
        "",
    ]

    # Process every sport independently. A single API/match failure must not
    # abort the complete daily report.
    for sport_key, cfg in SPORTS_CONFIG.items():
        print(f"SPORT SCAN: {sport_key} — FIXTURES", flush=True)
        candidates = []
        fixtures = []

        try:
            fixtures = _get_sport_fixtures(cfg, start, end)

        except Exception as exc:
            print(
                f"SPORT FIXTURE ERROR: {sport_key} {exc!r}",
                flush=True
            )
            continue

        # Russia / Belarus / Philippines / Singapore + friendly matches
        # are blocked globally for ALL SPORTS.
        filtered_fixtures = []

        for match in fixtures:
            try:
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
                league_name_cf = str(
                    league_name or ""
                ).strip().casefold()

                # BLOCK FRIENDLY MATCHES
                if (
                    "friendly" in league_name_cf
                    or "club friendly" in league_name_cf
                ):
                    print(
                        f"SPORT BLOCKED FRIENDLY: "
                        f"{match.get('id')} — {league_name}",
                        flush=True
                    )
                    continue

                # BLOCKED COUNTRIES — ALL SPORTS
                if country in {
                    "russia",
                    "belarus",
                    "philippines",
                    "singapore",
                }:
                    print(
                        f"SPORT BLOCKED COUNTRY: "
                        f"{match.get('id')} — {country}",
                        flush=True
                    )
                    continue

                filtered_fixtures.append(match)

            except Exception as exc:
                print(
                    f"SPORT FILTER ERROR: "
                    f"{sport_key} {exc!r}",
                    flush=True
                )

        fixtures = filtered_fixtures

        try:
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

            # Recent-form/home-away context is the expensive stage. Only the
            # up to ten strongest base candidates get it; all others stay out of the
            # API pipeline.
            context_candidates = base_candidates[:MAX_CONTEXT_CANDIDATES]

            for item in context_candidates:
                try:
                    home = item["match"].get("homeTeam") or item["match"].get("home") or {}
                    away = item["match"].get("awayTeam") or item["match"].get("away") or {}
                    home_id = _sport_team_id(home)
                    away_id = _sport_team_id(away)
                    home_ctx = _sport_team_context(
                        sport_key, home_id, item["league_id"], item["season"], cfg["metric"], last_n=7
                    )
                    away_ctx = _sport_team_context(
                        sport_key, away_id, item["league_id"], item["season"], cfg["metric"], last_n=7
                    )
                    markets = _sport_market_candidates(
                        item["home_avg"], item["away_avg"], home_ctx, away_ctx
                    )
                    if not markets:
                        over_market, over_prob = _sport_probability(item["expected"], "over")
                        under_market, under_prob = _sport_probability(item["expected"], "under")
                        if over_market and under_market:
                            markets = [(over_market, over_prob)] if over_prob >= under_prob else [(under_market, under_prob)]
                    if not markets:
                        continue
                    item["markets"] = markets
                    item["home_context"] = home_ctx
                    item["away_context"] = away_ctx
                    candidates.append(item)
                except APIQuotaExceeded:
                    raise
                except Exception as exc:
                    print(f"SPORT CONTEXT ERROR: {sport_key} {exc!r}", flush=True)
                    continue

            # If the context stage found nothing, keep the strongest average
            # candidates as total-only signals rather than sending an empty sport.
            if not candidates and base_candidates:
                for item in base_candidates[:5]:
                    over_market, over_prob = _sport_probability(item["expected"], "over")
                    under_market, under_prob = _sport_probability(item["expected"], "under")
                    if over_market and under_market:
                        item["markets"] = [(over_market, over_prob)] if over_prob >= under_prob else [(under_market, under_prob)]
                        item["home_context"] = None
                        item["away_context"] = None
                        candidates.append(item)

            lines.append(_build_sport_top3_section(cfg["name"], candidates))
            lines.append("")
            lines.append("────────────────────")
            lines.append("")
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

def _manual_test_send(message):
    """Telegram sender used by SPORT_MANUAL_TEST=1."""
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        try:
            from config import BOT_TOKEN as _BOT_TOKEN, CHAT_ID as _CHAT_ID
            token = token or _BOT_TOKEN
            chat_id = chat_id or _CHAT_ID
        except Exception:
            pass
    if not token or not chat_id:
        print("SPORT MANUAL TEST: BOT_TOKEN/CHAT_ID missing; report will be printed only", flush=True)
        print(message, flush=True)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    r.raise_for_status()
    print(f"SPORT MANUAL TEST TELEGRAM SENT | status={r.status_code}", flush=True)


    if __name__ == "__main__":
        # Run once immediately only when explicitly enabled.
        if SPORT_MANUAL_TEST:
            result = run_sport_top3_daily_scanner(_manual_test_send)
            print("SPORT MANUAL TEST END | signal_sent=" + str(bool(result)), flush=True)
        else:
            print("SPORT TOP3 MODULE READY | normal launch window: 10:00-10:05 BG", flush=True)



