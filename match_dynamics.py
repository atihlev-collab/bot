"""Conservative, test-only live match dynamics evaluator.

This module samples API-Football cumulative statistics once per scan and
compares 5- and 10-minute deltas. It never sends Telegram messages and does
not modify the existing betting markets.
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("match_dynamics.db")
MIN_SAMPLE_INTERVAL = 50
TEST_MODE = True


def _number(value):
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        return float(value or 0)
    except (TypeError, ValueError):
        return None


def _read(team_stats, aliases):
    stats = team_stats.get("statistics", []) if isinstance(team_stats, dict) else []
    wanted = {str(x).strip().lower().replace("_", " ") for x in aliases}
    for item in stats:
        actual = str(item.get("type") or "").strip().lower().replace("_", " ")
        if actual in wanted:
            return _number(item.get("value"))
    return None


def _team_values(team_stats):
    return {
        "shots": _read(team_stats, ("Total Shots",)),
        "sot": _read(team_stats, ("Shots on Goal", "Shots on Target")),
        "danger": _read(team_stats, ("Dangerous Attacks",)),
        "corners": _read(team_stats, ("Corner Kicks", "Corners")),
        "fouls": _read(team_stats, ("Fouls",)),
        "yellow": _read(team_stats, ("Yellow Cards",)),
        "xg": _read(team_stats, ("Expected Goals", "Expected Goals (xG)", "xG")),
    }


def _init_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamics_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id INTEGER NOT NULL,
                sampled_at REAL NOT NULL,
                minute INTEGER NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                home_json TEXT NOT NULL,
                away_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dynamics_fixture_time
            ON dynamics_snapshots(fixture_id, sampled_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamics_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id INTEGER NOT NULL,
                market TEXT NOT NULL,
                minute INTEGER NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                confidence INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(fixture_id, market)
            )
        """)


def _window(conn, fixture_id, now, seconds):
    # Allow a 2-minute tolerance around the target window. If a reliable
    # comparison point is unavailable, the evaluator deliberately abstains.
    target = now - seconds
    row = conn.execute(
        """
        SELECT sampled_at, minute, home_goals, away_goals, home_json, away_json
        FROM dynamics_snapshots
        WHERE fixture_id = ? AND sampled_at BETWEEN ? AND ?
        ORDER BY sampled_at DESC LIMIT 1
        """,
        (fixture_id, target - 120, target + 60),
    ).fetchone()
    return row


def _delta(current, previous, key):
    a = current.get(key)
    b = previous.get(key)
    if a is None or b is None:
        return None
    return max(0.0, a - b)


def _fmt(value):
    return round(value, 2) if value is not None else None


