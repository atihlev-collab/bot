import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot

from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================
# CONFIG
# =========================
HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

PREMATCH_INTERVAL = 900
LIVE_INTERVAL = 60

logging.basicConfig(level=logging.WARNING)

sent_prematch = set()
live_sent = set()

# =========================
# BLOCKED COUNTRIES
# =========================
BLOCKED_COUNTRIES = [
    "Russia",
    "Belarus"
]

# =========================
# TELEGRAM
# =========================
async def send_signal(bot, msg):

    try:

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg
        )

    except Exception as e:
        print("TELEGRAM ERROR:", e)

# =========================================================
# PREMATCH
# =========================================================
async def prematch(bot):

    while True:

        try:

            now = datetime.now(TZ)

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?next=50",
                headers=HEADERS
            ).json()

            matches = r.get("response", [])

            prematch_list = []

            for m in matches:

                try:

                    fixture = m["fixture"]["id"]

                    if fixture in sent_prematch:
                        continue

                    date = datetime.fromisoformat(
                        m["fixture"]["date"].replace("Z", "+00:00")
                    ).astimezone(TZ)

                    if date <= now:
                        continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    league_name = m["league"]["name"]
                    country = m["league"]["country"]

                    # =========================
                    # BLOCK COUNTRIES
                    # =========================
                    if country in BLOCKED_COUNTRIES:
                        continue

                    home_id = m["teams"]["home"]["id"]
                    away_id = m["teams"]["away"]["id"]

                    league = m["league"]["id"]
                    season = m["league"]["season"]

                    # =========================
                    # TEAM STATS
                    # =========================
                    home_stats = requests.get(
                        f"https://v3.football.api-sports.io/teams/statistics?league={league}&season={season}&team={home_id}",
                        headers=HEADERS
                    ).json()

                    away_stats = requests.get(
                        f"https://v3.football.api-sports.io/teams/statistics?league={league}&season={season}&team={away_id}",
                        headers=HEADERS
                    ).json()

                    hs = home_stats["response"]
                    aws = away_stats["response"]

                    # =========================
                    # GOALS AVG
                    # =========================
                    home_avg = float(
                        hs["goals"]["for"]["average"]["total"]["home"] or 0
                    )

                    away_avg = float(
                        aws["goals"]["for"]["average"]["total"]["away"] or 0
                    )

                    # =========================
                    # OVER STATS
                    # =========================
                    home_over = int(
                        hs["fixtures"]["over_2_5"]["total"] or 0
                    )

                    away_over = int(
                        aws["fixtures"]["over_2_5"]["total"] or 0
                    )

                    # =========================
                    # SCORE
                    # =========================
                    score = 0

                    if home_avg >= 1.1:
                        score += 1

                    if away_avg >= 0.9:
                        score += 1

                    if home_over >= 4:
                        score += 1

                    if away_over >= 4:
                        score += 1

                    if score < 2:
                        continue

                    # =========================
                    # ODDS
                    # =========================
                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fixture}",
                        headers=HEADERS
                    ).json()

                    odds_response = od.get("response", [])

                    if not odds_response:
                        continue

                    odds_map = {}

                    for b in odds_response[0]["bookmakers"]:

                        for bet in b["bets"]:

                            for v in bet["values"]:

                                odds_map[v["value"]] = float(v["odd"])

                    odd = (
                        odds_map.get("Over 2.5")
                        or odds_map.get("Over 1.5")
                    )

                    if not odd:
                        continue

                    if odd < 1.35 or odd > 2.50:
                        continue

                    # =========================
                    # MARKET
                    # =========================
                    if odd <= 1.65:
                        market = "OVER 1.5 GOALS"
                    else:
                        market = "OVER 2.5 GOALS"

                    prematch_list.append({
                        "fixture": fixture,
                        "home": home,
                        "away": away,
                        "league": league_name,
                        "country": country,
                        "score": score,
                        "odd": odd,
                        "market": market,
                        "time": date.strftime("%H:%M")
                    })

                except Exception as e:
                    print("PREMATCH MATCH ERROR:", e)

            # =========================
            # TOP 3
            # =========================
            prematch_list = sorted(
                prematch_list,
                key=lambda x: x["score"],
                reverse=True
            )

            top_matches = prematch_list[:3]

            for game in top_matches:

                msg = f"""
📈 PREMATCH

🌍 {game['country']}
🏆 {game['league']}

🏟 {game['home']} vs {game['away']}
⏰ {game['time']}

🎯 MARKET: {game['market']}

📊 Score: {game['score']}/4
📈 Odd: {game['odd']}
"""

                await send_signal(bot, msg)

                sent_prematch.add(game["fixture"])

        except Exception as e:
            print("PREMATCH ERROR:", e)

        await asyncio.sleep(PREMATCH_INTERVAL)

