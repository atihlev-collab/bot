# SPORT-ONLY MAIN — HOURLY PREMATCH
# Football and LIVE are intentionally disabled here.
# SPORT keeps its own Highlightly provider; never use API-Football in this file.

import logging
import threading
import time
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

from config import BOT_TOKEN, CHAT_ID, HIGHLIGHTLY_API_KEY

TZ = ZoneInfo("Europe/Sofia")
DB_FILE = "v3_ai.db"
SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_HOST = "sport-highlights-api.p.rapidapi.com"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100

SPORT_DONE_HOUR = None
SPORT_RUNNING = False
SPORT_API_DAILY_LIMIT = 25000
SPORT_API_SAFETY_RESERVE = 500
SPORT_API_HARD_STOP = SPORT_API_DAILY_LIMIT - SPORT_API_SAFETY_RESERVE
SPORT_API_MIN_INTERVAL = 0.18
_SPORT_API_LAST_REQUEST_AT = 0.0
_SPORT_API_REQUEST_LOCK = threading.Lock()
_SPORT_API_CALLS = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", force=True)

class APIQuotaExceeded(Exception):
    pass

def _db():
    return sqlite3.connect(DB_FILE, timeout=30)

def _quota_init():
    with _db() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE TABLE IF NOT EXISTS sport_api_usage (day TEXT NOT NULL, provider TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, provider))")
        conn.execute("CREATE TABLE IF NOT EXISTS sport_api_quota_locks (provider TEXT PRIMARY KEY, locked_day TEXT NOT NULL, reason TEXT NOT NULL)")

def _sport_today():
    return datetime.now(TZ).date().isoformat()

def _quota_locked(scope="highlightly_sport"):
    _quota_init()
    today = _sport_today()
    with _db() as conn:
        row = conn.execute("SELECT locked_day FROM sport_api_quota_locks WHERE provider=?", (scope,)).fetchone()
        if row and row[0] == today:
            return True
        if row:
            conn.execute("DELETE FROM sport_api_quota_locks WHERE provider=?", (scope,))
    return False

def _set_quota_lock(scope="highlightly_sport", reason="provider quota exhausted"):
    _quota_init()
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO sport_api_quota_locks(provider, locked_day, reason) VALUES(?,?,?)", (scope, _sport_today(), str(reason)[:300]))

def _reserve_sport_request(scope="highlightly_sport"):
    """Atomically reserve one Highlightly request and keep a 500-request safety reserve."""
    _quota_init()
    today = _sport_today()
    with _db() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        lock = conn.execute("SELECT locked_day, reason FROM sport_api_quota_locks WHERE provider=?", (scope,)).fetchone()
        if lock and lock[0] == today:
            row = conn.execute("SELECT used FROM sport_api_usage WHERE day=? AND provider=?", (today, scope)).fetchone()
            return False, int(row[0]) if row else 0, f"daily lock: {lock[1]}"
        if lock:
            conn.execute("DELETE FROM sport_api_quota_locks WHERE provider=?", (scope,))
        conn.execute("INSERT OR IGNORE INTO sport_api_usage(day, provider, used) VALUES(?,?,0)", (today, scope))
        row = conn.execute("SELECT used FROM sport_api_usage WHERE day=? AND provider=?", (today, scope)).fetchone()
        used = int(row[0]) if row else 0
        if used >= SPORT_API_HARD_STOP:
            conn.execute("INSERT OR REPLACE INTO sport_api_quota_locks(provider, locked_day, reason) VALUES(?,?,?)", (scope, today, "local safety hard stop"))
            return False, used, "local safety hard stop"
        used += 1
        conn.execute("UPDATE sport_api_usage SET used=? WHERE day=? AND provider=?", (used, today, scope))
        return True, used, "reserved"

