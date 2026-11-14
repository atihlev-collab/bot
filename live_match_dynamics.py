"""Rolling live-match dynamics ported from main-v4.

Snapshots are persisted locally. The evaluator abstains until it has a
reliable baseline at least two minutes old and no more than six minutes old.
"""
import math
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.getenv("BOT_MATCH_DYNAMICS_DB", "bot_match_dynamics.db")
SNAPSHOT_INTERVAL_SECONDS = 60
DYNAMICS_WINDOW_SECONDS = 360
MIN_DYNAMICS_AGE_SECONDS = 120


def _num(value):
    try:
        if value is None:
            return 0.0
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _total(stats, *keys):
    home = stats.get("home") or {}
    away = stats.get("away") or {}
    total = 0.0
    for side in (home, away):
        for key in keys:
            if key in side and side.get(key) is not None:
                total += _num(side.get(key))
                break
    return total


@contextmanager
def _database():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_match_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id INTEGER NOT NULL,
            captured_at REAL NOT NULL,
            minute INTEGER NOT NULL,
            home_goals INTEGER NOT NULL DEFAULT 0,
            away_goals INTEGER NOT NULL DEFAULT 0,
            shots REAL NOT NULL DEFAULT 0,
            shots_on REAL NOT NULL DEFAULT 0,
            dangerous REAL NOT NULL DEFAULT 0,
            xg REAL NOT NULL DEFAULT 0,
            corners REAL NOT NULL DEFAULT 0,
            fouls REAL NOT NULL DEFAULT 0,
            yellow REAL NOT NULL DEFAULT 0,
            red REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_live_snapshots_fixture_time
        ON live_match_snapshots(fixture_id, captured_at)
    """)
    return conn


def record_live_snapshot(fixture_id, minute, stats, home_goals=0, away_goals=0, now=None):
    """Persist one cumulative-stat snapshot at most once per minute per fixture."""
    if not fixture_id or not isinstance(stats, dict):
        return
    now = float(now if now is not None else time.time())
    row = (
        int(fixture_id), now, int(_num(minute)), int(_num(home_goals)),
        int(_num(away_goals)),
        _total(stats, "total shots"),
        _total(stats, "shots on goal"),
        _total(stats, "dangerous attacks"),
        _total(stats, "expected goals"),
        _total(stats, "corner kicks"),
        _total(stats, "fouls", "total fouls"),
        _total(stats, "yellow cards", "yellow card"),
        _total(stats, "red cards"),
    )
    try:
        with _database() as conn:
            latest = conn.execute(
                "SELECT captured_at FROM live_match_snapshots "
                "WHERE fixture_id=? ORDER BY captured_at DESC LIMIT 1",
                (int(fixture_id),),
            ).fetchone()
            if latest and now - float(latest[0]) < SNAPSHOT_INTERVAL_SECONDS:
                return
            conn.execute("""
                INSERT INTO live_match_snapshots (
                    fixture_id,captured_at,minute,home_goals,away_goals,
                    shots,shots_on,dangerous,xg,corners,fouls,yellow,red
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, row)
            conn.execute(
                "DELETE FROM live_match_snapshots WHERE captured_at < ?",
                (now - 7 * 86400,),
            )
    except sqlite3.Error as exc:
        print("LIVE DYNAMICS SNAPSHOT ERROR:", fixture_id, repr(exc), flush=True)


