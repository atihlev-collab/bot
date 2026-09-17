# =========================================================
# SIMPLE DAILY STATISTICAL SCANNER
# Football + Basketball + Hockey + American Football +
# Baseball + Rugby + Volleyball + Handball
# =========================================================

import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import HIGHLIGHTLY_API_KEY

TZ = ZoneInfo("Europe/Sofia")

FOOTBALL_BASE = "https://soccer.highlightly.net"
FOOTBALL_HOST = "football-highlights-api.p.rapidapi.com"
SPORT_BASE = "https://sports.highlightly.net"
SPORT_HOST = "sport-highlights-api.p.rapidapi.com"

BLOCKED_COUNTRIES = {"russia", "belarus"}

SPORTS = {
    "basketball": ("🏀 БАСКЕТБОЛ", "points"),
    "hockey": ("🏒 ХОКЕЙ", "goals"),
    "american-football": ("🏈 NFL / NCAA — Division I / Division II", "points"),
    "baseball": ("⚾ БЕЙЗБОЛ", "runs"),
    "rugby": ("🏉 РЪГБИ", "points"),
    "volleyball": ("🏐 ВОЛЕЙБОЛ", "points"),
    "handball": ("🤾 ХАНДБАЛ", "goals"),
}

_session = requests.Session()
_last_call = 0.0
_MIN_INTERVAL = 0.12


def _get(url, host, params=None, timeout=25):
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

    headers = {
        "x-rapidapi-key": HIGHLIGHTLY_API_KEY,
        "x-rapidapi-host": host,
    }

    for attempt in range(4):
        try:
            print(f"API CALL: {url} params={params or {}} host={host}")
            r = _session.get(url, headers=headers, params=params or {}, timeout=timeout)
            print(f"API RESPONSE: status={r.status_code} url={url}")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(1.0 * (2 ** attempt), 6.0))
                continue
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, list):
                print(f"API DATA: list rows={len(payload)}")
                return payload
            if isinstance(payload, dict):
                if payload.get("errors"):
                    print("API ERROR:", payload.get("errors"))
                    return []
                data = payload.get("data", [])
                print(
                    f"API DATA: dict keys={list(payload.keys())[:10]} "
                    f"rows={len(data) if isinstance(data, list) else type(data).__name__}"
                )
                return data if isinstance(data, list) else []
            return []
        except Exception as exc:
            if attempt == 3:
                print("API REQUEST ERROR:", url, repr(exc))
                return []
            time.sleep(min(0.8 * (2 ** attempt), 5.0))
    return []


def _norm(value):
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("%", "").strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("current", "value", "score", "total"):
            if key in value:
                n = _number(value[key])
                if n is not None:
                    return n
    if isinstance(value, list) and value:
        return _number(value[0])
    return None


def _blocked(country):
    return _norm(country) in BLOCKED_COUNTRIES


def _football_match(raw):
    if not isinstance(raw, dict):
        return None
    home = raw.get("homeTeam") or {}
    away = raw.get("awayTeam") or {}
    league = raw.get("league") or {}
    country = raw.get("country") or {}
    state = raw.get("state") or {}
    score = state.get("score") or {}
    current = score.get("current")

    hs = aw = 0.0
    if isinstance(current, str):
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", current)
        if m:
            hs, aw = float(m.group(1)), float(m.group(2))
    elif isinstance(current, dict):
        hs = _number(current.get("home") or current.get("homeTeam")) or 0.0
        aw = _number(current.get("away") or current.get("awayTeam")) or 0.0

    description = str(state.get("description") or "")
    d = _norm(description)
    if "finished" in d or "final" in d or "ended" in d:
        status = "FT"
    else:
        status = "NS"

    return {
        "id": raw.get("id"),
        "date": raw.get("date"),
        "status": status,
        "home_id": home.get("id"),
        "away_id": away.get("id"),
        "home": home.get("name") or "Unknown",
        "away": away.get("name") or "Unknown",
        "league_id": league.get("id"),
        "league": league.get("name") or "",
        "season": league.get("season"),
        "country": country.get("name") if isinstance(country, dict) else str(country or ""),
        "goals_home": hs,
        "goals_away": aw,
    }


def _dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None


