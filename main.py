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
SPORT_RESULT_CHECK_DONE_HOUR = None
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
        if endpoint.endswith("/odds"):
            plan = payload.get("plan") or {} if isinstance(payload, dict) else {}
            pagination = payload.get("pagination") or {} if isinstance(payload, dict) else {}
            if data:
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
            else:
                print(
                    f"SPORT API ODDS EMPTY | endpoint={endpoint} | params={params} "
                    f"| tier={plan.get('tier', 'unknown') if isinstance(plan, dict) else 'unknown'} "
                    f"| plan_message={str(plan.get('message', '') if isinstance(plan, dict) else '')[:180]} "
                    f"| pagination={pagination}",
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


def _sent_sport_count_today():
    """Count unique fixtures already sent today in Bulgaria time."""
    init_scanner_db()
    now = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sport_sent_fixtures WHERE sent_at >= ? AND sent_at < ?",
            (day_start.isoformat(), next_day.isoformat()),
        ).fetchone()
    return int(row[0] or 0) if row else 0

def already_ran(run_key):
    init_scanner_db(); conn=_db()
    row=conn.execute("SELECT 1 FROM daily_scanner_runs WHERE run_key=?",(run_key,)).fetchone()
    conn.close(); return row is not None

def mark_ran(run_key):
    conn=_db()
    conn.execute("INSERT OR REPLACE INTO daily_scanner_runs(run_key, created_at) VALUES (?, ?)",(run_key,datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()


def _init_tipster_tracking():
    with _db() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("""CREATE TABLE IF NOT EXISTS sport_tipster_predictions (
            sport_key TEXT NOT NULL, match_id TEXT NOT NULL, start_at TEXT,
            home_team_id INTEGER, away_team_id INTEGER,
            home_name TEXT NOT NULL, away_name TEXT NOT NULL,
            selection TEXT NOT NULL, signal_strength REAL NOT NULL,
            expected_home REAL, expected_away REAL, created_at TEXT NOT NULL,
            outcome TEXT, actual_home REAL, actual_away REAL, settled_at TEXT,
            PRIMARY KEY (sport_key, match_id)
        )""")


def _record_tipster_predictions(items):
    _init_tipster_tracking()
    now_iso = datetime.now(TZ).isoformat()
    with _db() as conn:
        for item in items:
            dt = item.get("datetime")
            conn.execute(
                """INSERT OR IGNORE INTO sport_tipster_predictions
                (sport_key, match_id, start_at, home_team_id, away_team_id,
                 home_name, away_name, selection, signal_strength,
                 expected_home, expected_away, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["sport_key"], str(item["match_id"]), dt.isoformat() if dt else None,
                 item.get("home_team_id"), item.get("away_team_id"), item["home"], item["away"],
                 item["selection"], float(item["signal_strength"]), float(item["home_expected"]),
                 float(item["away_expected"]), now_iso),
            )


def _has_due_tipster_predictions():
    """Return whether any recorded pick has passed its scheduled start and still needs a result."""
    _init_tipster_tracking()
    with _db() as conn:
        row = conn.execute(
            """SELECT 1 FROM sport_tipster_predictions
               WHERE outcome IS NULL AND start_at IS NOT NULL AND start_at < ?
               LIMIT 1""",
            (datetime.now(TZ).isoformat(),),
        ).fetchone()
    return row is not None


def _settle_tipster_predictions():
    """Settle picks only when the same completed fixture appears in a team's recent history."""
    _init_tipster_tracking()
    with _db() as conn:
        pending = conn.execute(
            """SELECT sport_key, match_id, home_team_id, selection
               FROM sport_tipster_predictions
               WHERE outcome IS NULL AND start_at IS NOT NULL AND start_at < ?""",
            (datetime.now(TZ).isoformat(),),
        ).fetchall()
    settled = 0
    for sport_key, match_id, home_team_id, selection in pending:
        # A fixture can disappear from the home team's last-five list (or be
        # returned inconsistently by the API). Search both teams before leaving
        # a completed pick pending.
        with _db() as conn:
            ids_row = conn.execute(
                "SELECT home_team_id, away_team_id FROM sport_tipster_predictions WHERE sport_key=? AND match_id=?",
                (sport_key, str(match_id)),
            ).fetchone()
        team_ids = []
        if ids_row:
            for value in ids_row:
                try:
                    team_id = int(value) if value else None
                except (TypeError, ValueError):
                    team_id = None
                if team_id and team_id not in team_ids:
                    team_ids.append(team_id)
        if not team_ids and home_team_id:
            team_ids = [int(home_team_id)]
        found_match = None
        for team_id in team_ids:
            try:
                rows = _sport_api_get(f"{sport_key}/last-five-games", {"teamId": team_id})
            except APIQuotaExceeded:
                raise
            if not isinstance(rows, list):
                continue
            found_match = next(
                (row for row in rows if isinstance(row, dict) and str(_match_id(row)) == str(match_id)),
                None,
            )
            if found_match:
                break
        if not found_match:
            continue
        pair = _score_pair_for_history(found_match, sport_key)
        winner_pair = _winner_pair_for_history(found_match, sport_key, pair)
        if pair is None or winner_pair is None:
            continue
        if winner_pair[0] == winner_pair[1]:
            outcome = "V"
        elif (selection == "Победа дома" and winner_pair[0] > winner_pair[1]) or (selection == "Победа гостите" and winner_pair[1] > winner_pair[0]):
            outcome = "W"
        else:
            outcome = "L"
        with _db() as conn:
            conn.execute(
                """UPDATE sport_tipster_predictions SET outcome=?, actual_home=?, actual_away=?, settled_at=?
                   WHERE sport_key=? AND match_id=? AND outcome IS NULL""",
                (outcome, pair[0], pair[1], datetime.now(TZ).isoformat(), sport_key, str(match_id)),
            )
        settled += 1
        print(f"TIPSTER RESULT SETTLED | sport={sport_key} | match={match_id} | outcome={outcome} | score={pair[0]}-{pair[1]}", flush=True)
    with _db() as conn:
        row = conn.execute(
            """SELECT SUM(CASE WHEN outcome='W' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='L' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='V' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END)
               FROM sport_tipster_predictions"""
        ).fetchone()
    summary = tuple(int(x or 0) for x in row) if row else (0, 0, 0, 0)
    print(f"TIPSTER TRACK RECORD | wins={summary[0]} losses={summary[1]} void={summary[2]} pending={summary[3]} newly_settled={settled}", flush=True)
    return {"wins": summary[0], "losses": summary[1], "void": summary[2], "pending": summary[3], "newly_settled": settled}


def _tipster_summary_counts():
    """Read the stored W/L/V/pending totals without making extra API requests."""
    _init_tipster_tracking()
    with _db() as conn:
        row = conn.execute(
            """SELECT SUM(CASE WHEN outcome='W' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='L' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='V' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END)
               FROM sport_tipster_predictions"""
        ).fetchone()
    summary = tuple(int(x or 0) for x in row) if row else (0, 0, 0, 0)
    return {"wins": summary[0], "losses": summary[1], "void": summary[2], "pending": summary[3]}


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
MIN_SIGNAL_PROBABILITY = 56.0
MIN_SIGNAL_CONFIDENCE = 75.0
MAX_SIGNAL_RISK = 35.0
MIN_VALUE_EDGE = 2.0  # Minimum edge versus offered implied probability, in percentage points.
MIN_EXPECTED_VALUE_PCT = 2.5  # Estimated EV versus no-vig consensus; not a calibrated model edge.
MIN_CONSENSUS_BOOKMAKERS = 3  # Require a broader consensus; two-book estimates are fragile.
MIN_ODDS = 1.40
MAX_ODDS = None  # No upper odds ceiling; retain only the minimum price.
MAX_SIGNALS_PER_DAY = 5
MAX_SIGNALS_PER_SPORT = None  # No quota by sport; value decides the global ranking.


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
    """Compare full-match winner prices across non-Betano books; Betano coverage is diagnostic only."""
    mid = _match_id(match)
    if not mid:
        return []

    rows = _sport_api_get(
        f"{sport_key}/odds",
        {"matchId": mid, "oddsType": "prematch", "limit": 5, "offset": 0},
    )
    if not isinstance(rows, list):
        return []

    home, away = _sport_match_names(match)
    home_norm, away_norm = _normalize_label(home), _normalize_label(away)
    blocked_words = (
        "player", "first scorer", "next", "period", "quarter", "half",
        "set", "inning", "map", "special", "boost", "handicap", "spread",
        "asian handicap", "team total", "first half", "second half",
    )
    raw_quotes = []
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        row_book = row.get("bookmakerName") or row.get("bookmaker") or row.get("bookmaker_name") or ""
        if isinstance(row_book, dict):
            row_book = row_book.get("name") or row_book.get("bookmakerName") or row_book.get("title") or ""
        markets = row.get("odds") or row.get("markets") or []
        if not isinstance(markets, list):
            continue

        for market in markets:
            if not isinstance(market, dict):
                continue
            market_name = str(market.get("market") or market.get("name") or market.get("type") or "").strip()
            norm_market = _normalize_label(market_name)
            if not norm_market or any(word in norm_market for word in blocked_words):
                continue

            # This scanner now publishes only full-match winner picks (home or away).
            if not any(word in norm_market for word in (
                "winner", "moneyline", "money line", "1x2", "full time result",
                "match result", "match winner", "home/away", "home away",
                "победител", "to win",
            )):
                continue

            book_value = market.get("bookmakerName") or market.get("bookmaker") or market.get("bookmaker_name") or row_book
            if isinstance(book_value, dict):
                book_value = book_value.get("name") or book_value.get("bookmakerName") or book_value.get("title") or ""
            bookmaker = str(book_value).strip()
            if not bookmaker:
                continue

            values = market.get("values") or market.get("odds") or market.get("selections") or []
            if not isinstance(values, list):
                continue

            # Classify by actual outcomes, not inconsistent market titles across providers.
            market_has_draw = any(
                isinstance(v, dict)
                and _normalize_label(str(v.get("value") or v.get("label") or v.get("name") or "")) in {"draw", "x", "tie"}
                for v in values
            )

            for selection in values:
                if not isinstance(selection, dict):
                    continue
                label = str(selection.get("value") or selection.get("label") or selection.get("name") or "").strip()
                norm_label = _normalize_label(label)
                if not norm_label or any(word in norm_label for word in blocked_words):
                    continue
                try:
                    odd = float(selection.get("odd") if selection.get("odd") is not None else selection.get("odds"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(odd) or odd < 1.01 or odd > 25.0:
                    continue

                if norm_label in {"home", "1"} or norm_label == home_norm or norm_label.startswith(home_norm + " "):
                    side = "home"
                elif norm_label in {"away", "2"} or norm_label == away_norm or norm_label.startswith(away_norm + " "):
                    side = "away"
                elif norm_label in {"draw", "x", "tie"}:
                    side = "draw"
                else:
                    continue

                winner_type = "three_way" if market_has_draw else "two_way"
                market_key = ("winner", None, winner_type)
                dedup_key = (bookmaker.casefold(), market_key, side, round(odd, 4))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                raw_quotes.append({
                    "bookmaker": bookmaker, "market": market_name, "selection": label,
                    "odd": odd, "family": "winner", "line": None,
                    "market_key": market_key, "side": side,
                })

    if not raw_quotes:
        # Safe diagnostics: log API response structure, not credentials or full payloads.
        sample_rows = []
        for sample_row in rows[:3]:
            if not isinstance(sample_row, dict):
                sample_rows.append({"type": type(sample_row).__name__})
                continue
            sample_markets = sample_row.get("odds") or sample_row.get("markets") or []
            sample_rows.append({
                "row_keys": list(sample_row.keys())[:12],
                "bookmaker": str(sample_row.get("bookmakerName") or sample_row.get("bookmaker") or "")[:60],
                "market_names": [str(m.get("market") or m.get("name") or m.get("type") or "")[:70]
                                 for m in sample_markets[:8] if isinstance(m, dict)],
                "sample_values": [
                    [str(v.get("value") or v.get("label") or v.get("name") or "")[:35]
                     for v in (m.get("values") or m.get("selections") or [])[:4] if isinstance(v, dict)]
                    for m in sample_markets[:3] if isinstance(m, dict)
                ],
            })
        print(
            f"SPORT ODDS PARSE EMPTY | sport={sport_key} | fixture={mid} | rows={len(rows)} "
            f"| home={home!r} | away={away!r} | sample={sample_rows!r}",
            flush=True,
        )

    market_books = {}
    for quote in raw_quotes:
        market_key = quote["market_key"]
        book_key = quote["bookmaker"].strip().casefold()
        market_books.setdefault(market_key, {}).setdefault(book_key, {"name": quote["bookmaker"], "sides": {}})
        sides = market_books[market_key][book_key]["sides"]
        side = quote["side"]
        if side not in sides or quote["odd"] > sides[side]["odd"]:
            sides[side] = quote

    candidates = []
    for market_key, books in market_books.items():
        has_draw_anywhere = any("draw" in book_data["sides"] for book_data in books.values())
        requires_draw = market_key[2] == "three_way" or has_draw_anywhere
        required_sides = {"home", "away", "draw"} if requires_draw else {"home", "away"}

        # Betano coverage varies by sport. Its absence is diagnostic only.
        betano_has_match_market = any(
            "betano" in str(book_data.get("name") or "").casefold()
            and bool(set(book_data.get("sides", {})) & {"home", "away", "draw"})
            for book_data in books.values()
        )
        if not betano_has_match_market:
            print(
                f"SPORT BETANO CHECK | sport={sport_key} | fixture={mid} | "
                f"status=missing_not_blocking | market={market_key[0]}",
                flush=True,
            )

        # Build consensus and candidate prices from other bookmakers only.
        non_betano_books = {
            key: data for key, data in books.items()
            if "betano" not in str(data.get("name") or "").casefold()
        }
        fair_by_side = {}
        for book_data in non_betano_books.values():
            sides = book_data["sides"]
            if not required_sides.issubset(sides):
                continue
            inverse_sum = sum(1.0 / sides[s]["odd"] for s in required_sides)
            if inverse_sum <= 0:
                continue
            for side in required_sides:
                fair_by_side.setdefault(side, []).append(1.0 / sides[side]["odd"] / inverse_sum)

        for side in ("home", "away"):
            fair_values = fair_by_side.get(side, [])
            # Require at least three independent non-Betano full markets before
            # trusting the no-vig consensus for a value estimate.
            if len(fair_values) < MIN_CONSENSUS_BOOKMAKERS:
                continue
            fair_values.sort()
            mid_idx = len(fair_values) // 2
            fair_prob = fair_values[mid_idx] if len(fair_values) % 2 else (fair_values[mid_idx - 1] + fair_values[mid_idx]) / 2.0
            support = len(fair_values)

            # The offered price must come from a bookmaker with the same complete
            # outcome set used to calculate consensus. Never compare a partial market
            # against a full market, especially in sports where overtime/draw rules differ.
            available_quotes = [
                book_data["sides"][side]
                for book_data in non_betano_books.values()
                if required_sides.issubset(book_data["sides"])
                and side in book_data["sides"]
                and book_data["sides"][side]["odd"] >= MIN_ODDS
                and (MAX_ODDS is None or book_data["sides"][side]["odd"] <= MAX_ODDS)
            ]
            if not available_quotes:
                continue

            # Use the best available price from another bookmaker, not Betano.
            best_quote = max(available_quotes, key=lambda quote: quote["odd"])
            offered_implied = 1.0 / best_quote["odd"]
            ev_pct = (fair_prob * best_quote["odd"] - 1.0) * 100.0
            edge_pp = (fair_prob - offered_implied) * 100.0

            all_prices = [
                data["sides"][side]["odd"] for data in non_betano_books.values()
                if required_sides.issubset(data["sides"]) and side in data["sides"]
            ]
            all_prices.sort()
            median_odd = all_prices[len(all_prices) // 2] if all_prices else best_quote["odd"]

            # A very large single-book price outlier is more likely to be a stale,
            # mismatched, or differently defined market than a dependable edge.
            # Reject it rather than advertising an inflated EV estimate.
            if median_odd > 0 and best_quote["odd"] > median_odd * 1.20:
                print(
                    f"SPORT PRICE OUTLIER REJECTED | sport={sport_key} | fixture={mid} "
                    f"| side={side} | best={best_quote['odd']:.2f} | median={median_odd:.2f} "
                    f"| ratio={best_quote['odd'] / median_odd:.2f} | limit=1.20",
                    flush=True,
                )
                continue

            candidate = dict(best_quote)
            candidate.update({
                "bookmaker_count": support,
                "consensus_odd": median_odd,
                "fair_probability": round(fair_prob * 100.0, 2),
                "implied": round(offered_implied * 100.0, 1),
                "expected_value_pct": round(ev_pct, 2),
                "edge_pp": round(edge_pp, 2),
            })
            # Do not select only because a side is likely to win: require a
            # materially better offered price than the multi-book no-vig consensus.
            if candidate["edge_pp"] < MIN_VALUE_EDGE:
                continue
            if candidate["expected_value_pct"] < MIN_EXPECTED_VALUE_PCT:
                continue
            candidates.append(candidate)

    candidates.sort(key=lambda item: (-item.get("expected_value_pct", 0.0), -item.get("bookmaker_count", 0), -item.get("fair_probability", 0.0)))
    if not candidates:
        full_books = 0
        for market_key, books in market_books.items():
            required = {"home", "away", "draw"} if market_key[2] == "three_way" or any(
                "draw" in data.get("sides", {}) for data in books.values()
            ) else {"home", "away"}
            full_books += sum(
                1 for name, data in books.items()
                if "betano" not in str(data.get("name") or "").casefold()
                and required.issubset(data.get("sides", {}))
            )
        print(
            f"SPORT QUALITY NO PICK | sport={sport_key} | fixture={mid} | raw_quotes={len(raw_quotes)} "
            f"| grouped_winner_markets={len(market_books)} | non_betano_full_book_sets={full_books} "
            f"| reason=need 2+ non-Betano full winner markets + minimum odds {MIN_ODDS:.2f}; "
            f"no maximum odds; home/away winners only",
            flush=True,
        )
    return candidates

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
        f"   🎯 Избор: {_selection_label(item)}",
        f"   🏪 Букмейкър: {item.get('bookmaker') or 'Unknown bookmaker'}",
        f"   💰 Коефициент: {item['odd']:.2f}",
        f"   📌 Имплицитна вероятност на коефициента: {item['implied']:.1f}%",
        f"   🎯 Оценена пазарна вероятност (без маржа): {item.get('fair_probability', 0.0):.1f}%",
        f"   📈 Очаквана стойност (EV): {item.get('expected_value_pct', 0.0):+.2f}% | Edge: {item.get('edge_pp', 0.0):+.2f} п.п.",
        f"   🧮 Букмейкъри с пълен пазар за консенсус: {item.get('bookmaker_count', 0)}",
        f"   📊 Медианен коефициент за избора: {item.get('consensus_odd', item['odd']):.2f}",
        f"   🏆 Лига: {item['league'] or '-'} | Държава: {item['country'] or '-'}",
        f"   📅 {dt.strftime('%d.%m.%Y')} | {dt.strftime('%H:%M')} BG",
    ])



# PREMATCH market-value mode: compare the best price with a multi-book no-vig consensus.
# This estimates price value; it is not a separately trained or calibrated outcome model.
SPORT_STATS_ONLY = False
_SPORT_LAST_FIVE_CACHE = {}


def _score_pair_from_text(value):
    if value is None:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*[-–:]\s*(-?\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None


def _score_pair_for_history(match, sport_key):
    state = match.get("state") or {}
    score = state.get("score") or match.get("score") or {}
    if not isinstance(score, dict):
        return _score_pair_from_text(score)

    # Baseball stores run totals as innings nested under home/away.
    if sport_key == "baseball":
        home_box, away_box = score.get("home") or {}, score.get("away") or {}
        if isinstance(home_box, dict) and isinstance(away_box, dict):
            home_innings = home_box.get("innings") or []
            away_innings = away_box.get("innings") or []
            if home_innings and away_innings:
                try:
                    return sum(float(x or 0) for x in home_innings), sum(float(x or 0) for x in away_innings)
                except (TypeError, ValueError):
                    pass

    # Volleyball's total points must be derived from set scores, not the match's set count.
    if sport_key == "volleyball":
        set_pairs = []
        for key in ("firstSet", "secondSet", "thirdSet", "fourthSet", "fifthSet"):
            pair = _score_pair_from_text(score.get(key))
            if pair:
                set_pairs.append(pair)
        if set_pairs:
            return sum(p[0] for p in set_pairs), sum(p[1] for p in set_pairs)

    current = _score_pair_from_text(score.get("current"))
    if current:
        return current

    # Some APIs expose only period / quarter score fields.
    pairs = []
    for key, value in score.items():
        if any(token in str(key).lower() for token in ("period", "quarter", "inning", "set")):
            pair = _score_pair_from_text(value)
            if pair:
                pairs.append(pair)
    if pairs:
        return sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    return None


def _winner_pair_for_history(match, sport_key, score_pair=None):
    # Volleyball winners are decided by sets, not total points.
    if sport_key == "volleyball":
        state = match.get("state") or {}
        score = state.get("score") or match.get("score") or {}
        if not isinstance(score, dict):
            return None
        home_sets = away_sets = set_count = 0
        for key in ("firstSet", "secondSet", "thirdSet", "fourthSet", "fifthSet"):
            pair = _score_pair_from_text(score.get(key))
            if pair:
                set_count += 1
                if pair[0] > pair[1]:
                    home_sets += 1
                elif pair[1] > pair[0]:
                    away_sets += 1
        return (float(home_sets), float(away_sets)) if set_count else None
    return score_pair


def _team_recent_form(sport_key, team_id):
    cache_key = (sport_key, int(team_id))
    if cache_key in _SPORT_LAST_FIVE_CACHE:
        return _SPORT_LAST_FIVE_CACHE[cache_key]
    rows = _sport_api_get(f"{sport_key}/last-five-games", {"teamId": int(team_id)})
    if not isinstance(rows, list):
        rows = []
    wins = losses = draws = 0
    scored = conceded = 0.0
    games = 0
    for match in rows:
        if not isinstance(match, dict):
            continue
        pair = _score_pair_for_history(match, sport_key)
        winner_pair = _winner_pair_for_history(match, sport_key, pair)
        if pair is None or winner_pair is None:
            continue
        home = match.get("homeTeam") or match.get("home") or {}
        away = match.get("awayTeam") or match.get("away") or {}
        home_id = _sport_team_id(home)
        away_id = _sport_team_id(away)
        if int(team_id) == home_id:
            for_score, against = pair
            winner_for, winner_against = winner_pair
        elif int(team_id) == away_id:
            against, for_score = pair
            winner_against, winner_for = winner_pair
        else:
            continue
        scored += for_score
        conceded += against
        games += 1
        if winner_for > winner_against:
            wins += 1
        elif winner_for < winner_against:
            losses += 1
        else:
            draws += 1
    result = {
        "games": games, "wins": wins, "losses": losses, "draws": draws,
        "avg_scored": scored / games if games else None,
        "avg_conceded": conceded / games if games else None,
        "win_rate": (wins + 0.5 * draws) / games if games else None,
    }
    _SPORT_LAST_FIVE_CACHE[cache_key] = result
    return result


def _selection_label(preview):
    """Display the actual team name followed by WIN; never rely on home/away wording."""
    selection = preview.get("tracking_selection") or preview.get("selection")
    if selection == "Победа дома" or preview.get("side") == "home":
        return f"{preview.get('home', 'Home')} WIN"
    if selection == "Победа гостите" or preview.get("side") == "away":
        return f"{preview.get('away', 'Away')} WIN"
    return str(preview.get("selection") or "неопределен избор")


def _statistical_preview(sport_key, match):
    """Estimate a conservative winner probability from recent results; not a calibrated model."""
    home_obj = match.get("homeTeam") or match.get("home") or {}
    away_obj = match.get("awayTeam") or match.get("away") or {}
    home_id, away_id = _sport_team_id(home_obj), _sport_team_id(away_obj)
    if not home_id or not away_id:
        return None, "липсват ID на отбор"

    home_form = _team_recent_form(sport_key, home_id)
    away_form = _team_recent_form(sport_key, away_id)
    if home_form["games"] < 4 or away_form["games"] < 4:
        return None, f"недостатъчна история (нужни са поне 4 мача; домакин {home_form['games']}/5, гост {away_form['games']}/5)"

    # Matchup scoring estimate. Volleyball points are kept for context only;
    # match winners are evaluated by sets in _team_recent_form.
    home_expected = (home_form["avg_scored"] + away_form["avg_conceded"]) / 2.0
    away_expected = (away_form["avg_scored"] + home_form["avg_conceded"]) / 2.0
    total_expected = home_expected + away_expected

    # Beta-style smoothing avoids treating 5-0 and 0-5 short streaks as certainties.
    home_rate = (home_form["wins"] + 0.5 * home_form["draws"] + 1.5) / (home_form["games"] + 3.0)
    away_rate = (away_form["wins"] + 0.5 * away_form["draws"] + 1.5) / (away_form["games"] + 3.0)
    form_probability = max(0.05, min(0.95, 0.5 + 0.45 * (home_rate - away_rate)))

    # Turn the matchup score differential into a probability-like component.
    # Shrink toward 50% because the recent sample is small and league strength varies.
    scoring_gap = home_expected - away_expected
    scale = max(1.0, total_expected * 0.18)
    score_probability = 1.0 / (1.0 + math.exp(-max(-6.0, min(6.0, scoring_gap / scale))))
    blended_home_probability = 0.60 * form_probability + 0.40 * score_probability
    sample_games = min(home_form["games"], away_form["games"])
    reliability = sample_games / (sample_games + 5.0)
    home_probability = 0.5 + reliability * (blended_home_probability - 0.5)
    home_probability = max(0.50, min(0.85, home_probability))
    away_probability = 1.0 - home_probability

    if home_probability >= away_probability:
        selection = "Победа дома"
        model_probability = home_probability * 100.0
    else:
        selection = "Победа гостите"
        model_probability = away_probability * 100.0

    # Confidence/risk describe data support, not a proven hit rate.
    model_confidence = min(85.0, 55.0 + sample_games * 4.0 + min(10.0, abs(home_rate - away_rate) * 10.0))
    model_risk = 100.0 - model_confidence
    if model_probability < MIN_SIGNAL_PROBABILITY:
        return None, f"вероятността е под прага ({model_probability:.1f}% < {MIN_SIGNAL_PROBABILITY:.1f}%)"

    preview = {
        "sport_key": sport_key,
        "home": _sport_team_name(home_obj),
        "away": _sport_team_name(away_obj),
        "home_expected": home_expected,
        "away_expected": away_expected,
        "total_expected": total_expected,
        "selection": selection,
        "signal_strength": model_probability,
        "quality_score": model_probability,
        "model_probability": model_probability,
        "model_confidence": model_confidence,
        "model_risk": model_risk,
        "home_probability": home_probability * 100.0,
        "away_probability": away_probability * 100.0,
        "home_form": home_form,
        "away_form": away_form,
        "datetime": _sport_match_datetime(match),
        "league": _sport_league_country(match)[0],
        "country": _sport_league_country(match)[1],
        "match_id": _match_id(match),
        "home_team_id": home_id,
        "away_team_id": away_id,
    }
    return preview, None


def _run_sport_stats_only(start, end, now_bg, run_suffix, send_func, started):
    _SPORT_LAST_FIVE_CACHE.clear()
    tracking = _settle_tipster_predictions()
    sections = []
    previews = []
    counts = {}
    for sport_key, cfg in SPORTS_CONFIG.items():
        try:
            fixtures = _get_sport_fixtures(cfg, start, end)
            future = []
            for match in fixtures:
                dt = _sport_match_datetime(match)
                if dt is None or dt <= datetime.now(TZ):
                    continue
                league_name, country_name = _sport_league_country(match)
                country_cf = str(country_name or "").strip().casefold()
                league_cf = str(league_name or "").strip().casefold()
                if country_cf in {"russia", "belarus", "русия", "беларус", "philippines", "the philippines", "филипини", "singapore", "сингапур"}:
                    continue
                if "friendly" in league_cf or "приятел" in league_cf:
                    continue
                future.append(match)
            future.sort(key=lambda m: _sport_match_datetime(m) or end)
            future = future[:MAX_FIXTURES_TO_EVALUATE]
            sport_previews, reasons = [], {}
            history_sufficient = 0
            for match in future:
                try:
                    preview, reason = _statistical_preview(sport_key, match)
                    if preview:
                        sport_previews.append(preview)
                        history_sufficient += 1
                    elif reason:
                        reasons[reason] = reasons.get(reason, 0) + 1
                        # A weak-edge rejection means both teams had enough
                        # valid historical games; it is not a history failure.
                        if "слабо статистическо предимство" in reason or "вероятността е под прага" in reason:
                            history_sufficient += 1
                except APIQuotaExceeded:
                    raise
                except Exception as exc:
                    print(f"SPORT STATS ERROR | sport={sport_key} match={_match_id(match)} | {exc!r}", flush=True)
            # Ranking includes only candidates clearing the edge threshold; separately
            # report fixtures where both teams had enough valid history.
            sport_previews.sort(key=lambda p: (-p["quality_score"], p["datetime"] or end))
            counts[sport_key] = {
                "fixtures": len(fixtures),
                "future": len(future),
                "history": history_sufficient,
                "qualified": len(sport_previews),
            }
            sections.append((sport_key, cfg, sport_previews, reasons))
            previews.extend(sport_previews)
            print(f"SPORT STATS SUMMARY | sport={sport_key} fixtures={len(fixtures)} future={len(future)} usable={len(sport_previews)}", flush=True)
        except APIQuotaExceeded:
            print(f"SPORT STATS QUOTA STOP | sport={sport_key}", flush=True)
            counts[sport_key] = {"fixtures": 0, "future": 0, "history": 0, "qualified": 0}
            sections.append((sport_key, cfg, [], {"достигнат API лимит": 1}))
            # Still render every sport section; do not hide sports after a quota stop.
            for remaining_key, remaining_cfg in list(SPORTS_CONFIG.items())[list(SPORTS_CONFIG).index(sport_key) + 1:]:
                sections.append((remaining_key, remaining_cfg, [], {"не е проверен заради API лимит": 1}))
                counts[remaining_key] = {"fixtures": 0, "future": 0, "history": 0, "qualified": 0}
            break
        except Exception as exc:
            print(f"SPORT STATS SECTION ERROR | sport={sport_key} | {exc!r}", flush=True)
            counts[sport_key] = {"fixtures": 0, "future": 0, "history": 0, "qualified": 0}
            sections.append((sport_key, cfg, [], {"грешка при зареждане": 1}))

    # Global leaderboard by estimated win probability, while limiting concentration
    # in one sport. This allows strong basketball/volleyball/handball candidates
    # to appear instead of letting hockey occupy nearly every daily slot.
    previews.sort(key=lambda p: (-p["model_probability"], p["model_risk"], p["datetime"] or end))
    top_signals = []
    selected_keys = set()
    selected_per_sport = {}
    for preview in previews:
        sport_key = preview["sport_key"]
        if selected_per_sport.get(sport_key, 0) >= MAX_SIGNALS_PER_SPORT:
            continue
        fixture_key = (sport_key, str(preview["match_id"]))
        if fixture_key in selected_keys:
            continue
        top_signals.append(preview)
        selected_keys.add(fixture_key)
        selected_per_sport[sport_key] = selected_per_sport.get(sport_key, 0) + 1
        if len(top_signals) >= MAX_SIGNALS_PER_DAY:
            break
    # Persist picks before composing the report so the tracking line includes them.
    if top_signals:
        _record_tipster_predictions(top_signals)
        tracking = _tipster_summary_counts()
    lines = [
        f"📊 SPORT PREMATCH — ТИПСТЪРСКИ ПОДБОР ({run_suffix or '10:00'} BG)",
        now_bg.strftime("%d.%m.%Y %H:%M BG"), "",
        "🏁 РЕЖИМ: PREMATCH — само срещи, които още не са започнали",
        f"Период: {start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "МЕТОД: мултиспорт рейтинг по последни до 5 резултата, изгладена успеваемост и очаквана разлика в резултата; минимум 4 валидни мача на всеки отбор.",
        f"Глобален подбор по най-висока изчислена вероятност за победа: праг минимум {MIN_SIGNAL_PROBABILITY:.1f}%; до {MAX_SIGNALS_PER_DAY} срещи общо и максимум {MAX_SIGNALS_PER_SPORT} от един спорт. Публикуват се само преминалите прага — без допълване насила.",
        f"Минимална оценена вероятност за публикуване: {MIN_SIGNAL_PROBABILITY:.1f}%. Вероятността е предварителна оценка от малка извадка, НЕ е калибрирана историческа успеваемост. Коефициенти и EV се показват само когато има надежден коефициент за точния пазар; при липса на такъв не се измисля цена или положителна стойност.",
        f"РЕЗУЛТАТИ ДО МОМЕНТА: {tracking['wins']} печалби / {tracking['losses']} загуби / {tracking['void']} анулирани / {tracking['pending']} неприключили.",
        "Русия, Беларус, Филипини, Сингапур и приятелски мачове — блокирани. LIVE е изключен.", ""
    ]
    for sport_key, cfg, sport_previews, reasons in sections:
        published_for_sport = [p for p in sport_previews if (sport_key, str(p["match_id"])) in selected_keys]
        lines.extend([f"{cfg['name']} — {'ПРОГНОЗА' if published_for_sport else 'НЯМА ПУБЛИКУВАН ИЗБОР'}", ""])
        if published_for_sport:
            for pick_index, p in enumerate(published_for_sport, 1):
                dt = p["datetime"]
                lines.extend([
                    f"Среща {pick_index}: {p['home']} - {p['away']}",
                    f"Форма (последни до 5): домакин {p['home_form']['wins']}-{p['home_form']['draws']}-{p['home_form']['losses']} ({p['home_form']['games']} мача), гост {p['away_form']['wins']}-{p['away_form']['draws']}-{p['away_form']['losses']} ({p['away_form']['games']} мача)",
                    f"🎯 PICK: {_selection_label(p)}",
                    f"📈 Model probability: {p['model_probability']:.1f}% | оценка, не валидиран процент успеваемост",
                    f"🤖 Подкрепа от данните: {p['model_confidence']:.1f}/100 | 🛡 Индикатор риск: {p['model_risk']:.1f}/100 (евристики, не реален процент успеваемост)",
                    f"📊 Очакван резултат по последна форма: {p['home_expected']:.1f} - {p['away_expected']:.1f} | общо: {p['total_expected']:.1f}",
                    "💰 Коефициент / EV: не е потвърден от източника за този пазар; не се изчислява без реална цена.",
                    f"Лига: {p['league'] or '-'} | Държава: {p['country'] or '-'}",
                    f"Начало: {dt.strftime('%d.%m.%Y %H:%M')} BG" if dt else "Начало: неизвестно",
                ])
                if pick_index < len(published_for_sport):
                    lines.append("")
            lines.append(f"Срещи в прозореца: {counts.get(sport_key, {}).get('fixtures', 0)} | бъдещи проверени: {counts.get(sport_key, {}).get('future', 0)} | с достатъчна история: {counts.get(sport_key, {}).get('history', 0)} | преминали прага за избор: {counts.get(sport_key, {}).get('qualified', 0)}")
        else:
            lines.append(f"Срещи в прозореца: {counts.get(sport_key, {}).get('fixtures', 0)} | бъдещи проверени: {counts.get(sport_key, {}).get('future', 0)} | с достатъчна история: {counts.get(sport_key, {}).get('history', 0)} | преминали прага за избор: {counts.get(sport_key, {}).get('qualified', 0)}")
            if sport_previews:
                lines.append(f"Има кандидати, но не са сред глобалните {MAX_SIGNALS_PER_DAY} избора по вероятност.")
            elif reasons:
                lines.append("Причина: " + ", ".join(f"{k} ({v})" for k, v in sorted(reasons.items())))
            else:
                lines.append("Няма бъдещи срещи след филтрите или API не върна достатъчно данни.")
        lines.extend(["", "────────────────────", ""])

    lines.extend([f"🎯 ТОП ДО {MAX_SIGNALS_PER_DAY} ТИПСТЪРСКИ ИЗБОРА", ""])
    for idx, p in enumerate(top_signals, 1):
        lines.append(f"{idx}. {p['sport_key'].upper()} — {p['home']} - {p['away']} | {_selection_label(p)} | Model probability {p['model_probability']:.1f}% | risk {p['model_risk']:.1f}/100")
    if not top_signals:
        lines.append("Няма срещи, преминали минималните условия за публикуване.")
    lines.extend(["", f"📡 API заявки: {_SPORT_API_CALLS}", f"⏱ Scan time: {time.time() - started:.1f}s"])
    message = "\n".join(lines)
    print(message, flush=True)
    if send_func and run_suffix:
        try:
            _send_sport_report_chunks(message, send_func, max_chars=3700)
            print("SPORT STATS TELEGRAM REPORT SENT | picks_saved_before_publish", flush=True)
        except Exception as exc:
            print(f"SPORT STATS TELEGRAM ERROR: {exc!r}", flush=True)
    elif send_func and top_signals:
        _send_sport_report_chunks(message, send_func, max_chars=3700)
    return message


def run_sport_top3_daily_scanner(send_func=None, run_suffix=None):
    """SPORT PREMATCH only: odds-free statistical picks based on recent completed results."""
    global _SPORT_API_CALLS, _SPORT_STATS_CACHE
    _SPORT_API_CALLS = 0
    _SPORT_STATS_CACHE = {}
    started = time.time()

    now_bg = datetime.now(TZ)
    day_key = now_bg.strftime("%Y-%m-%d")
    run_key = f"sport_prematch:{day_key}" if not run_suffix else f"sport_prematch:{day_key}:{run_suffix}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT SCANNER ALREADY RAN TODAY: {day_key} BG"), flush=True)
        return ""

    # Consume today's run before making any API calls. Even if Telegram fails
    # or the process restarts mid-scan, the scheduler will not spend requests again today.
    mark_ran(run_key)
    print(f"SPORT DAILY RUN LOCKED | day={day_key} | scheduled run=10:00 Europe/Sofia", flush=True)

    # Fixed 24-hour fixture window in Bulgaria time: today at 12:00 -> tomorrow at 12:00.
    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=24)
    if SPORT_STATS_ONLY:
        return _run_sport_stats_only(start, end, now_bg, run_suffix, send_func, started)
    now_scan = datetime.now(TZ)

    all_signals = []
    sport_scan_stats = {}
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
            raw_future_count = len(future)
            # Wider fixture pool like the football scanner, with a hard cap
            # to keep daily API usage controlled and leave room for every sport.
            future = future[:MAX_FIXTURES_TO_EVALUATE]
            sport_scan_stats[sport_key] = {"fetched": len(fixtures), "future_candidates": raw_future_count, "odds_checks": len(future), "market_candidates": 0}
            print(
                f"SPORT FIXTURE SUMMARY | sport={sport_key} fetched={len(fixtures)} "
                f"future_candidates={raw_future_count} odds_checks={len(future)}",
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
                    sport_scan_stats[sport_key]["market_candidates"] += 1
                    best = options[0]
                    home, away = _sport_match_names(match, sport_key)
                    league, country = _sport_league_country(match)
                    dt = _sport_match_datetime(match)
                    home_obj = match.get("homeTeam") or match.get("home") or {}
                    away_obj = match.get("awayTeam") or match.get("away") or {}
                    all_signals.append({
                        "sport_key": sport_key,
                        "sport_name": cfg["name"],
                        "match_id": mid,
                        "home": home,
                        "away": away,
                        "home_team_id": _sport_team_id(home_obj),
                        "away_team_id": _sport_team_id(away_obj),
                        "tracking_selection": "Победа дома" if best.get("side") == "home" else "Победа гостите",
                        "signal_strength": best.get("fair_probability", 0.0),
                        "home_expected": 0.0,
                        "away_expected": 0.0,
                        "bookmaker": best.get("bookmaker") or "Unknown bookmaker",
                        "market": best["market"],
                        "selection": best["selection"],
                        "odd": best["odd"],
                        "implied": best.get("implied", round(100.0 / best["odd"], 1)),
                        "fair_probability": best.get("fair_probability", 0.0),
                        "expected_value_pct": best.get("expected_value_pct", 0.0),
                        "edge_pp": best.get("edge_pp", 0.0),
                        "bookmaker_count": best.get("bookmaker_count", 0),
                        "consensus_odd": best.get("consensus_odd", best["odd"]),
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

    # Rank all eligible picks together and publish no more than five per day.
    # There is no quota per sport; a sport appears only when a candidate clears value filters.
    already_sent = _sent_sport_fixture_keys()
    sent_today = _sent_sport_count_today()
    daily_remaining = max(0, MAX_SIGNALS_PER_DAY - sent_today)
    eligible_by_sport = {}
    eligible = []
    for sport_key in SPORTS_CONFIG:
        sport_candidates = [
            item for item in all_signals
            if item["sport_key"] == sport_key
            and (str(item["sport_key"]), str(item["match_id"])) not in already_sent
        ]
        sport_candidates.sort(key=lambda x: (
            -x.get("expected_value_pct", 0.0),
            -x.get("bookmaker_count", 0),
            -x.get("fair_probability", 0.0),
            str(x["match_id"]),
        ))
        eligible_by_sport[sport_key] = len(sport_candidates)
        eligible.extend(sport_candidates)

    # Value first, then independent bookmaker support and edge. Do not fill slots with weak picks.
    eligible.sort(key=lambda x: (
        -x.get("expected_value_pct", 0.0),
        -x.get("bookmaker_count", 0),
        -x.get("edge_pp", 0.0),
        -x.get("fair_probability", 0.0),
        str(x["match_id"]),
    ))
    final = []
    selected_per_sport = {}
    selected_fixtures = set()
    for item in eligible:
        if len(final) >= min(MAX_SIGNALS_PER_DAY, daily_remaining):
            break
        fixture_key = (str(item["sport_key"]), str(item["match_id"]))
        if fixture_key in selected_fixtures:
            continue
        final.append(item)
        selected_fixtures.add(fixture_key)
        sport_key = item["sport_key"]
        selected_per_sport[sport_key] = selected_per_sport.get(sport_key, 0) + 1

    print(
        "SPORT TIPSTER QUALITY GLOBAL TOP PICKS | "
        f"eligible={sum(eligible_by_sport.values())} | sent_today_before_scan={sent_today} | "
        f"remaining_daily_slots={daily_remaining} | selected={len(final)} | "
        f"daily_cap={MAX_SIGNALS_PER_DAY} | no per-sport quota | by_sport={selected_per_sport} | "
        f"min_books={MIN_CONSENSUS_BOOKMAKERS} | odds_min={MIN_ODDS:.2f} | odds_max=none | "
        f"min_edge_pp={MIN_VALUE_EDGE:.1f} | min_ev_pct={MIN_EXPECTED_VALUE_PCT:.1f} | "
        f"ranking=EV_then_support_then_edge",
        flush=True,
    )

    lines = [
        f"📊 SPORT PREMATCH — ОТЧЕТ ({run_suffix or '10:00'} BG)",
        now_bg.strftime("%d.%m.%Y %H:%M BG"),
        "",
        "🏁 РЕЖИМ: PREMATCH — само срещи, които още не са започнали",
        f"Период: {start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        f"Подбор: максимум {MAX_SIGNALS_PER_DAY} сигнала общо за всички спортове; няма квота по спорт и няма допълване насила",
        f"Филтри: минимум {MIN_CONSENSUS_BOOKMAKERS} независими небетано букмейкъра с пълен пазар; коефициент минимум {MIN_ODDS:.2f}; edge минимум {MIN_VALUE_EDGE:.1f} п.п.; оценена EV минимум {MIN_EXPECTED_VALUE_PCT:.1f}%; без горна граница на коефициента",
        "Пазари: само победа на домакина или госта за целия мач; равенства, тотали, непълни пазари, хендикапи, спредове и отделни периоди се изключват",
        "Коефициентът за избора се взема от друг букмейкър. Betano е само допълнителна проверка; липсата му не блокира сигнал.",
        "Метод: no-vig консенсус от няколко букмейкъра. Edge и EV са оценки спрямо този консенсус, не гарантирана доходност и не доказана успеваемост на независим модел.",
        f"Коефициент минимум {MIN_ODDS:.2f} | EV минимум {MIN_EXPECTED_VALUE_PCT:.1f}% | edge минимум {MIN_VALUE_EDGE:.1f} п.п. | максимум {MAX_SIGNALS_PER_DAY} сигнала дневно",
        "Русия, Беларус, Филипини, Сингапур и приятелски мачове — блокирани. LIVE е изключен.",
        "",
    ]

    if final:
        lines.extend(["🎯 PREMATCH — ГЛОБАЛЕН ТОП ПО СТОЙНОСТ", ""])
        first_section = True
        for sport_key, cfg in SPORTS_CONFIG.items():
            sport_items = [item for item in final if item["sport_key"] == sport_key]
            if not first_section:
                lines.extend(["", "────────────────────", ""])
            first_section = False
            lines.append(f"{cfg['name']} — ТОП {len(sport_items)}")
            lines.append("")
            if not sport_items:
                stats = sport_scan_stats.get(sport_key, {})
                candidate_count = sum(1 for item in all_signals if item["sport_key"] == sport_key)
                lines.append(
                    f"Срещи в прозореца: {stats.get('fetched', 0)} | бъдещи след филтрите: {stats.get('future_candidates', 0)} | проверени за коефициенти: {stats.get('odds_checks', 0)}."
                )
                lines.append(
                    f"Срещи с поне един подходящ пазар: {stats.get('market_candidates', 0)} | сигнали преди общия дневен подбор: {candidate_count}."
                )
                lines.append("Няма сигнал за този спорт, който да е преминал всички условия.")
                continue
            for i, item in enumerate(sport_items, 1):
                lines.append(_format_sport_simple_signal(i, item))
                if i < len(sport_items):
                    lines.extend(["", "────────────────────", ""])
    else:
        lines.append("🎯 Няма нов подходящ PREMATCH сигнал за днешния отчет.")

    lines.extend([
        "",
        f"📡 API заявки: {_SPORT_API_CALLS}",
        f"⏱ Scan time: {time.time() - started:.1f}s",
    ])
    message = "\n".join(lines)
    print(message, flush=True)

    if send_func and (final or run_suffix):
        try:
            _send_sport_report_chunks(message, send_func, max_chars=3700)
            if final:
                tracked_items = []
                for item in final:
                    tracked = dict(item)
                    tracked["selection"] = item.get("tracking_selection", "Победа дома")
                    tracked["home_expected"] = 0.0
                    tracked["away_expected"] = 0.0
                    tracked["signal_strength"] = float(item.get("fair_probability", 0.0))
                    tracked_items.append(tracked)
                _record_tipster_predictions(tracked_items)
                _mark_sent_sport_fixtures(final)
            print("SPORT TELEGRAM REPORT SENT", flush=True)
        except Exception as exc:
            print(f"SPORT TELEGRAM ERROR: {exc!r}", flush=True)
            return message
    elif not final:
        print("SPORT DAILY RESULT | no new qualifying odds; no signal sent", flush=True)

    print(f"SPORT DAILY COMPLETE | day={day_key} | signals={len(final)} | api_calls={_SPORT_API_CALLS}", flush=True)
    return message

def _run_sport(day_key, run_suffix=None):
    global SPORT_RUNNING, SPORT_DONE_HOUR
    try:
        print(f"SPORT DAILY THREAD START | day={day_key}", flush=True)
        report = run_sport_top3_daily_scanner(send_telegram, run_suffix=run_suffix)
        print(f"SPORT DAILY THREAD RESULT | day={day_key} | report={bool(report)}", flush=True)
    except APIQuotaExceeded as exc:
        print(f"SPORT DAILY QUOTA STOP | day={day_key} | {exc}", flush=True)
    except Exception as exc:
        print(f"SPORT DAILY ERROR | day={day_key} | {exc!r}", flush=True)
        logging.exception("SPORT DAILY ERROR")
    finally:
        SPORT_DONE_HOUR = day_key
        SPORT_RUNNING = False
        print(f"SPORT DAILY THREAD END | day={day_key}", flush=True)

def _run_sport_result_check(hour_key):
    """Retry settling finished picks hourly without running odds or a full fixture scan."""
    global SPORT_RUNNING, SPORT_RESULT_CHECK_DONE_HOUR
    try:
        _SPORT_API_CALLS_BEFORE_RESULT_CHECK = _SPORT_API_CALLS
        print(f"SPORT RESULT CHECK START | hour={hour_key}", flush=True)
        summary = _settle_tipster_predictions()
        print(
            f"SPORT RESULT CHECK COMPLETE | hour={hour_key} | "
            f"newly_settled={summary.get('newly_settled', 0)} | "
            f"api_calls_used={_SPORT_API_CALLS - _SPORT_API_CALLS_BEFORE_RESULT_CHECK}",
            flush=True,
        )
    except APIQuotaExceeded as exc:
        print(f"SPORT RESULT CHECK QUOTA STOP | hour={hour_key} | {exc}", flush=True)
    except Exception as exc:
        print(f"SPORT RESULT CHECK ERROR | hour={hour_key} | {exc!r}", flush=True)
        logging.exception("SPORT RESULT CHECK ERROR")
    finally:
        SPORT_RUNNING = False


def main_loop():
    global SPORT_RUNNING, SPORT_DONE_HOUR, SPORT_RESULT_CHECK_DONE_HOUR
    print("SPORT-ONLY SYSTEM START | Highlightly Sport | PREMATCH ONLY | LIVE OFF", flush=True)
    print("SPORT SCHEDULER READY | multi-book market-value PREMATCH | run 10:00 BG | fixture window 12:00 BG -> next day 12:00 BG | max 5 picks, no sport quota | min 3 books, edge>=2pp, EV>=2.5% | hard stop 24,500 requests/day", flush=True)
    while True:
        now = datetime.now(TZ)
        day_key = now.strftime("%Y-%m-%d")
        daily_key = f"sport_prematch:{day_key}:10:00"

        # Run at 10:00 BG. If Railway starts/restarts after 10:00, catch up
        # immediately once for today instead of silently missing the daily report.
        past_scheduled_time = (now.hour, now.minute) >= (10, 0)
        if past_scheduled_time and not SPORT_RUNNING and SPORT_DONE_HOUR != day_key:
            if already_ran(daily_key):
                SPORT_DONE_HOUR = day_key
                print(f"SPORT 10:00 SCHEDULER SKIP | day={day_key} | already ran", flush=True)
            elif _quota_locked("highlightly_sport"):
                SPORT_DONE_HOUR = day_key
                print(f"SPORT 10:00 SCHEDULER SKIP | day={day_key} | daily quota lock", flush=True)
                mark_ran(daily_key)
            else:
                SPORT_RUNNING = True
                trigger_kind = "on-time" if (now.hour, now.minute) == (10, 0) else "catch-up-after-10:00"
                print(f"SPORT 10:00 SCHEDULER TRIGGER | mode={trigger_kind} | day={day_key} | time={now:%Y-%m-%d %H:%M:%S} BG", flush=True)
                threading.Thread(target=_run_sport, args=(day_key, "10:00"), daemon=True).start()

        # Retry result settlement once per hour, but only if at least one saved
        # prediction has passed kickoff and is still pending. Never overlap a
        # settlement pass with the main daily scan.
        result_hour_key = now.strftime("%Y-%m-%d-%H")
        if (
            not SPORT_RUNNING
            and SPORT_RESULT_CHECK_DONE_HOUR != result_hour_key
            and _has_due_tipster_predictions()
        ):
            SPORT_RESULT_CHECK_DONE_HOUR = result_hour_key
            SPORT_RUNNING = True
            threading.Thread(
                target=_run_sport_result_check,
                args=(result_hour_key,),
                daemon=True,
            ).start()

        time.sleep(20)


if __name__ == "__main__":
    main_loop()