def evaluate_match_dynamics(fixture_id, minute, stats, home_goals=0, away_goals=0, now=None):
    """Return rolling deltas and a conservative no-more-goals estimate."""
    now = float(now if now is not None else time.time())
    current = {
        "shots": _total(stats, "total shots"),
        "shots_on": _total(stats, "shots on goal"),
        "dangerous": _total(stats, "dangerous attacks"),
        "xg": _total(stats, "expected goals"),
        "corners": _total(stats, "corner kicks"),
        "fouls": _total(stats, "fouls", "total fouls"),
        "yellow": _total(stats, "yellow cards", "yellow card"),
        "red": _total(stats, "red cards"),
    }
    result = {
        "ready": False,
        "window_minutes": 0.0,
        "deltas": {key: 0.0 for key in current},
        "attack_index": 0.0,
        "cooling": False,
        "score_changed_in_window": False,
        "goal_lambda": None,
        "no_more_goals": None,
        "one_or_more_goals": None,
        "two_or_more_goals": None,
        "card_pressure": 0.0,
        "corner_pressure": 0.0,
    }
    if not fixture_id:
        return result

    try:
        with _connect() as conn:
            baseline = conn.execute("""
                SELECT captured_at, home_goals, away_goals, shots, shots_on, dangerous, xg,
                       corners, fouls, yellow, red
                FROM live_match_snapshots
                WHERE fixture_id=? AND captured_at<=? AND captured_at>=?
                ORDER BY captured_at ASC LIMIT 1
            """, (
                int(fixture_id),
                now - MIN_DYNAMICS_AGE_SECONDS,
                now - DYNAMICS_WINDOW_SECONDS,
            )).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return result

    if not baseline:
        return result

    age = max(1.0, now - float(baseline[0]))
    if age < MIN_DYNAMICS_AGE_SECONDS:
        return result
    window_minutes = age / 60.0
    result["score_changed_in_window"] = (
        int(_num(home_goals)) != int(_num(baseline[1]))
        or int(_num(away_goals)) != int(_num(baseline[2]))
    )
    old_keys = ("shots", "shots_on", "dangerous", "xg", "corners", "fouls", "yellow", "red")
    deltas = {
        key: max(0.0, current[key] - _num(baseline[index + 3]))
        for index, key in enumerate(old_keys)
    }
    result["ready"] = True
    result["window_minutes"] = round(window_minutes, 2)
    result["deltas"] = {key: round(value, 2) for key, value in deltas.items()}

    attack_index = (
        deltas["shots"] * 5.0
        + deltas["shots_on"] * 15.0
        + deltas["dangerous"] * 0.30
        + deltas["xg"] * 35.0
        + deltas["corners"] * 8.0
    )
    attack_index = min(100.0, attack_index * (5.0 / max(2.0, window_minutes)))
    result["attack_index"] = round(attack_index, 1)
    result["cooling"] = (
        deltas["shots"] <= 1
        and deltas["shots_on"] == 0
        and deltas["dangerous"] < 5
        and deltas["xg"] < 0.03
    )

    elapsed = max(10.0, min(float(minute or 0), 95.0))
    remaining = max(0.0, 95.0 - elapsed)
    observed_xg = current["xg"]
    if observed_xg <= 0:
        observed_xg = current["shots_on"] * 0.11 + current["shots"] * 0.025
    baseline_rate = observed_xg / elapsed

    recent_xg_proxy = deltas["xg"]
    if recent_xg_proxy <= 0.005:
        recent_xg_proxy = deltas["shots_on"] * 0.11 + deltas["shots"] * 0.025
    recent_rate = recent_xg_proxy / max(1.0, window_minutes * 60.0)

    rate = baseline_rate * 0.65 + recent_rate * 0.35
    if result["cooling"]:
        rate *= 0.68
    elif attack_index >= 35:
        rate *= 1.12
    rate = max(0.0, min(0.08, rate))
    lam = min(5.0, rate * remaining * 0.78)
    p0 = math.exp(-lam) * 100.0
    p1 = (1.0 - math.exp(-lam)) * 100.0
    p2 = (1.0 - math.exp(-lam) * (1.0 + lam)) * 100.0
    result["goal_lambda"] = round(lam, 3)
    result["no_more_goals"] = round(max(0.0, min(90.0, p0)), 1)
    result["one_or_more_goals"] = round(max(0.0, min(100.0, p1)), 1)
    result["two_or_more_goals"] = round(max(0.0, min(100.0, p2)), 1)

    result["card_pressure"] = round(min(
        100.0,
        deltas["fouls"] * 7.0
        + deltas["yellow"] * 28.0
        + deltas["red"] * 45.0
        + (12.0 if deltas["fouls"] >= 5 and abs(deltas["yellow"]) > 0 else 0.0),
    ), 1)
    result["corner_pressure"] = round(min(
        100.0,
        deltas["corners"] * 32.0
        + deltas["shots"] * 2.0
        + deltas["dangerous"] * 0.12
        + deltas["shots_on"] * 8.0,
    ), 1)
    return result