def _sport_api_get(endpoint, params=None):
    """Highlightly-only adapter with persisted budget control and request pacing."""
    global _SPORT_API_CALLS, _SPORT_API_LAST_REQUEST_AT
    params = dict(params or {})
    scope = "highlightly_sport"
    if _quota_locked(scope):
        raise APIQuotaExceeded("Highlightly Sport daily quota locked")
    try:
        with _SPORT_API_REQUEST_LOCK:
            delay = SPORT_API_MIN_INTERVAL - (time.monotonic() - _SPORT_API_LAST_REQUEST_AT)
            if delay > 0:
                time.sleep(delay)
            reserved, used, reason = _reserve_sport_request(scope)
            if not reserved:
                print(f"SPORT API REQUEST BLOCKED | Highlightly | used={used}/{SPORT_API_HARD_STOP} | endpoint={endpoint} | reason={reason}", flush=True)
                raise APIQuotaExceeded("Highlightly Sport daily budget exhausted")
            _SPORT_API_LAST_REQUEST_AT = time.monotonic()
            _SPORT_API_CALLS += 1
            response = requests.get(
                f"{SPORT_API_BASE}/{endpoint.lstrip('/')}",
                headers={"x-rapidapi-key": HIGHLIGHTLY_API_KEY, "x-rapidapi-host": SPORT_API_HOST},
                params=params,
                timeout=10,
            )
        print(f"SPORT API REQUEST {_SPORT_API_CALLS} | provider=Highlightly | used={used}/{SPORT_API_HARD_STOP} | endpoint={endpoint} | params={params} | status={response.status_code}", flush=True)
        if response.status_code == 429:
            _set_quota_lock(scope, "HTTP 429 from Highlightly Sport")
            print("SPORT QUOTA LOCKED | Highlightly HTTP 429 until Bulgaria day changes", flush=True)
            raise APIQuotaExceeded("Highlightly Sport returned HTTP 429")
        if response.status_code in (401, 403):
            print(f"SPORT API AUTH/ACCESS ERROR | status={response.status_code} | body={response.text[:500]}", flush=True)
            return []
        if response.status_code != 200:
            print(f"SPORT API HTTP ERROR | status={response.status_code} | endpoint={endpoint} | body={response.text[:500]}", flush=True)
            return []
        try:
            payload = response.json()
        except ValueError:
            print(f"SPORT API INVALID JSON | endpoint={endpoint} | body={response.text[:300]}", flush=True)
            return []
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        if isinstance(payload, dict):
            plan = payload.get("plan") or {}
            if isinstance(plan, dict) and (plan.get("tier") or plan.get("message")):
                print(
                    f"SPORT API PLAN | endpoint={endpoint} | tier={plan.get('tier', 'unknown')} "
                    f"| message={str(plan.get('message', ''))[:180]}",
                    flush=True,
                )
        if not isinstance(data, list):
            print(
                f"SPORT API DATA SHAPE ERROR | endpoint={endpoint} | "
                f"payload_type={type(payload).__name__} | data_type={type(data).__name__} "
                f"| top_keys={list(payload.keys())[:15] if isinstance(payload, dict) else 'n/a'}",
                flush=True,
            )
            return []
        if endpoint.endswith("/odds") and data:
            first = data[0] if isinstance(data[0], dict) else {}
            markets = first.get("odds") or first.get("markets") or []
            first_market = markets[0] if isinstance(markets, list) and markets and isinstance(markets[0], dict) else {}
            print(
                f"SPORT API ODDS SHAPE | endpoint={endpoint} | rows={len(data)} "
                f"| row_keys={list(first.keys())[:15]} | market_count={len(markets) if isinstance(markets, list) else 'non-list'} "
                f"| market_keys={list(first_market.keys())[:12]} "
                f"| market={str(first_market.get('market') or first_market.get('name') or first_market.get('type') or '')[:80]}",
                flush=True,
            )
        return data
    except APIQuotaExceeded:
        raise
    except requests.RequestException as exc:
        print(f"SPORT API REQUEST ERROR | endpoint={endpoint} | {exc!r}", flush=True)
        return []
    except Exception as exc:
        print(f"SPORT API UNEXPECTED ERROR | endpoint={endpoint} | {exc!r}", flush=True)
        return []

