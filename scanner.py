#=======================================================
# DAILY STATISTICAL SCANNER
# =========================================================
# Runs once at/after 11:00 and once at/after 21:00 Bulgaria time.
# 11:00: today's fixtures 11:00-23:59 BG
# 21:00: tomorrow's fixtures 00:00-10:00 BG
# =========================================================

import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import API_KEY, CHAT_ID, BOT_TOKEN

from PIL import Image, ImageDraw, ImageFont

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
TZ = ZoneInfo("Europe/Sofia")
DB_FILE = "v3_ai.db"
HISTORY_GAMES = 5
MAX_WORKERS = 8


def _api(endpoint, params=None, timeout=25):
    try:
        r = requests.get(
            f"{BASE_URL}/{endpoint}",
            headers=HEADERS,
            params=params or {},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            print("SCANNER API ERROR:", data.get("errors"))
            return []
        return data.get("response", [])
    except Exception as e:
        print("SCANNER REQUEST ERROR:", endpoint, repr(e))
        return []


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


def get_team_history(team_id):
    fixtures = _api("fixtures", {"team": team_id, "last": HISTORY_GAMES})
    out = []
    for f in fixtures:
        status = (f.get("fixture", {}).get("status", {}) or {}).get("short", "")
        if status in {"FT", "AET", "PEN"}:
            out.append(f)
    return out[-HISTORY_GAMES:]


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


def get_fixture_stats(fixture):
    fid = fixture["fixture"]["id"]
    cached = _read_cached_stat(fid)
    if cached is not None:
        return cached

    response = _api("fixtures/statistics", {"fixture": fid})
    data = {}
    for block in response:
        tid = block.get("team", {}).get("id")
        if not tid:
            continue
        values = {}
        for item in block.get("statistics", []) or []:
            name = _norm(item.get("type"))
            value = _safe_float(item.get("value"))
            values[name] = value
        data[str(tid)] = values

    # Goals are always taken from the fixture score, not statistics.
    home_id = str(fixture.get("teams", {}).get("home", {}).get("id"))
    away_id = str(fixture.get("teams", {}).get("away", {}).get("id"))
    goals = fixture.get("goals", {}) or {}
    data.setdefault(home_id, {})["goals_scored"] = _safe_float(goals.get("home"))
    data.setdefault(away_id, {})["goals_scored"] = _safe_float(goals.get("away"))
    data.setdefault(home_id, {})["goals_conceded"] = _safe_float(goals.get("away"))
    data.setdefault(away_id, {})["goals_conceded"] = _safe_float(goals.get("home"))

    _write_cached_stat(fid, data)
    return data


def build_team_profile(team_id, histories):
    buckets = {
        "corners": [],
        "shots": [],
        "cards": [],
        "goals_scored": [],
        "goals_conceded": [],
    }

    for fixture in histories:
        try:
            stats = get_fixture_stats(fixture)
            s = stats.get(str(team_id), {})
        except Exception:
            continue

        # API-Football uses these normalized names.
        for key, aliases in {
            "corners": ["corner kicks"],
            "shots": ["total shots"],
            "cards": ["yellow cards", "cards"],
        }.items():
            value = next((s.get(a) for a in aliases if s.get(a) is not None), None)
            if value is not None:
                buckets[key].append(value)

        for key in ("goals_scored", "goals_conceded"):
            if s.get(key) is not None:
                buckets[key].append(s[key])

    profile = {}
    for key, vals in buckets.items():
        # Never convert missing data to zero.
        profile[key] = (sum(vals) / len(vals), len(vals)) if len(vals) >= HISTORY_GAMES else None
    return profile


def betano_markets(fixture_id):
    response = _api("odds", {"fixture": fixture_id})
    if not response:
        return set()

    bookmaker = None
    for b in response[0].get("bookmakers", []) or []:
        if b.get("id") == 32 or _norm(b.get("name")) == "betano":
            bookmaker = b
            break
    if not bookmaker:
        return set()

    names = set()
    for bet in bookmaker.get("bets", []) or []:
        name = _norm(bet.get("name"))
        if name:
            names.add(name)
    return names


def market_available(names, market):
    if market == "corners":
        return any("corner" in n for n in names)
    if market == "cards":
        return any(("yellow card" in n or re.search(r"(^| )cards?( |$)", n)) for n in names)
    if market == "shots":
        return any("shot" in n for n in names)
    return True


def analyse_fixture(fixture, team_histories):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    hp = build_team_profile(home["id"], team_histories.get(home["id"], []))
    ap = build_team_profile(away["id"], team_histories.get(away["id"], []))

    markets = {}
    try:
        betano = betano_markets(fixture["fixture"]["id"])
    except Exception:
        betano = set()

    # For corners/shots/cards we intentionally follow the user's requested
    # method: home average produced + away average produced.
    for market in ("corners", "shots", "cards"):
        if not market_available(betano, market):
            continue
        h = hp.get(market)
        a = ap.get(market)
        if h and a:
            markets[market] = {
                "expected": h[0] + a[0],
                "home": h[0],
                "away": a[0],
                "sample": min(h[1], a[1]),
            }

    # Goals use a balanced scored/conceded model rather than adding only
    # scoring averages, which systematically overstates expected goals.
    hs = hp.get("goals_scored")
    hc = hp.get("goals_conceded")
    as_ = ap.get("goals_scored")
    ac = ap.get("goals_conceded")
    if hs and hc and as_ and ac:
        home_xg = (hs[0] + ac[0]) / 2
        away_xg = (as_[0] + hc[0]) / 2
        markets["goals"] = {
            "expected": home_xg + away_xg,
            "home": home_xg,
            "away": away_xg,
            "sample": min(hs[1], hc[1], as_[1], ac[1]),
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


def format_market(results, key, label, emoji):
    valid = [r for r in results if key in r["markets"]]
    high = sorted(valid, key=lambda r: r["markets"][key]["expected"], reverse=True)[:3]
    low = sorted(valid, key=lambda r: r["markets"][key]["expected"])[:3]

    lines = [f"{emoji} {label} — 🔥 НАЙ-МНОГО"]
    if not high:
        lines.append("Няма достатъчно реални данни + подходящ Betano пазар.")
    else:
        for i, r in enumerate(high, 1):
            x = r["markets"][key]
            lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
            lines.append(f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}")

    lines.append("")
    lines.append(f"{emoji} {label} — ❄️ НАЙ-МАЛКО")
    if not low:
        lines.append("Няма достатъчно реални данни + подходящ Betano пазар.")
    else:
        for i, r in enumerate(low, 1):
            x = r["markets"][key]
            lines.append(f"{i}. {r['home_name']} - {r['away_name']}")
            lines.append(f"   {x['home']:.2f} + {x['away']:.2f} = {x['expected']:.2f}")
    return "\n".join(lines)



def _scanner_font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _render_daily_scanner_image(message, mode="day"):
    """Render the complete DAILY scanner as a large, bold Telegram image."""
    width = 1200
    margin_x = 55
    top = 45
    bottom = 55

    title_font = _scanner_font(44, True)
    date_font = _scanner_font(30, True)
    section_font = _scanner_font(36, True)
    match_font = _scanner_font(32, True)
    value_font = _scanner_font(30, True)
    normal_font = _scanner_font(28, False)
    bold_font = _scanner_font(28, True)

    lines = message.splitlines()
    prepared = []
    for line in lines:
        text = line.rstrip()
        if not text:
            prepared.append(("blank", ""))
            continue
        stripped = text.strip()
        if stripped == "📊 DAILY STATISTICAL SCANNER":
            prepared.append(("title", stripped))
        elif re.match(r"^\d{2}\.\d{2}\.\d{4}$", stripped):
            prepared.append(("date", stripped))
        elif any(stripped.startswith(e) for e in ("🚩", "🟨", "🎯", "⚽")):
            prepared.append(("section", stripped))
        elif re.match(r"^\d+\.\s", stripped):
            prepared.append(("match", stripped))
        elif stripped.startswith("⏱"):
            prepared.append(("value", stripped))
        elif " + " in stripped and " = " in stripped:
            prepared.append(("value", stripped))
        else:
            prepared.append(("normal", stripped))

    fonts = {
        "title": title_font,
        "date": date_font,
        "section": section_font,
        "match": match_font,
        "value": value_font,
        "normal": normal_font,
    }

    # Estimate height from actual font metrics.
    heights = {
        "title": 62, "date": 45, "section": 54,
        "match": 48, "value": 43, "normal": 40, "blank": 25,
    }
    height = top + bottom + sum(heights[k] for k, _ in prepared)
    img = Image.new("RGB", (width, max(height, 400)), "white")
    draw = ImageDraw.Draw(img)

    y = top
    for kind, text in prepared:
        if kind == "blank":
            y += heights[kind]
            continue
        f = fonts[kind]
        draw.text((margin_x, y), text, font=f, fill="black")
        y += heights[kind]

    path = f"/tmp/daily_scanner_{mode}.jpg"
    img.save(path, "JPEG", quality=95, optimize=True)
    return path


def _send_daily_scanner_photo(photo_path, mode="day"):
    try:
        with open(photo_path, "rb") as photo:
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": CHAT_ID,
                    "caption": "📊 DAILY STATISTICAL SCANNER"
                },
                files={"photo": photo},
                timeout=30,
            )
        if response.status_code == 200:
            return True
        print("SCANNER TELEGRAM PHOTO FAILED:", response.status_code, response.text)
        return False
    except Exception as exc:
        print("SCANNER TELEGRAM PHOTO ERROR:", repr(exc))
        return False