def _football_fixtures(start, end):
    """Return real upcoming football fixtures in the BG window."""
    out = {}
    day = start.date()
    now_utc = datetime.now(timezone.utc)

    while day <= end.date():
        rows = _get(
            f"{FOOTBALL_BASE}/matches",
            FOOTBALL_HOST,
            {"date": day.isoformat(), "timezone": "Europe/Sofia", "limit": 100},
        )
        print(f"FOOTBALL FIXTURES API: date={day.isoformat()} rows={len(rows)}")
        if rows:
            print("FOOTBALL FIRST RAW:", rows[0])

        for raw in rows:
            m = _football_match(raw)
            if not m or not m["id"] or not m["date"]:
                continue
            try:
                dt_utc = datetime.fromisoformat(str(m["date"]).replace("Z", "+00:00"))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_bg = dt_utc.astimezone(TZ)
            except Exception:
                continue

            if dt_bg < start or dt_bg >= end or dt_utc <= now_utc:
                continue
            if _blocked(m["country"]):
                continue
            out[int(m["id"])] = m

        day += timedelta(days=1)

    result = sorted(out.values(), key=lambda x: x["date"])
    print(f"FOOTBALL FIXTURES RESULT: {len(result)}")
    if not result:
        print("FOOTBALL DIAGNOSTIC: API returned no usable fixture after filtering.")
    return result


def _football_team_history(team_id, league_id, season):
    if not team_id or not season:
        return []

    rows = []
    if league_id:
        rows += _get(
            f"{FOOTBALL_BASE}/matches", FOOTBALL_HOST,
            {"leagueId": league_id, "season": season, "homeTeamId": team_id, "limit": 100},
        )
        rows += _get(
            f"{FOOTBALL_BASE}/matches", FOOTBALL_HOST,
            {"leagueId": league_id, "season": season, "awayTeamId": team_id, "limit": 100},
        )

    # Fallback if the league query returned too little.
    if len(rows) < 3:
        rows += _get(
            f"{FOOTBALL_BASE}/matches", FOOTBALL_HOST,
            {"season": season, "homeTeamId": team_id, "limit": 100},
        )
        rows += _get(
            f"{FOOTBALL_BASE}/matches", FOOTBALL_HOST,
            {"season": season, "awayTeamId": team_id, "limit": 100},
        )

    result = {}
    for raw in rows:
        m = _football_match(raw)
        if not m or not m["id"] or m["status"] != "FT":
            continue
        if int(m["season"] or 0) != int(season):
            continue
        if _blocked(m["country"]):
            continue
        result[int(m["id"])] = m
    return sorted(result.values(), key=lambda x: x["date"])


def _football_stats(fixture_id):
    rows = _get(
        f"{FOOTBALL_BASE}/statistics/{int(fixture_id)}",
        FOOTBALL_HOST,
    )
    result = {}
    aliases = {
        "corners": {"corners", "corner kicks", "corner"},
        "shots": {"total shots", "total shot", "shots", "shots total"},
        "cards": {"yellow cards", "yellow card", "yellow cards total"},
    }
    for block in rows:
        team = block.get("team") or {}
        tid = team.get("id")
        if not tid:
            continue
        vals = {}
        for item in block.get("statistics") or []:
            name = _norm(item.get("displayName") or item.get("type") or item.get("name"))
            value = _number(item.get("value"))
            if value is None:
                continue
            for canonical, names in aliases.items():
                if name in names:
                    vals[canonical] = value
        result[int(tid)] = vals
    return result