# =========================================================
# LIVE
# =========================================================
async def live(bot):

    while True:

        try:

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=HEADERS
            ).json()

            matches = r.get("response", [])

            for m in matches:

                try:

                    fixture = m["fixture"]["id"]

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    league_name = m["league"]["name"]
                    country = m["league"]["country"]

                    # =========================
                    # BLOCK COUNTRIES
                    # =========================
                    if country in BLOCKED_COUNTRIES:
                        continue

                    minute = m["fixture"]["status"]["elapsed"] or 0

                    if minute < 20 or minute > 75:
                        continue

                    home_goals = m["goals"]["home"] or 0
                    away_goals = m["goals"]["away"] or 0

                    # =========================
                    # STATS
                    # =========================
                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture}",
                        headers=HEADERS
                    ).json()

                    stats = sr.get("response", [])

                    if len(stats) < 2:
                        continue

                    h_stats = stats[0]["statistics"]
                    a_stats = stats[1]["statistics"]

                    # attacks
                    home_attacks = int(h_stats[0]["value"] or 0)
                    away_attacks = int(a_stats[0]["value"] or 0)

                    # shots
                    home_shots = int(h_stats[2]["value"] or 0)
                    away_shots = int(a_stats[2]["value"] or 0)

                    total_attacks = home_attacks + away_attacks
                    total_shots = home_shots + away_shots

                    pressure = total_attacks / max(1, minute)
                    shot_rate = total_shots / max(1, minute)

                    # =========================
                    # ODDS
                    # =========================
                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fixture}",
                        headers=HEADERS
                    ).json()

                    bookmakers = od.get("response", [])

                    if not bookmakers:
                        continue

                    odds_map = {}

                    for b in bookmakers[0]["bookmakers"]:

                        for bet in b["bets"]:

                            for v in bet["values"]:

                                odds_map[v["value"]] = float(v["odd"])

                    # =====================================================
                    # OVER 1.5 GOALS
                    # =====================================================
                    over_key = f"OVER15_{fixture}"

                    if over_key not in live_sent:

                        if (
                            minute >= 20
                            and total_attacks >= 16
                            and total_shots >= 2
                            and pressure >= 0.40
                            and shot_rate >= 0.06
                        ):

                            odd = (
                                odds_map.get("Over 1.5")
                                or odds_map.get("Over 2.5")
                            )

                            if odd and odd >= 1.30:

                                msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 MARKET: OVER 1.5 GOALS

📊 Total attacks: {total_attacks}
📊 Total shots: {total_shots}

📈 Odd: {odd}
"""

                                await send_signal(bot, msg)

                                live_sent.add(over_key)

                    # =====================================================
                    # UNDER 1.5 GOALS
                    # =====================================================
                    under_key = f"UNDER15_{fixture}"

                    if under_key not in live_sent:

                        if (
                            minute >= 25
                            and minute <= 70
                            and total_attacks <= 12
                            and total_shots <= 1
                            and pressure <= 0.30
                            and (home_goals + away_goals) <= 1
                        ):

                            odd = odds_map.get("Under 1.5")

                            if odd and odd >= 1.40:

                                msg = f"""
❄️ LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 MARKET: UNDER 1.5 GOALS

📊 Total attacks: {total_attacks}
📊 Total shots: {total_shots}

📈 Odd: {odd}
"""

                                await send_signal(bot, msg)

                                live_sent.add(under_key)

                    # =====================================================
                    # NEXT GOAL HOME
                    # =====================================================
                    next_home_key = f"NEXTHOME_{fixture}"

                    if next_home_key not in live_sent:

                        dominance_home = (
                            home_attacks - away_attacks
                        )

                        if (
                            minute >= 20
                            and minute <= 75
                            and dominance_home >= 8
                            and home_shots >= away_shots + 2
                            and home_shots >= 2
                            and pressure >= 0.45
                        ):

                            odd = (
                                odds_map.get(home)
                                or odds_map.get("Home")
                            )

                            if odd and odd >= 1.50:

                                msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 MARKET: NEXT GOAL HOME

📊 Home attacks: {home_attacks}
📊 Away attacks: {away_attacks}

📊 Home shots: {home_shots}
📊 Away shots: {away_shots}

📈 Odd: {odd}
"""

                                await send_signal(bot, msg)

                                live_sent.add(next_home_key)

                    # =====================================================
                    # NEXT GOAL AWAY
                    # =====================================================
                    next_away_key = f"NEXTAWAY_{fixture}"

                    if next_away_key not in live_sent:

                        dominance_away = (
                            away_attacks - home_attacks
                        )

                        if (
                            minute >= 20
                            and minute <= 75
                            and dominance_away >= 8
                            and away_shots >= home_shots + 2
                            and away_shots >= 2
                            and pressure >= 0.45
                        ):

                            odd = (
                                odds_map.get(away)
                                or odds_map.get("Away")
                            )

                            if odd and odd >= 1.50:

                                msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 MARKET: NEXT GOAL AWAY

📊 Home attacks: {home_attacks}
📊 Away attacks: {away_attacks}

📊 Home shots: {home_shots}
📊 Away shots: {away_shots}

📈 Odd: {odd}
"""

                                await send_signal(bot, msg)

                                live_sent.add(next_away_key)

                except Exception as e:
                    print("LIVE MATCH ERROR:", e)

        except Exception as e:
            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# MAIN
# =========================================================
async def main():

    bot = Bot(token=BOT_TOKEN)

    print("🚀 SYSTEM RUNNING")

    await asyncio.gather(
        prematch(bot),
        live(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
