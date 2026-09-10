# =========================================================
# OTHER SPORTS DAILY SCANNER — SOFASCORE
# =========================================================
# Drop-in replacement for the old API-Sports Other Sports scanner.
#
# Runs once daily from main.py at/after 10:00 BG.
# Window: 12:00 today -> 12:00 tomorrow.
# Sports: basketball, ice hockey, handball, rugby, American football,
#        baseball.
#
# HISTORY RULE:
#   1) current season only
#   2) same tournament first
#   3) then other official tournaments from the SAME season
#   4) minimum 3 completed observations per team
#   5) never use previous seasons
#
# SofaScore public web endpoints are used with a browser User-Agent.
# =========================================================

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from config import CHAT_ID  # noqa: F401
except Exception:
    CHAT_ID = None

TZ = ZoneInfo("Europe/Sofia")
SOFASCORE_BASE = "https://www.sofascore.com/api/v1"
SOFASCORE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.sofascore.com/",
}

# SofaScore sport slugs used by its scheduled-events routes.
SPORTS = {
    "basketball": {"label": "🏀 БАСКЕТБОЛ", "slug": "basketball"},
    "hockey": {"label": "🏒 ХОКЕЙ", "slug": "ice-hockey"},
    "handball": {"label": "🤾 ХАНДБАЛ", "slug": "handball"},
    "rugby": {"label": "🏉 РЪГБИ", "slug": "rugby"},
    "american_football": {"label": "🏈 NFL / NCAA", "slug": "american-football"},
    "baseball": {"label": "⚾ БЕЙЗБОЛ", "slug": "baseball"},
}

# The prediction is based on the sport's basic scoring unit.
# We deliberately keep this simple, exactly like the old football scanner:
# average the team's actual scores and add home + away averages.
SCORE_KEYS = {
    "basketball": ("home", "away"),
    "hockey": ("home", "away"),
    "handball": ("home", "away"),
    "rugby": ("home", "away"),
    "american_football": ("home", "away"),
    "baseball": ("home", "away"),
}

_SESSION = requests.Session()
_SESSION.headers.update(SOFASCORE_HEADERS)
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.20
_HISTORY_CACHE = {}
_EVENTS_CACHE = {}