def _football_profiles(histories):
    profiles = {}
    fixture_ids = {}
    for history in histories.values():
        for m in history:
            fixture_ids[m["id"]] = m

    stats_cache = {}
    for i, (fid, match) in enumerate(fixture_ids.items(), 1):
        stats_cache[fid] = _football_stats(fid)
        if i % 50 == 0:
            print(f"FOOTBALL HISTORY STATS: {i}/{len(fixture_ids)}")

    for key, history in histories.items():
        team_id = key[0]
        sums = {"goals_scored": 0.0, "goals_conceded": 0.0, "corners": 0.0, "shots": 0.0, "cards": 0.0}
        counts = {k: 0 for k in sums}

        for m in history:
            if int(m["home_id"] or 0) == int(team_id):
                sums["goals_scored"] += m["goals_home"]
                sums["goals_conceded"] += m["goals_away"]
            else:
                sums["goals_scored"] += m["goals_away"]
                sums["goals_conceded"] += m["goals_home"]
            counts["goals_scored"] += 1
            counts["goals_conceded"] += 1

            data = stats_cache.get(m["id"], {}).get(int(team_id), {})
            for metric in ("corners", "shots", "cards"):
                if data.get(metric) is not None:
                    sums[metric] += data[metric]
                    counts[metric] += 1

        profiles[key] = {
            metric: (sums[metric] / counts[metric], counts[metric])
            for metric in sums if counts[metric]
        }
    return profiles


def _football_candidates(fixtures):
    histories = {}
    for m in fixtures:
        for tid in (m["home_id"], m["away_id"]):
            key = (int(tid), int(m["league_id"] or 0), int(m["season"] or 0))
            if key not in histories:
                histories[key] = _football_team_history(*key)

    profiles = _football_profiles(histories)
    candidates = []
    for m in fixtures:
        hp = profiles.get((int(m["home_id"]), int(m["league_id"] or 0), int(m["season"] or 0)), {})
        ap = profiles.get((int(m["away_id"]), int(m["league_id"] or 0), int(m["season"] or 0)), {})
        markets = {}

        for metric in ("corners", "shots", "cards"):
            h, a = hp.get(metric), ap.get(metric)
            if h and a and h[1] >= 3 and a[1] >= 3:
                markets[metric] = {"home": h[0], "away": a[0], "expected": h[0] + a[0]}

        hs, hc = hp.get("goals_scored"), hp.get("goals_conceded")
        aws, ac = ap.get("goals_scored"), ap.get("goals_conceded")
        if hs and hc and aws and ac and min(hs[1], hc[1], aws[1], ac[1]) >= 3:
            home_expected = (hs[0] + ac[0]) / 2
            away_expected = (aws[0] + hc[0]) / 2
            markets["goals"] = {"home": home_expected, "away": away_expected, "expected": home_expected + away_expected}

        if markets:
            candidates.append({"match": m, "markets": markets, "datetime": _dt(m["date"])})
    return candidates


def _sport_teams(m):
    return m.get("homeTeam") or m.get("home") or {}, m.get("awayTeam") or m.get("away") or {}


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
        return str(team.get("name") or team.get("displayName") or "Unknown")
    return str(team or "Unknown")


def _sport_score(m):
    state = m.get("state") or {}
    score = state.get("score") or m.get("score") or {}
    current = score.get("current") if isinstance(score, dict) else None
    if isinstance(current, str):
        hit = re.search(r"(-?\d+(?:\.\d+)?)\s*[-:]\s*(-?\d+(?:\.\d+)?)", current)
        if hit:
            return float(hit.group(1)), float(hit.group(2))
    if isinstance(current, dict):
        h = _number(current.get("home") or current.get("homeTeam"))
        a = _number(current.get("away") or current.get("awayTeam"))
        if h is not None and a is not None:
            return h, a
    if isinstance(score, dict):
        h = _number(score.get("home") or score.get("homeTeam"))
        a = _number(score.get("away") or score.get("awayTeam"))
        if h is not None and a is not None:
            return h, a

    # Some sport-specific responses expose the final score directly.
    h = _number(m.get("homeScore") or m.get("homeTeamScore"))
    a = _number(m.get("awayScore") or m.get("awayTeamScore"))
    if h is not None and a is not None:
        return h, a
    return None


def _sport_finished(m):
    """last-five-games is documented by Highlightly as finished games only."""
    return _sport_score(m) is not None


def _sport_league(m):
    league = m.get("league") or {}
    if isinstance(league, dict):
        name = league.get("name") or league.get("leagueName") or ""
        season = league.get("season") or m.get("season")
        country = league.get("country") or {}
        if isinstance(country, dict):
            country = country.get("name") or country.get("countryName") or ""
    else:
        name, season, country = str(league), None, ""
    if not country:
        country = m.get("country") or m.get("countryName") or ""
        if isinstance(country, dict):
            country = country.get("name") or ""
    return str(name), str(country), season