def record_match_dynamics(fixture_id, minute, home_goals, away_goals,
                          home_stats, away_stats):
    """Persist one snapshot and log only conservative, data-backed candidates."""
    try:
        _init_db()
        now = time.time()
        home = _team_values(home_stats)
        away = _team_values(away_stats)

        # Core metrics must exist for both teams. Missing values are unknown,
        # never silently converted to zero.
        required = ("shots", "sot", "corners", "fouls", "yellow")
        if any(home[k] is None or away[k] is None for k in required):
            print("DYNAMICS ABSTAIN - REQUIRED STATS MISSING:", fixture_id)
            return []

        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            last = conn.execute(
                "SELECT sampled_at FROM dynamics_snapshots WHERE fixture_id=? ORDER BY sampled_at DESC LIMIT 1",
                (fixture_id,),
            ).fetchone()
            if last and now - last[0] < MIN_SAMPLE_INTERVAL:
                return []

            conn.execute(
                """
                INSERT INTO dynamics_snapshots
                (fixture_id, sampled_at, minute, home_goals, away_goals, home_json, away_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fixture_id, now, int(minute), int(home_goals), int(away_goals),
                 json.dumps(home), json.dumps(away)),
            )

            w5 = _window(conn, fixture_id, now, 300)
            w10 = _window(conn, fixture_id, now, 600)
            if not w5 or not w10:
                print("DYNAMICS WARMUP - WAITING FOR 5/10 MIN HISTORY:", fixture_id)
                return []

            old5h, old5a = json.loads(w5[4]), json.loads(w5[5])
            old10h, old10a = json.loads(w10[4]), json.loads(w10[5])
            d5h = {k: _delta(home, old5h, k) for k in home}
            d5a = {k: _delta(away, old5a, k) for k in away}
            d10h = {k: _delta(home, old10h, k) for k in home}
            d10a = {k: _delta(away, old10a, k) for k in away}

            # If the feed cannot provide danger/xG, those metrics are omitted;
            # they are not fabricated or used as zero.
            score_total = int(home_goals) + int(away_goals)
            goal_diff = abs(int(home_goals) - int(away_goals))
            candidates = []

            def team_is_active(short, long):
                return (
                    long["shots"] is not None and long["sot"] is not None
                    and long["shots"] >= 2 and long["sot"] >= 1
                    and short["shots"] is not None and short["shots"] >= 1
                    and (
                        (long["danger"] is not None and long["danger"] >= 6)
                        or (long["corners"] is not None and long["corners"] >= 1)
                    )
                )

            home_active = team_is_active(d5h, d10h)
            away_active = team_is_active(d5a, d10a)

            # One-sided goal candidate: sustained pressure plus a recent shot
            # and clear separation from the opponent's attacking output.
            if 35 <= minute <= 82 and goal_diff <= 1:
                home_one_sided = (
                    home_active and not away_active
                    and d10h["shots"] >= 3 and d10h["sot"] >= 1
                    and d5h["shots"] >= 1
                    and (d10a["shots"] is not None and d10a["shots"] <= 1)
                    and (d10a["sot"] is not None and d10a["sot"] == 0)
                    and (
                        d10h["danger"] is None or d10a["danger"] is None
                        or d10h["danger"] >= d10a["danger"] + 5
                    )
                )
                away_one_sided = (
                    away_active and not home_active
                    and d10a["shots"] >= 3 and d10a["sot"] >= 1
                    and d5a["shots"] >= 1
                    and (d10h["shots"] is not None and d10h["shots"] <= 1)
                    and (d10h["sot"] is not None and d10h["sot"] == 0)
                    and (
                        d10a["danger"] is None or d10h["danger"] is None
                        or d10a["danger"] >= d10h["danger"] + 5
                    )
                )
                if home_one_sided:
                    candidates.append(("NEXT GOAL HOME", 82, {"home_5m": d5h, "home_10m": d10h, "away_10m": d10a}))
                elif away_one_sided:
                    candidates.append(("NEXT GOAL AWAY", 82, {"away_5m": d5a, "away_10m": d10a, "home_10m": d10h}))

            # At least two more goals: both sides must independently show
            # repeated activity; one-sided pressure is not enough.
            if (
                25 <= minute <= 65 and score_total <= 2
                and home_active and away_active
                and d10h["sot"] >= 1 and d10a["sot"] >= 1
                and d10h["shots"] >= 2 and d10a["shots"] >= 2
            ):
                candidates.append(("OVER 1.5 REMAINING GOALS", 80, {"home_10m": d10h, "away_10m": d10a}))

            # No more goals: require a genuinely quiet 10-minute period,
            # not merely low full-match totals.
            quiet_values = (
                d10h["shots"], d10a["shots"], d10h["sot"], d10a["sot"],
                d10h["corners"], d10a["corners"]
            )
            if (
                70 <= minute <= 85
                and all(v is not None for v in quiet_values)
                and sum(d10h["shots"] + d10a["shots"] for _ in (0,)) <= 1
                and d10h["sot"] == 0 and d10a["sot"] == 0
                and d10h["corners"] + d10a["corners"] <= 1
                and (d10h["danger"] is None or d10a["danger"] is None
                     or d10h["danger"] + d10a["danger"] <= 4)
                and (d10h["xg"] is None or d10a["xg"] is None
                     or d10h["xg"] + d10a["xg"] <= 0.10)
            ):
                candidates.append(("UNDER 0.5 REMAINING GOALS", 80, {"home_10m": d10h, "away_10m": d10a}))

            # Cards: require repeated fouls in the last 10 minutes plus an
            # already physical match; score closeness adds context, not proof.
            fouls_10 = d10h["fouls"] + d10a["fouls"]
            yellows_now = home["yellow"] + away["yellow"]
            yellows_before = old10h["yellow"] + old10a["yellow"]
            if (
                55 <= minute <= 85 and fouls_10 >= 4
                and yellows_now >= 2 and yellows_now >= yellows_before
                and goal_diff <= 2
            ):
                candidates.append(("NEXT CARD", 78, {"fouls_10m": fouls_10, "yellow_total": yellows_now}))

            # Corners: require fresh attacking activity, not just a high
            # cumulative corner count.
            attacks_10 = None
            if d10h["danger"] is not None and d10a["danger"] is not None:
                attacks_10 = d10h["danger"] + d10a["danger"]
            if (
                25 <= minute <= 85
                and attacks_10 is not None and attacks_10 >= 10
                and d10h["shots"] is not None and d10a["shots"] is not None
                and d10h["shots"] + d10a["shots"] >= 3
                and (d5h["shots"] or 0) + (d5a["shots"] or 0) >= 1
            ):
                candidates.append(("NEXT CORNER", 78, {"dangerous_attacks_10m": attacks_10, "home_10m": d10h, "away_10m": d10a}))

            stored = []
            for market, confidence, evidence in candidates:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO dynamics_predictions
                    (fixture_id, market, minute, home_goals, away_goals, confidence, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fixture_id, market, int(minute), int(home_goals), int(away_goals),
                     confidence, json.dumps(evidence, default=str), now),
                )
                if cur.rowcount == 1:
                    stored.append(market)
                    print("DYNAMICS TEST CANDIDATE:", fixture_id, market,
                          "MIN=", minute, "CONF=", confidence,
                          "SCORE=", home_goals, "-", away_goals,
                          "EVIDENCE=", evidence)

            # Keep this database bounded; retain recent match history only.
            conn.execute("DELETE FROM dynamics_snapshots WHERE sampled_at < ?", (now - 14 * 86400,))
            conn.execute("DELETE FROM dynamics_predictions WHERE created_at < ?", (now - 30 * 86400,))
            return stored

    except Exception as exc:
        print("MATCH DYNAMICS ERROR:", fixture_id, repr(exc))
        return []
