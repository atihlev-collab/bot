# SPORT-ONLY MAIN — 17:15 BG
# Football is NOT started here.
# Sport scanner runs once daily, strictly 17:15-17:20 BG.
# Fixture window remains controlled inside the sport scanner:
# 12:00 BG today -> 12:00 BG next day.

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from config import BOT_TOKEN, CHAT_ID

TZ = ZoneInfo("Europe/Sofia")

# STRICT DAILY LAUNCH: 17:15 -> 17:20 BG ONLY.
SPORT_START_MINUTES = 17 * 60 + 15
SPORT_LAUNCH_END_MINUTES = 17 * 60 + 20

SPORT_DONE_DAY = None
SPORT_RUNNING = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    print(f"SPORT TELEGRAM SENT | status={response.status_code}", flush=True)



# ============================================================
# EMBEDDED SPORT ENGINE — previously sport_top3.py
# ============================================================

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
DB_FILE = "v3_ai.db"
SPORT_API_BASE = "https://sports.highlightly.net"
SPORT_API_HOST = "sport-highlights-api.p.rapidapi.com"
SPORT_API_TZ = "Europe/Sofia"
SPORT_API_LIMIT = 100
SPORT_HISTORY_FROM = None  # SPORT PREMATCH is market-only; no historical team statistics.

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
        "teams": "basketball/teams",
        "metric": "points"
    },
    "hockey": {
        "name": "🏒 ХОКЕЙ",
        "endpoint": "hockey/matches",
        "teams": "hockey/teams",
        "metric": "goals"
    },
    "american-football": {
        "name": "🏈 NFL / NCAA — Division I / Division II",
        "endpoint": "american-football/matches",
        "teams": "american-football/teams",
        "metric": "points"
    },
    "baseball": {
        "name": "⚾ БЕЙЗБОЛ",
        "endpoint": "baseball/matches",
        "teams": "baseball/teams",
        "metric": "runs"
    },
    "rugby": {
        "name": "🏉 РЪГБИ",
        "endpoint": "rugby/matches",
        "teams": "rugby/teams",
        "metric": "points"
    },
    "volleyball": {
        "name": "🏐 ВОЛЕЙБОЛ",
        "endpoint": "volleyball/matches",
        "teams": "volleyball/teams",
        "metric": "points"
    },
    "handball": {
        "name": "🤾 ХАНДБАЛ",
        "endpoint": "handball/matches",
        "teams": "handball/teams",
        "metric": "goals"
    },
}
_SPORT_API_CALLS = 0

# Hard limits: prevent one sport with many fixtures from exhausting the daily
# Sport API quota before the scanner reaches the other sports.
MAX_FIXTURES_TO_EVALUATE = 20
MAX_CONTEXT_CANDIDATES = 12

# HARD SIGNAL RULES — never publish weak statistical picks.
MIN_SIGNAL_PROBABILITY = 68.0
MIN_SIGNAL_CONFIDENCE = 78.0
MAX_SIGNAL_RISK = 35.0
MIN_VALUE_EDGE = -2.0
MIN_ODDS = 1.40
MAX_SIGNAL_ODDS = 1.70
BETANO_BOOKMAKER = "Betano"
MAX_TOTAL_SIGNALS = 5


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


def _betano_odd_for_market(sport_key, match, market_label):
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
            "bookmakerName": BETANO_BOOKMAKER,
            "oddsType": "prematch",
            "limit": 100,
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
        bookmaker = str(row.get("bookmakerName") or row.get("bookmaker") or "").strip().lower()
        if bookmaker and "betano" not in bookmaker:
            continue

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