def _sport_fixtures(sport, start, end):
    out = {}
    day = start.date()
    while day <= end.date():
        rows = _get(
            f"{SPORT_BASE}/{sport}/matches", SPORT_HOST,
            {"date": day.isoformat(), "timezone": "Europe/Sofia", "limit": 100},
        )
        print(f"SPORT FIXTURES API: sport={sport} date={day.isoformat()} rows={len(rows)}")
        if rows:
            print(f"SPORT FIRST RAW: {sport}:", rows[0])
        for m in rows:
            dt = _dt(m.get("date") or m.get("startTime") or m.get("startDate"))
            if not dt or not (start <= dt < end) or dt <= datetime.now(TZ):
                continue
            _lg, country, _season = _sport_league(m)
            if _blocked(country):
                continue
            h, a = _sport_teams(m)
            if not _sport_team_id(h) or not _sport_team_id(a):
                continue
            out[m.get("id") or f"{_sport_team_id(h)}-{_sport_team_id(a)}-{dt.isoformat()}"] = m
        day += timedelta(days=1)
    result = list(out.values())
    if not result:
        print(f"SPORT DIAGNOSTIC: {sport} API returned no usable fixture after filtering.")
    return result


def _american_allowed(m):
    league, _country, _season = _sport_league(m)
    s = _norm(league)
    return any(x in s for x in ("nfl", "ncaa", "division i", "division ii", "division 1", "division 2"))


def _sport_last_five(sport, team_id, season):
    rows = _get(
        f"{SPORT_BASE}/{sport}/last-five-games",
        SPORT_HOST,
        {"teamId": int(team_id)},
    )
    games = []
    for m in rows:
        if not isinstance(m, dict) or not _sport_finished(m):
            continue
        _lg, country, game_season = _sport_league(m)
        if _blocked(country):
            continue
        # Highlightly's last-five endpoint is already the team's finished-game
        # history. If season is present, enforce it; if the endpoint omits it,
        # keep the real game instead of throwing away valid data.
        if season is not None and game_season is not None:
            try:
                if int(game_season) != int(season):
                    continue
            except Exception:
                pass
        games.append(m)
        if len(games) == 5:
            break
    return games


def _sport_average(sport, team_id, season):
    games = _sport_last_five(sport, team_id, season)
    values = []
    for m in games:
        h, a = _sport_teams(m)
        score = _sport_score(m)
        if score is None:
            continue
        if _sport_team_id(h) == int(team_id):
            values.append(score[0])
        elif _sport_team_id(a) == int(team_id):
            values.append(score[1])
    if len(values) < 3:
        return None
    return sum(values) / len(values)


def _format_item(i, c):
    m = c["match"]
    return (
        f"{i}. {c['home']} - {c['away']}\n"
        f"   {c['home_avg']:.2f} + {c['away_avg']:.2f} = {c['expected']:.2f}\n"
        f"   Лига: {c['league'] or '-'}\n"
        f"   Държава: {c['country'] or '-'}\n"
        f"   Начало: {c['datetime'].strftime('%H:%M')} BG"
    )


def _top_section(title, candidates):
    lines = [title, "", "🔥 НАД"]
    high = sorted(candidates, key=lambda x: x["expected"], reverse=True)[:5]
    low = sorted(candidates, key=lambda x: x["expected"])[:5]
    if not high:
        lines.append("Няма достатъчно исторически статистически данни.")
    else:
        for i, c in enumerate(high, 1):
            lines.append(_format_item(i, c))
            if i < len(high):
                lines.append("")
    lines += ["", "❄️ ПОД"]
    if not low:
        lines.append("Няма достатъчно исторически статистически данни.")
    else:
        for i, c in enumerate(low, 1):
            lines.append(_format_item(i, c))
            if i < len(low):
                lines.append("")
    return "\n".join(lines)


def _send_chunks(message, send_func, limit=3700):
    if not send_func:
        return
    chunks = []
    current = ""
    for section in message.split("\n────────────────────\n"):
        section = section.strip()
        if not section:
            continue
        candidate = section if not current else current + "\n\n────────────────────\n\n" + section
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = section
    if current:
        chunks.append(current)
    for chunk in chunks:
        send_func(chunk)