def run_daily_scanner(mode="day", reference_date=None, send_func=None):
    init_scanner_db()
    now_bg = datetime.now(TZ)
    ref = reference_date or now_bg.date()
    ref = ref if hasattr(ref, "year") else now_bg.date()

    if mode == "day":
        # 11:00 BG -> today's fixtures 11:00-23:59 BG
        start = datetime(ref.year, ref.month, ref.day, 11, 0, tzinfo=TZ)
        end = datetime(ref.year, ref.month, ref.day + 1, 0, 0, tzinfo=TZ)
        title = "11:00 ДНЕВЕН СКЕНЕР"
    else:
        # 21:00 BG -> tomorrow's fixtures 00:00-09:59 BG
        next_day = ref + timedelta(days=1)
        start = datetime(next_day.year, next_day.month, next_day.day, 0, 0, tzinfo=TZ)
        end = datetime(next_day.year, next_day.month, next_day.day, 10, 0, tzinfo=TZ)
        title = "21:00 НОЩЕН СКЕНЕР"

    matches = get_fixtures_for_window(start, end)
    print(f"SCANNER {mode.upper()}: {len(matches)} upcoming fixtures")

    # Get each unique team's last five fixtures only once.
    team_ids = set()
    for m in matches:
        team_ids.add(m["teams"]["home"]["id"])
        team_ids.add(m["teams"]["away"]["id"])

    histories = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(get_team_history, tid): tid for tid in team_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                histories[tid] = fut.result()
            except Exception:
                histories[tid] = []

    # Historical fixture statistics are cached in SQLite, so a second scan
    # does not download the same statistics again.
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(analyse_fixture, m, histories): m for m in matches}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result["markets"]:
                    results.append(result)
            except Exception as e:
                print("SCANNER MATCH ERROR:", repr(e))

    results.sort(key=lambda x: x["date"])
    lines = [
        "📊 DAILY STATISTICAL SCANNER",
        now_bg.strftime("%d.%m.%Y"),
        f"\n{title}",
        f"Мачове в прозореца: {len(matches)}",
        f"Мачове с поне един валиден пазар: {len(results)}",
        f"История на отбор: {HISTORY_GAMES} мача",
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

    # Telegram receives the DAILY scanner as an image so the complete signal
    # is genuinely larger/bolder, not just Markdown/HTML formatting.
    try:
        photo_path = _render_daily_scanner_image(message, mode)
        _send_daily_scanner_photo(photo_path, mode)
    except Exception as exc:
        print("SCANNER IMAGE ERROR:", repr(exc))
        # Keep the existing text fallback if image generation fails.
        if send_func:
            send_func(message)

    return message


_START = time.time()


def run_due_scans(send_func):
    """Run the single statistical scanner on its two daily schedules."""
    init_scanner_db()
    now = datetime.now(TZ)
    today = now.date()

    # 11:00 BG: today's fixtures 11:00-23:59.
    if now.hour >= 11 and now.hour < 21:
        key = f"day11:{today.isoformat()}"
        if not already_ran(key):
            print("DAILY SCANNER 11:00 STARTED")
            run_daily_scanner("day", today, send_func)
            mark_ran(key)
            print("DAILY SCANNER 11:00 FINISHED")

    # 21:00 BG: tomorrow's fixtures 00:00-09:59.
    if now.hour >= 21:
        key = f"night21:{today.isoformat()}"
        if not already_ran(key):
            print("DAILY SCANNER 21:00 STARTED")
            run_daily_scanner("night", today, send_func)
            mark_ran(key)
            print("DAILY SCANNER 21:00 FINISHED")