def _market_only_candidates(sport_key, match):
    """Build conservative PREMATCH candidates from market consensus only.

    This deliberately does NOT call team-statistics endpoints. The football
    engine's decision architecture is retained: probability -> confidence ->
    risk -> value -> Betano gate, but the probability input for SPORT comes
    from bookmaker consensus because SPORT no longer uses historical stats.
    """
    mid = _match_id(match)
    if not mid:
        return []

    rows = _sport_api_get(
        f"{sport_key}/odds",
        {
            "matchId": mid,
            "oddsType": "prematch",
            "limit": 100,
            "offset": 0,
        },
    )
    if not isinstance(rows, list):
        return []

    # Gather odds by normalized market/selection from all bookmakers.
    books = {}
    betano = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        bookmaker = str(row.get("bookmakerName") or row.get("bookmaker") or "").strip()
        market_list = row.get("odds") or row.get("markets") or []
        if not isinstance(market_list, list):
            continue
        for market in market_list:
            if not isinstance(market, dict):
                continue
            market_name = str(market.get("market") or market.get("name") or market.get("type") or "").strip()
            values = market.get("values") or market.get("odds") or market.get("selections") or []
            if not isinstance(values, list):
                continue
            for selection in values:
                if not isinstance(selection, dict):
                    continue
                label = str(selection.get("value") or selection.get("label") or selection.get("name") or "").strip()
                try:
                    odd = float(selection.get("odd") if selection.get("odd") is not None else selection.get("odds"))
                except (TypeError, ValueError):
                    continue
                if odd < MIN_ODDS or odd > MAX_SIGNAL_ODDS:
                    continue
                key = (_normalize_label(market_name), _normalize_label(label))
                implied = 100.0 / odd
                books.setdefault(key, []).append((bookmaker, odd, implied))
                if bookmaker.casefold().find("betano") >= 0:
                    betano[key] = odd

    candidates=[]
    for key, entries in books.items():
        if key not in betano:
            continue
        # Ignore isolated/ambiguous market labels. A single-book price is not
        # strong enough to be called a consensus signal.
        if len(entries) < 2:
            continue
        probs=[x[2] for x in entries]
        probs.sort()
        median=probs[len(probs)//2] if len(probs)%2 else (probs[len(probs)//2-1]+probs[len(probs)//2])/2
        mean=sum(probs)/len(probs)
        spread=max(probs)-min(probs)
        confidence=max(0.0, min(95.0, 82.0 + min(10.0, len(entries)*1.5) - spread*1.2))
        probability=max(0.0, min(95.0, median))
        risk=max(0.0, min(100.0, 100.0-confidence + spread*0.8))
        odd=betano[key]
        edge=_sport_value_edge(probability, odd)
        if probability < MIN_SIGNAL_PROBABILITY or confidence < MIN_SIGNAL_CONFIDENCE or risk > MAX_SIGNAL_RISK or edge < MIN_VALUE_EDGE:
            continue
        market_name, label = key
        display = f"{market_name} — {label}".strip(" —")
        candidates.append({
            "market": display,
            "probability": round(probability,1),
            "confidence": round(confidence,1),
            "risk": round(risk,1),
            "odd": round(odd,2),
            "edge": round(edge,2),
            "bookmakers": len(entries),
        })
    candidates.sort(key=lambda x:(x["confidence"],x["probability"],x["edge"],-x["risk"]), reverse=True)
    return candidates[:3]


def _build_market_only_report_section(candidates):
    if not candidates:
        return ""
    lines=["🏆 НАЙ-СИЛНИТЕ PREMATCH ЗА ДЕНЯ", ""]
    for i,c in enumerate(candidates,1):
        home, away = _sport_match_names(c["match"], c["sport_key"])
        dt = c["datetime"]
        lines.append(f"{i}. {c['sport_name']} | {home} - {away}")
        lines.append(f"   🎯 {c['market']}")
        lines.append(f"   📊 Probability: {c['probability']:.1f}% | Confidence: {c['confidence']:.1f}% | Risk: {c['risk']:.1f}%")
        lines.append(f"   💰 Betano: {c['odd']:.2f} | Edge: {c['edge']:+.2f}% | Books: {c['bookmakers']}")
        lines.append(f"   🕒 {dt.strftime('%d.%m.%Y %H:%M')} BG")
        if i < len(candidates):
            lines.append("")
    return "\n".join(lines)

def run_sport_top3_daily_scanner(send_func=None):
    """SPORT PREMATCH — only the strongest few matches for the whole day."""
    global _SPORT_API_CALLS
    _SPORT_API_CALLS = 0
    started = time.time()
    now_bg = datetime.now(TZ)
    run_key = f"sport_prematch:{now_bg.date().isoformat()}"
    if already_ran(run_key):
        print(_signal_text(f"SPORT DAILY SCANNER ALREADY RAN: {now_bg.date().isoformat()}"), flush=True)
        return ""

    start = now_bg.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    all_signals = []

    for sport_key, cfg in SPORTS_CONFIG.items():
        try:
            fixtures = _get_sport_fixtures(cfg, start, end)
            now_scan = datetime.now(TZ)
            future = []
            for match in fixtures:
                dt = _sport_match_datetime(match)
                if not dt or dt <= now_scan:
                    continue
                league_name, country = _sport_league_country(match)
                lc = league_name.casefold()
                cc = country.casefold()
                if "friendly" in lc or "club friendly" in lc or cc in {"russia", "belarus"}:
                    continue
                future.append(match)
            future.sort(key=lambda m: _sport_match_datetime(m) or end)
            selected = future[:MAX_FIXTURES_TO_EVALUATE]

            for match in selected:
                try:
                    dt = _sport_match_datetime(match)
                    for c in _market_only_candidates(sport_key, match):
                        c.update({"sport_key": sport_key, "sport_name": cfg["name"], "match": match, "datetime": dt})
                        all_signals.append(c)
                except APIQuotaExceeded:
                    raise
                except Exception as exc:
                    print(f"SPORT MARKET ERROR: {sport_key} {exc!r}", flush=True)

            print(f"SPORT PREMATCH SCAN: {sport_key} fixtures={len(future)} selected={len(selected)}", flush=True)
        except APIQuotaExceeded:
            print(f"SPORT API QUOTA EXHAUSTED | after={sport_key}", flush=True)
            break
        except Exception as exc:
            print(f"SPORT SECTION ERROR: {sport_key}: {exc!r}", flush=True)

    # Football-style final selection: rank globally, do not force one signal
    # from every sport and do not print empty sport sections.
    all_signals.sort(key=lambda x: (x["confidence"], x["probability"], x["edge"], -x["risk"]), reverse=True)
    winners = all_signals[:MAX_TOTAL_SIGNALS]

    lines = [
        "📊 DAILY SPORT PREMATCH",
        now_bg.strftime("%d.%m.%Y"),
        "",
        "🏁 РЕЖИМ: само предстоящи срещи",
        f"Период: {start.strftime('%d.%m.%Y %H:%M')} BG → {end.strftime('%d.%m.%Y %H:%M')} BG",
        "Селекция: probability → confidence → risk → value → Betano gate",
        f"Betano ≥ {MIN_ODDS:.2f} | максимум {MAX_TOTAL_SIGNALS} сигнала общо",
        "",
    ]

    if winners:
        lines.append(_build_market_only_report_section(winners))
    else:
        lines.append("Няма достатъчно силен PREMATCH сигнал за днес.")

    lines.extend([
        "",
        f"📡 API заявки: {_SPORT_API_CALLS}",
        f"⏱ Scan time: {time.time() - started:.1f}s",
    ])
    message = "\n".join(lines)
    print(message, flush=True)
    if send_func:
        try:
            _send_sport_report_chunks(message, send_func, max_chars=3700)
        except Exception as exc:
            print(f"SPORT TELEGRAM ERROR: {exc!r}", flush=True)
            return message
    mark_ran(run_key)
    print(f"SPORT DAILY COMPLETE | day={now_bg.date().isoformat()} | api_calls={_SPORT_API_CALLS}", flush=True)
    return message


def _run_sport(day):
    global SPORT_RUNNING, SPORT_DONE_DAY

    try:
        print(f"SPORT THREAD START | day={day}", flush=True)

        result = run_sport_top3_daily_scanner(send_telegram)

        if result:
            SPORT_DONE_DAY = day
            print(f"SPORT THREAD SUCCESS | day={day}", flush=True)
        else:
            print(f"SPORT THREAD EMPTY | day={day}", flush=True)

    except APIQuotaExceeded as exc:
        SPORT_DONE_DAY = day
        print(f"SPORT THREAD QUOTA | day={day} | {exc}", flush=True)

    except Exception as exc:
        print(f"SPORT THREAD ERROR | day={day} | {exc!r}", flush=True)
        logging.exception("SPORT THREAD ERROR")

    finally:
        SPORT_RUNNING = False
        print(
            f"SPORT THREAD END | day={day} | "
            f"running={SPORT_RUNNING} | done={SPORT_DONE_DAY}",
            flush=True,
        )


def main_loop():
    global SPORT_RUNNING, SPORT_DONE_DAY

    print("SPORT-ONLY SYSTEM START", flush=True)
    print(
        "FOOTBALL DISABLED HERE — football runs in the separate system",
        flush=True,
    )
    print(
        "SPORT SCHEDULER READY | daily trigger 17:15-17:20 BG ONLY | "
        "fixture window 12:00 -> next day 12:00",
        flush=True,
    )

    while True:
        now = datetime.now(TZ)
        day = now.date().isoformat()
        minutes = now.hour * 60 + now.minute

        print(
            f"SPORT SCHEDULER TICK | {now:%Y-%m-%d %H:%M:%S} | "
            f"minutes={minutes} | running={SPORT_RUNNING} | done={SPORT_DONE_DAY}",
            flush=True,
        )

        # HARD DAILY WINDOW:
        # Never launch before 17:15 and never catch up after 17:20.
        in_launch_window = (
            SPORT_START_MINUTES <= minutes <= SPORT_LAUNCH_END_MINUTES
        )

        if in_launch_window and SPORT_DONE_DAY != day and not SPORT_RUNNING:
            if (
                hasattr(globals().get("_quota_locked"), "__call__")
                and _quota_locked("sport")
            ):
                print(f"SPORT QUOTA ALREADY LOCKED | day={day}", flush=True)
                SPORT_DONE_DAY = day
            else:
                SPORT_RUNNING = True
                print(
                    f"SPORT TRIGGER | day={day} | launch=17:15-17:20 BG",
                    flush=True,
                )

                threading.Thread(
                    target=_run_sport,
                    args=(day,),
                    daemon=True,
                ).start()

        time.sleep(30)


if __name__ == "__main__":
    main_loop()