def run_football_scanner(send_func=None, reference_date=None):
    ref = reference_date or datetime.now(TZ).date()
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    started = time.time()

    fixtures = _football_fixtures(start, end)
    candidates = _football_candidates(fixtures)

    lines = [
        "📊 ⚽ FOOTBALL DAILY STATISTICAL SCANNER",
        ref.strftime("%d.%m.%Y"),
        f"Мачове: {len(fixtures)}",
        "История: всички налични завършени мачове от текущия сезон",
        "",
    ]
    labels = [("corners", "🚩 КОРНЕРИ"), ("cards", "🟨 КАРТОНИ"), ("shots", "🎯 УДАРИ"), ("goals", "⚽ ГОЛОВЕ")]
    for metric, label in labels:
        arr = []
        for c in candidates:
            if metric in c["markets"]:
                x = c["markets"][metric]
                m = c["match"]
                arr.append({
                    "home": m["home"], "away": m["away"], "league": m["league"],
                    "country": m["country"], "datetime": c["datetime"],
                    "home_avg": x["home"], "away_avg": x["away"], "expected": x["expected"],
            })
        lines.append(_top_section(label, arr))
        lines.append("")
    lines.append(f"⏱ Scan time: {time.time() - started:.1f}s")
    message = "\n".join(lines)
    print(message)
    _send_chunks(message, send_func)
    return message


def run_sport_daily_scanner(send_func=None):
    started = time.time()
    now = datetime.now(TZ)
    start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    lines = [
        "📊 🏆 SPORTS DAILY STATISTICAL SCANNER",
        now.strftime("%d.%m.%Y"),
        f"Период: {start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "История: последните 5 завършени мача от текущия сезон",
        "",
    ]

    for sport, (title, _metric) in SPORTS.items():
        fixtures = _sport_fixtures(sport, start, end)
        if sport == "american-football":
            fixtures = [m for m in fixtures if _american_allowed(m)]
        candidates = []
        for m in fixtures:
            h, a = _sport_teams(m)
            hid, aid = _sport_team_id(h), _sport_team_id(a)
            _lg, country, season = _sport_league(m)
            if not hid or not aid:
                continue
            ha = _sport_average(sport, hid, season)
            aa = _sport_average(sport, aid, season)
            dt = _dt(m.get("date") or m.get("startTime") or m.get("startDate"))
            if ha is None or aa is None or not dt:
                continue
            candidates.append({
                "match": m, "home": _sport_team_name(h), "away": _sport_team_name(a),
                "league": _lg, "country": country, "datetime": dt,
                "home_avg": ha, "away_avg": aa, "expected": ha + aa,
            })

        print(f"SPORT RESULT: {sport} fixtures={len(fixtures)} valid={len(candidates)}")
        lines.append(_top_section(title, candidates))
        lines += ["", "────────────────────", ""]

    lines.append(f"⏱ Scan time: {time.time() - started:.1f}s")
    message = "\n".join(lines)
    print(message)
    _send_chunks(message, send_func)
    return message


def run_due_scans(send_func):
    """Called by main.py. Runs both scanners once per day after 10:30 BG."""
    today = datetime.now(TZ).date()
    key_file = "scanner_last_run.txt"
    today_key = today.isoformat()

    last = ""
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            last = f.read().strip()
    except OSError:
        pass

    now = datetime.now(TZ)
    if now.hour < 10 or (now.hour == 10 and now.minute < 30):
        return False
    if last == today_key:
        return True

    print("=" * 60)
    print("SIMPLE SCANNER START")
    print("DATE:", today_key)
    print("=" * 60)

    try:
        run_football_scanner(send_func, today)
    except Exception as exc:
        print("FOOTBALL SCANNER ERROR:", repr(exc))

    try:
        run_sport_daily_scanner(send_func)
    except Exception as exc:
        print("SPORT SCANNER ERROR:", repr(exc))

    try:
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(today_key)
    except OSError as exc:
        print("SCANNER RUN MARK ERROR:", repr(exc))

    print("SIMPLE SCANNER FINISHED")
    return True


if __name__ == "__main__":
    print("SIMPLE FOOTBALL + SPORTS SCANNER")