def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    print(f"SPORT TELEGRAM SENT | status={response.status_code}", flush=True)

# ============================================================
# EMBEDDED SPORT ENGINE — Highlightly Sport Ultra
# ============================================================

def init_scanner_db():
    conn = _db()
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_scanner_runs (run_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sport_sent_fixtures (
        sport_key TEXT NOT NULL,
        match_id TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (sport_key, match_id)
    )""")
    conn.commit()
    conn.close()

def _sent_sport_fixture_keys():
    init_scanner_db()
    conn = _db()
    try:
        rows = conn.execute("SELECT sport_key, match_id FROM sport_sent_fixtures").fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}
    finally:
        conn.close()

def _mark_sent_sport_fixtures(items):
    if not items:
        return
    init_scanner_db()
    conn = _db()
    try:
        sent_at = datetime.now(TZ).isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO sport_sent_fixtures(sport_key, match_id, sent_at) VALUES(?,?,?)",
            [(str(item["sport_key"]), str(item["match_id"]), sent_at) for item in items],
        )
        conn.commit()
    finally:
        conn.close()

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
        if total>1: chunk=f"📊 SPORT DAILY PREMATCH ({i}/{total})\n\n"+chunk
        send_func(chunk)

SPORTS_CONFIG = {
    "basketball": {
        "name": "🏀 БАСКЕТБОЛ",
        "endpoint": "basketball/matches",
        "metric": "points"
    },
    "hockey": {
        "name": "🏒 ХОКЕЙ",
        "endpoint": "hockey/matches",
        "metric": "goals"
    },
    "american-football": {
        "name": "🏈 NFL / NCAA — Division I / Division II",
        "endpoint": "american-football/matches",
        "metric": "points"
    },
    "baseball": {
        "name": "⚾ БЕЙЗБОЛ",
        "endpoint": "baseball/matches",
        "metric": "runs"
    },
    "rugby": {
        "name": "🏉 РЪГБИ",
        "endpoint": "rugby/matches",
        "metric": "points"
    },
    "volleyball": {
        "name": "🏐 ВОЛЕЙБОЛ",
        "endpoint": "volleyball/matches",
        "metric": "points"
    },
    "handball": {
        "name": "🤾 ХАНДБАЛ",
        "endpoint": "handball/matches",
        "metric": "goals"
    },
}

# Hard limits: prevent one sport with many fixtures from exhausting the daily
# Sport API quota before the scanner reaches the other sports.
MAX_FIXTURES_TO_EVALUATE = 100
MAX_CONTEXT_CANDIDATES = 12

# HARD SIGNAL RULES — never publish weak statistical picks.
MIN_SIGNAL_PROBABILITY = 65.0
MIN_SIGNAL_CONFIDENCE = 75.0
MAX_SIGNAL_RISK = 35.0
MIN_VALUE_EDGE = -2.0
MIN_ODDS = 1.50
MAX_SIGNALS_PER_SPORT = 5


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


def _match_id(match):
    value = match.get("id") or match.get("matchId")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_label(value):
    # Keep both Latin and Cyrillic characters. The previous normalizer
    # stripped Cyrillic, so labels such as "ПОБЕДИТЕЛ: HOME" became only
    # "home" and the winner/handicap Betano matcher could never recognize them.
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9.+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _odd_for_market(sport_key, match, market_label):
    """Return the Betano odd for the exact model market, or None.

    Supports winner, match totals, team totals and handicap/spread labels.
    No approximate line is accepted: the bookmaker line must match the model.
    """
    mid = _match_id(match)
    if not mid:
        return None

    rows = _sport_api_get(
        f"{sport_key}/odds",
        {
            "matchId": mid,
            "oddsType": "prematch",
            "limit": 5,
            "offset": 0,
        },
    )
    if not isinstance(rows, list):
        return None

    wanted = _normalize_label(market_label)
    numbers = re.findall(r"[-+]?[0-9]+(?:\.[0-9]+)?", wanted)
    line = numbers[-1] if numbers else None
    is_home = "home" in wanted
    is_away = "away" in wanted
    is_over = "over" in wanted
    is_under = "under" in wanted
    is_handicap = "хендикап" in wanted or "handicap" in wanted or "spread" in wanted
    is_team_total = "team over" in wanted or "team under" in wanted
    is_match_total = (is_over or is_under) and not is_team_total

    def line_matches(value, market_name):
        nums = re.findall(r"[-+]?[0-9]+(?:\.[0-9]+)?", _normalize_label(value) + " " + _normalize_label(market_name))
        if not line:
            return True
        target = float(line)
        for n in nums:
            try:
                if abs(float(n) - target) < 1e-6:
                    return True
            except ValueError:
                pass
        return False

    for row in rows:
        if not isinstance(row, dict):
            continue
        bookmaker_value = row.get("bookmakerName") or row.get("bookmaker") or row.get("bookmaker_name") or ""
        if isinstance(bookmaker_value, dict):
            bookmaker_value = bookmaker_value.get("name") or bookmaker_value.get("bookmakerName") or bookmaker_value.get("title") or ""
        bookmaker = str(bookmaker_value).strip().lower()

        market_list = row.get("odds") or row.get("markets") or []
        if not isinstance(market_list, list):
            continue

        for market in market_list:
            if not isinstance(market, dict):
                continue
            market_name = str(market.get("market") or market.get("name") or market.get("type") or "")
            norm_market = _normalize_label(market_name)
            values = market.get("values") or market.get("odds") or market.get("selections") or []
            if not isinstance(values, list):
                continue

            for selection in values:
                if not isinstance(selection, dict):
                    continue
                value = str(selection.get("value") or selection.get("label") or selection.get("name") or "").strip()
                try:
                    odd = float(selection.get("odd") if selection.get("odd") is not None else selection.get("odds"))
                except (TypeError, ValueError):
                    continue
                if odd < MIN_ODDS:
                    continue

                nv = _normalize_label(value)
                combined = f"{norm_market} {nv}"

                # Winner / moneyline.
                if ("победител home" in wanted or wanted == "home"):
                    if (nv == "home" or nv == "1" or "home" in nv) and any(k in combined for k in ("winner", "moneyline", "match winner", "1x2", "победител")):
                        return odd
                if ("победител away" in wanted or wanted == "away"):
                    if (nv == "away" or nv == "2" or "away" in nv) and any(k in combined for k in ("winner", "moneyline", "match winner", "1x2", "победител")):
                        return odd

                # Match total or team total. Require the exact line.
                if (is_match_total or is_team_total) and (is_over or is_under):
                    if (is_over and "over" not in nv) or (is_under and "under" not in nv):
                        continue
                    if not line_matches(value, market_name):
                        continue
                    if is_team_total:
                        # Team-total markets normally expose the team name or
                        # home/away in either market or selection.
                        if is_home and not ("home" in combined or _normalize_label(_sport_team_name(match.get("homeTeam") or match.get("home") or {})) in combined):
                            continue
                        if is_away and not ("away" in combined or _normalize_label(_sport_team_name(match.get("awayTeam") or match.get("away") or {})) in combined):
                            continue
                    return odd

                # Handicap / spread. Match exact sign/line and side.
                if is_handicap and line_matches(value, market_name):
                    side_ok = (is_home and ("home" in combined or "1" in nv)) or (is_away and ("away" in combined or "2" in nv))
                    if side_ok:
                        return odd

    return None


def _available_prematch_options(sport_key, match):
    """Read available prematch markets and return only conservative options.

    SPORT intentionally has no historical/statistical model. The selection is
    based on markets returned by the odds provider for the exact fixture.
    Keep standard prematch market families and rank by highest available odds
    within the configured 1.50–1.80 range. This is odds ranking, not a claim
    that the highest odds have the highest chance of winning.
    """
    mid = _match_id(match)
    if not mid:
        return []

    rows = _sport_api_get(
        f"{sport_key}/odds",
        {
            "matchId": mid,
            "oddsType": "prematch",
            "limit": 5,
            "offset": 0,
        },
    )
    if not isinstance(rows, list):
        return []

    home, away = _sport_match_names(match)
    allowed_market_words = (
        "winner", "moneyline", "match winner", "1x2", "total",
        "over", "under", "handicap", "spread", "победител",
        "общо", "над", "под", "хендикап",
    )
    blocked_words = (
        "player", "first scorer", "next", "period", "quarter",
        "half", "set", "inning", "map", "special", "boost",
    )

    options = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_bookmaker = row.get("bookmakerName") or row.get("bookmaker") or row.get("bookmaker_name") or ""
        if isinstance(row_bookmaker, dict):
            row_bookmaker = row_bookmaker.get("name") or row_bookmaker.get("bookmakerName") or row_bookmaker.get("title") or ""

        market_list = row.get("odds") or row.get("markets") or []
        if not isinstance(market_list, list):
            continue

        for market in market_list:
            if not isinstance(market, dict):
                continue
            market_name = str(
                market.get("market") or market.get("name") or market.get("type") or ""
            ).strip()
            bookmaker_value = (
                market.get("bookmakerName") or market.get("bookmaker") or
                market.get("bookmaker_name") or row_bookmaker
            )
            if isinstance(bookmaker_value, dict):
                bookmaker_value = (
                    bookmaker_value.get("name") or bookmaker_value.get("bookmakerName") or
                    bookmaker_value.get("title") or ""
                )
            norm_market = _normalize_label(market_name)
            if any(word in norm_market for word in blocked_words):
                continue

            # Volleyball: only full-match winner markets. Do not use totals,
            # over/under points, handicaps, or set-specific winner markets.
            if sport_key == "volleyball" and not any(
                word in norm_market for word in ("match winner", "winner", "moneyline")
            ):
                continue

            values = market.get("values") or market.get("odds") or market.get("selections") or []
            if not isinstance(values, list):
                continue

            for selection in values:
                if not isinstance(selection, dict):
                    continue
                label = str(
                    selection.get("value") or selection.get("label") or selection.get("name") or ""
                ).strip()
                try:
                    odd = float(
                        selection.get("odd")
                        if selection.get("odd") is not None
                        else selection.get("odds")
                    )
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(odd) or odd < MIN_ODDS or odd > 1.80:
                    continue

                norm_label = _normalize_label(label)
                combined = f"{norm_market} {norm_label}"
                if any(word in combined for word in blocked_words):
                    continue

                # Volleyball picks are strictly match winner: home or away.
                # Accept provider labels Home/Away, 1/2, or the actual team names.
                if sport_key == "volleyball":
                    home_label = _normalize_label(home)
                    away_label = _normalize_label(away)
                    if norm_label not in {"home", "away", "1", "2", home_label, away_label}:
                        continue

                core_market = any(word in combined for word in allowed_market_words)
                recognizable_selection = (
                    norm_label in {"1", "2", "x", "home", "away", "draw", "over", "under"}
                    or norm_label == _normalize_label(home)
                    or norm_label == _normalize_label(away)
                    or "over" in norm_label
                    or "under" in norm_label
                    or "home" in norm_label
                    or "away" in norm_label
                )
                if not core_market and not recognizable_selection:
                    continue

                key = (norm_market, _normalize_label(label), round(odd, 3))
                if key in seen:
                    continue
                seen.add(key)

                implied = round((1.0 / odd) * 100.0, 1)
                options.append({
                    "bookmaker": str(bookmaker_value).strip() or "Unknown bookmaker",
                    "market": market_name or "PREMATCH",
                    "selection": label or "-",
                    "odd": odd,
                    "implied": implied,
                })

    # Rugby has returned odds rows but no eligible options in recent logs.
    # Emit compact raw-shape diagnostics only when a rugby fixture produces
    # zero options, so the next run shows whether the provider uses a different
    # selection/odds field or whether the available prices are outside 1.50–1.80.
    if not options and sport_key in {"rugby", "volleyball"}:
        samples = []
        for row in rows[:2]:
            if not isinstance(row, dict):
                continue
            markets = row.get("odds") or row.get("markets") or []
            if not isinstance(markets, list):
                continue
            for market in markets[:3]:
                if not isinstance(market, dict):
                    continue
                values = market.get("values") or market.get("odds") or market.get("selections") or []
                first_value = values[0] if isinstance(values, list) and values else None
                samples.append({
                    "market": str(market.get("market") or market.get("name") or market.get("type") or "")[:60],
                    "values_type": type(values).__name__,
                    "values_count": len(values) if isinstance(values, list) else None,
                    "selection_keys": list(first_value.keys())[:10] if isinstance(first_value, dict) else [],
                    "selection_sample": str(first_value)[:180] if first_value is not None else None,
                })
        print(
            f"SPORT ODDS PARSER DEBUG | sport={sport_key} | match={mid} | "
            f"allowed_range={MIN_ODDS:.2f}-1.80 | samples={samples[:6]}",
            flush=True,
        )

    # Keep the highest eligible odds per fixture; do not publish multiple
    # correlated markets from the same fixture.
    options.sort(key=lambda x: (-x["odd"], x["implied"]))
    return options



def _format_sport_simple_signal(index, item):
    dt = item["datetime"]

    sport_name = item.get("sport_name") or {
        "basketball": "🏀 БАСКЕТБОЛ",
        "hockey": "🏒 ХОКЕЙ",
        "american-football": "🏈 АМЕРИКАНСКИ ФУТБОЛ",
        "baseball": "⚾ БЕЙЗБОЛ",
        "rugby": "🏉 РЪГБИ",
        "volleyball": "🏐 ВОЛЕЙБОЛ",
        "handball": "🤾 ХАНДБАЛ",
    }.get(item.get("sport_key"), "🏟️ СПОРТ")

    return "\n".join([
        f"{index}. {sport_name} — {item['home']} - {item['away']}",
        f"   🎯 Пазар: {item['market']}",
        f"   📊 Избор: {item['selection']}",
        f"   🏪 Букмейкър: {item.get('bookmaker') or 'Unknown bookmaker'}",
        f"   💰 Коефициент: {item['odd']:.2f}",
        f"   📌 Имплицитна вероятност: {item['implied']:.1f}%",
        f"   🏆 Лига: {item['league'] or '-'} | Държава: {item['country'] or '-'}",
        f"   📅 {dt.strftime('%d.%m.%Y')} | {dt.strftime('%H:%M')} BG",
    ])


def run_sport_top3_daily_scanner(send_func=None):
    """SPORT PREMATCH only: no historical/team statistics; rank available bookmaker markets globally."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    hour_key = now_bg.strftime("%Y-%m-%d-%H")
    run_key = f"sport_prematch:{hour_key}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT SCANNER ALREADY RAN THIS HOUR: {hour_key} BG"), flush=True)
        return ""

    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    now_scan = datetime.now(TZ)

    all_signals = []
    seen_matches = set()

    for sport_key, cfg in SPORTS_CONFIG.items():
        try:
            fixtures = _get_sport_fixtures(cfg, start, end)
            future = []
            for match in fixtures:
                dt = _sport_match_datetime(match)
                if dt is None or dt <= now_scan:
                    continue
                league_name, country_name = _sport_league_country(match)
                country_cf = str(country_name or "").strip().casefold()
                league_cf = str(league_name or "").strip().casefold()
                if country_cf in {
                    "russia", "belarus", "русия", "беларус",
                    "philippines", "the philippines", "филипини",
                    "singapore", "сингапур",
                }:
                    print(f"SPORT BLOCKED COUNTRY: {sport_key} {match.get('id')} {country_name}", flush=True)
                    continue
                if "friendly" in league_cf or "приятел" in league_cf:
                    print(f"SPORT BLOCKED FRIENDLY: {sport_key} {match.get('id')} {league_name}", flush=True)
                    continue
                future.append(match)

            future.sort(key=lambda m: _sport_match_datetime(m) or end)
            # Wider fixture pool like the football scanner, with a hard cap
            # to keep daily API usage controlled and leave room for every sport.
            future = future[:MAX_FIXTURES_TO_EVALUATE]
            print(
                f"SPORT FIXTURE SUMMARY | sport={sport_key} fetched={len(fixtures)} "
                f"future_candidates={sum(1 for m in fixtures if (_sport_match_datetime(m) is not None and _sport_match_datetime(m) > now_scan))} "
                f"odds_checks={len(future)}",
                flush=True,
            )

            for match in future:
                try:
                    mid = _match_id(match)
                    fixture_key = (sport_key, str(mid))
                    if not mid or fixture_key in seen_matches:
                        continue
                    options = _available_prematch_options(sport_key, match)
                    print(
                        f"SPORT ODDS RESULT | sport={sport_key} match={mid} "
                        f"safe_options={len(options)}",
                        flush=True,
                    )
                    if not options:
                        continue
                    best = options[0]
                    home, away = _sport_match_names(match, sport_key)
                    league, country = _sport_league_country(match)
                    dt = _sport_match_datetime(match)
                    all_signals.append({
                        "sport_key": sport_key,
                        "sport_name": cfg["name"],
                        "match_id": mid,
                        "home": home,
                        "away": away,
                        "bookmaker": best.get("bookmaker") or "Unknown bookmaker",
                        "market": best["market"],
                        "selection": best["selection"],
                        "odd": best["odd"],
                        "implied": best["implied"],
                        "league": league,
                        "country": country,
                        "datetime": dt,
                    })
                    seen_matches.add(fixture_key)
                except APIQuotaExceeded:
                    raise
                except Exception as exc:
                    print(f"SPORT ODDS ERROR: {sport_key} {exc!r}", flush=True)

        except APIQuotaExceeded:
            print(f"SPORT QUOTA STOP: {sport_key}", flush=True)
            break
        except Exception as exc:
            print(f"SPORT SECTION ERROR: {sport_key}: {exc!r}", flush=True)

    # Rank independently inside each sport. This prevents one sport from
    # taking all available slots just because it has more markets/fixtures.
    already_sent = _sent_sport_fixture_keys()
    unseen_signals = [
        item for item in all_signals
        if (str(item["sport_key"]), str(item["match_id"])) not in already_sent
    ]

    final = []
    selected_by_sport = {}
    for sport_key in SPORTS_CONFIG:
        sport_items = [
            item for item in unseen_signals
            if item["sport_key"] == sport_key
        ]
        sport_items.sort(key=lambda x: (-x["odd"], -x["implied"], str(x["match_id"])))
        selected = sport_items[:MAX_SIGNALS_PER_SPORT]
        final.extend(selected)
        if selected:
            selected_by_sport[sport_key] = len(selected)

    print(
        "SPORT TOP FIVE PER SPORT | "
        f"selected_total={len(final)} | by_sport={selected_by_sport} | "
        f"max_per_sport={MAX_SIGNALS_PER_SPORT} | ranking=highest_odds_first",
        flush=True,
    )

    lines = [
        "📊 SPORT PREMATCH — ЧАСОВА ПРОВЕРКА",
        now_bg.strftime("%d.%m.%Y %H:%M BG"),
        "",
        "🏁 РЕЖИМ: PREMATCH — само срещи, които още не са започнали",
        f"Период: {start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "Подбор: отделно за всеки спорт; до 5 срещи на спорт, подредени по най-висок коефициент",
        "Метод: без историческа статистика; по-висок коефициент не означава по-голяма вероятност за печалба",
        f"Коефициент: {MIN_ODDS:.2f}–1.80 | максимум {MAX_SIGNALS_PER_SPORT} нови сигнала на спорт",
        "Русия, Беларус, Филипини, Сингапур и приятелски мачове — блокирани. Няма филтър само за Betano; LIVE е изключен.",
        "",
    ]

    if final:
        lines.extend(["🎯 PREMATCH — ТОП СРЕЩИ ПО СПОРТОВЕ", ""])
        first_section = True
        for sport_key, cfg in SPORTS_CONFIG.items():
            sport_items = [item for item in final if item["sport_key"] == sport_key]
            if not first_section:
                lines.extend(["", "────────────────────", ""])
            first_section = False
            lines.append(f"{cfg['name']} — ТОП {len(sport_items)}")
            lines.append("")
            if not sport_items:
                lines.append("Няма нови срещи с подходящ коефициент 1.50–1.80.")
                continue
            for i, item in enumerate(sport_items, 1):
                lines.append(_format_sport_simple_signal(i, item))
                if i < len(sport_items):
                    lines.extend(["", "────────────────────", ""])
    else:
        lines.append("🎯 Няма нов подходящ PREMATCH сигнал в този час.")

    lines.extend([
        "",
        f"📡 API заявки: {_SPORT_API_CALLS}",
        f"⏱ Scan time: {time.time() - started:.1f}s",
    ])
    message = "\n".join(lines)
    print(message, flush=True)

    if send_func and final:
        try:
            _send_sport_report_chunks(message, send_func, max_chars=3700)
            _mark_sent_sport_fixtures(final)
            print("SPORT TELEGRAM REPORT SENT", flush=True)
        except Exception as exc:
            print(f"SPORT TELEGRAM ERROR: {exc!r}", flush=True)
            return message
    elif not final:
        print("SPORT HOURLY RESULT | no new qualifying odds; no signal sent", flush=True)

    mark_ran(run_key)
    print(f"SPORT HOURLY COMPLETE | hour={hour_key} | signals={len(final)} | api_calls={_SPORT_API_CALLS}", flush=True)
    return message