def _get(path, params=None, timeout=20):
    """Small, throttled GET wrapper. No API-Sports key and no retries storm."""
    global _LAST_CALL
    wait = _MIN_INTERVAL - (time.monotonic() - _LAST_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL = time.monotonic()

    url = f"{SOFASCORE_BASE}{path}"
    try:
        response = _SESSION.get(url, params=params or {}, timeout=timeout)
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print("SOFASCORE REQUEST ERROR:", path, repr(exc))
        return None
    except ValueError as exc:
        print("SOFASCORE JSON ERROR:", path, repr(exc))
        return None


def _event_time(event):
    ts = event.get("startTimestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ)
    except (TypeError, ValueError, OSError):
        return None


def _event_id(event):
    try:
        return int(event.get("id"))
    except (TypeError, ValueError):
        return None


def _team_id(team):
    try:
        return int(team.get("id"))
    except (TypeError, ValueError):
        return None


def _season_id(event):
    season = event.get("season") or {}
    value = season.get("id")
    return str(value) if value is not None else ""


def _tournament_id(event):
    tournament = event.get("tournament") or {}
    unique = event.get("uniqueTournament") or tournament.get("uniqueTournament") or {}
    value = unique.get("id") or tournament.get("id")
    return str(value) if value is not None else ""


def _tournament_name(event):
    tournament = event.get("tournament") or {}
    unique = event.get("uniqueTournament") or tournament.get("uniqueTournament") or {}
    return unique.get("name") or tournament.get("name") or "-"


def _country_name(event):
    tournament = event.get("tournament") or {}
    category = event.get("category") or tournament.get("category") or {}
    return category.get("name") or tournament.get("country", {}).get("name") or "-"


def _status_finished(event):
    status = event.get("status") or {}
    typ = str(status.get("type") or "").lower()
    code = status.get("code")
    if typ in {"finished", "ended", "after_penalties", "after_extra_time"}:
        return True
    # SofaScore commonly uses code 100 for finished events.
    return code == 100


def _is_upcoming(event, start, end):
    dt = _event_time(event)
    if not dt or dt < start or dt >= end:
        return False
    status = event.get("status") or {}
    return str(status.get("type") or "").lower() in {
        "notstarted", "scheduled", "created", "postponed", "delayed"
    } and str(status.get("type") or "").lower() not in {"postponed", "delayed"}


def _score(event, side):
    score = event.get(f"{side}Score") or {}
    # Different sports can expose current/period scores; current is the
    # final/basic total for completed events.
    for key in ("current", "normaltime", "overtime", "extra1", "extra2"):
        value = score.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _score_total(event):
    home = _score(event, "home")
    away = _score(event, "away")
    if home is None or away is None:
        return None
    return home, away


def _scheduled_events(sport_slug, day):
    key = (sport_slug, day.isoformat())
    if key in _EVENTS_CACHE:
        return _EVENTS_CACHE[key]
    payload = _get(f"/sport/{sport_slug}/scheduled-events/{day.isoformat()}")
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if not isinstance(events, list):
        events = []
    _EVENTS_CACHE[key] = events
    return events


def get_fixtures(sport, start, end):
    slug = SPORTS[sport]["slug"]
    result = []
    seen = set()
    day = start.date()
    while day <= end.date():
        for event in _scheduled_events(slug, day):
            eid = _event_id(event)
            if not eid or eid in seen:
                continue
            if not _is_upcoming(event, start, end):
                continue
            home = event.get("homeTeam") or {}
            away = event.get("awayTeam") or {}
            if not _team_id(home) or not _team_id(away):
                continue
            seen.add(eid)
            result.append(event)
        day += timedelta(days=1)
    result.sort(key=lambda e: _event_time(e) or datetime.max.replace(tzinfo=TZ))
    return result


def _team_history_page(team_id, page):
    payload = _get(f"/team/{team_id}/events/last/{page}")
    events = payload.get("events", []) if isinstance(payload, dict) else []
    return events if isinstance(events, list) else []


def get_team_history(team_id, season_id, tournament_id=None, max_games=12):
    """Current-season history only; same tournament is preferred locally."""
    cache_key = (int(team_id), str(season_id))
    if cache_key not in _HISTORY_CACHE:
        all_current = []
        seen = set()
        # Fetch a few pages only until we have enough current-season games.
        # Pages are chronological from newest backwards.
        for page in range(4):
            page_events = _team_history_page(team_id, page)
            if not page_events:
                break
            for event in page_events:
                eid = _event_id(event)
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                if not _status_finished(event):
                    continue
                if _season_id(event) != str(season_id):
                    continue
                if _score_total(event) is None:
                    continue
                all_current.append(event)
            # Once we have 12 current-season games, no need to go deeper.
            if len(all_current) >= max_games:
                break
        all_current.sort(key=lambda e: _event_time(e) or datetime.min.replace(tzinfo=TZ), reverse=True)
        _HISTORY_CACHE[cache_key] = all_current

    current = list(_HISTORY_CACHE.get(cache_key, []))
    if tournament_id:
        same = [e for e in current if _tournament_id(e) == str(tournament_id)]
        other = [e for e in current if _tournament_id(e) != str(tournament_id)]
        return (same + other)[:max_games]
    return current[:max_games]


def _team_average(team_id, history):
    values = []
    for event in history:
        home = event.get("homeTeam") or {}
        away = event.get("awayTeam") or {}
        scores = _score_total(event)
        if not scores:
            continue
        hs, aws = scores
        if _team_id(home) == int(team_id):
            values.append(hs)
        elif _team_id(away) == int(team_id):
            values.append(aws)
    if len(values) < 3:
        return None
    return sum(values) / len(values), len(values)


def _prediction(event, sport):
    home = event.get("homeTeam") or {}
    away = event.get("awayTeam") or {}
    season = _season_id(event)
    tournament = _tournament_id(event)
    if not season:
        return None
    hhist = get_team_history(_team_id(home), season, tournament)
    ahist = get_team_history(_team_id(away), season, tournament)
    havg = _team_average(_team_id(home), hhist)
    aavg = _team_average(_team_id(away), ahist)
    if not havg or not aavg:
        return None
    return {
        "event_id": _event_id(event),
        "home": home.get("name") or "HOME",
        "away": away.get("name") or "AWAY",
        "home_avg": havg[0],
        "away_avg": aavg[0],
        "expected": havg[0] + aavg[0],
        "sample": min(havg[1], aavg[1]),
        "league": _tournament_name(event),
        "country": _country_name(event),
        "time": _event_time(event),
    }


def _format_sport(sport, predictions):
    label = SPORTS[sport]["label"]
    predictions = sorted(predictions, key=lambda x: x["expected"], reverse=True)
    high = predictions[:3]
    high_ids = {x["event_id"] for x in high}
    low = sorted(
        [x for x in predictions if x["event_id"] not in high_ids],
        key=lambda x: x["expected"],
    )[:3]

    lines = [label, "🔥 TOP 3 НАД"]
    if high:
        for i, p in enumerate(high, 1):
            lines.extend([
                f"{i}. {p['home']} - {p['away']}",
                f"   Очаквано: {p['home_avg']:.2f} + {p['away_avg']:.2f} = {p['expected']:.2f}",
                f"   {p['league']} | {p['country']} | {p['time'].strftime('%d.%m %H:%M')} BG",
            ])
            if i < len(high):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистика.")

    lines.extend(["", "❄️ TOP 3 ПОД"])
    if low:
        for i, p in enumerate(low, 1):
            lines.extend([
                f"{i}. {p['home']} - {p['away']}",
                f"   Очаквано: {p['home_avg']:.2f} + {p['away_avg']:.2f} = {p['expected']:.2f}",
                f"   {p['league']} | {p['country']} | {p['time'].strftime('%d.%m %H:%M')} BG",
            ])
            if i < len(low):
                lines.append("")
    else:
        lines.append("Няма достатъчно статистика.")
    return "\n".join(lines)


def run_other_sports_scanner(reference_date=None, send_func=None):
    """Run once daily: next 24 hours, six non-football sports."""
    ref = reference_date or datetime.now(TZ).date()
    start = datetime(ref.year, ref.month, ref.day, 12, 0, tzinfo=TZ)
    end = start + timedelta(hours=24)

    lines = [
        "🌍 DAILY OTHER SPORTS SCANNER — SOFASCORE",
        ref.strftime("%d.%m.%Y"),
        "10:00 BG → срещи 12:00 днес до 12:00 утре",
        "История: текущ сезон; първо същият турнир, после други официални турнири",
        "Източник: SofaScore",
        "",
    ]

    total_fixtures = 0
    total_valid = 0

    for sport in SPORTS:
        try:
            fixtures = get_fixtures(sport, start, end)
            total_fixtures += len(fixtures)
            predictions = []
            for event in fixtures:
                prediction = _prediction(event, sport)
                if prediction:
                    predictions.append(prediction)
            total_valid += len(predictions)
            lines.append(_format_sport(sport, predictions))
            lines.append("")
            print(
                f"SOFASCORE OTHER [{sport}]: fixtures={len(fixtures)} valid={len(predictions)}"
            )
        except Exception as exc:
            print(f"SOFASCORE OTHER ERROR [{sport}]: {exc!r}")
            lines.extend([SPORTS[sport]["label"], "Грешка при зареждането на данните.", ""])

    lines.append(f"Мачове: {total_fixtures} | Валидни статистически сигнали: {total_valid}")
    message = "\n".join(lines)
    print(f"SOFASCORE OTHER MESSAGE READY — {total_valid} valid / {total_fixtures} fixtures")
    if send_func:
        send_func(message)
    return message


if __name__ == "__main__":
    print(run_other_sports_scanner())