def _run_sport(hour_key):
    global SPORT_RUNNING, SPORT_DONE_HOUR
    try:
        print(f"SPORT HOURLY THREAD START | hour={hour_key}", flush=True)
        report = run_sport_top3_daily_scanner(send_telegram)
        print(f"SPORT HOURLY THREAD RESULT | hour={hour_key} | report={bool(report)}", flush=True)
    except APIQuotaExceeded as exc:
        print(f"SPORT HOURLY QUOTA STOP | hour={hour_key} | {exc}", flush=True)
    except Exception as exc:
        print(f"SPORT HOURLY ERROR | hour={hour_key} | {exc!r}", flush=True)
        logging.exception("SPORT HOURLY ERROR")
    finally:
        SPORT_DONE_HOUR = hour_key
        SPORT_RUNNING = False
        print(f"SPORT HOURLY THREAD END | hour={hour_key}", flush=True)

def main_loop():
    global SPORT_RUNNING, SPORT_DONE_HOUR
    print("SPORT-ONLY SYSTEM START | Highlightly Sport | PREMATCH ONLY | LIVE OFF", flush=True)
    print("SPORT SCHEDULER READY | once per clock hour Europe/Sofia | fixture window 12:00 BG -> next day 12:00 BG | hard stop 24,500 Highlightly requests/day", flush=True)
    while True:
        now = datetime.now(TZ)
        hour_key = now.strftime("%Y-%m-%d-%H")
        if not SPORT_RUNNING and SPORT_DONE_HOUR != hour_key:
            hourly_key = f"sport_prematch:{hour_key}"
            if already_ran(hourly_key):
                SPORT_DONE_HOUR = hour_key
                print(f"SPORT SCHEDULER SKIP | hour={hour_key} | already ran", flush=True)
            elif _quota_locked("highlightly_sport"):
                SPORT_DONE_HOUR = hour_key
                print(f"SPORT SCHEDULER SKIP | hour={hour_key} | daily quota lock", flush=True)
            else:
                SPORT_RUNNING = True
                print(f"SPORT HOURLY TRIGGER | hour={hour_key} | time={now:%Y-%m-%d %H:%M:%S} BG", flush=True)
                threading.Thread(target=_run_sport, args=(hour_key,), daemon=True).start()
        time.sleep(30)


if __name__ == "__main__":
    main_loop()


