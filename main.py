import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

from config import BOT_TOKEN, API_KEY, CHAT_ID

# =========================
# CONFIG
# =========================
HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

LIVE_INTERVAL = 60

logging.basicConfig(level=logging.WARNING)

live_sent = set()

BLOCKED_COUNTRIES = [
    "Russia",
    "Belarus"
]

# =========================
# TELEGRAM BOT
# =========================
bot = Bot(token=BOT_TOKEN)

# =========================================================
# HELPERS
# =========================================================
def get_best_matches(mode="today"):

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=100",
            headers=HEADERS
        ).json()

        matches = r.get("response", [])

        prematch_list = []

        now = datetime.now(TZ)

        for m in matches:

            try:

                fixture = m["fixture"]["id"]

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                # =====================================
                # TODAY / NIGHT FILTER
                # =====================================
                if mode == "today":

                    if hour < 8 or hour > 22:
                        continue

                if mode == "night":

                    if hour < 22 and hour > 7:
                        continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                country = m["league"]["country"]
                league_name = m["league"]["name"]

                if country in BLOCKED_COUNTRIES:
                    continue

                home_id = m["teams"]["home"]["id"]
                away_id = m["teams"]["away"]["id"]

                league = m["league"]["id"]
                season = m["league"]["season"]

                # =====================================
                # TEAM STATS
                # =====================================
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

                home_avg = float(
                    hs["goals"]["for"]["average"]["total"]["home"] or 0
                )

                away_avg = float(
                    aws["goals"]["for"]["average"]["total"]["away"] or 0
                )

                home_over = int(
                    hs["fixtures"]["over_2_5"]["total"] or 0
                )

                away_over = int(
                    aws["fixtures"]["over_2_5"]["total"] or 0
                )

                # =====================================
                # SCORE
                # =====================================
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

                # =====================================
                # ODDS
                # =====================================
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

                # =====================================
                # MARKET
                # =====================================
                if odd <= 1.65:
                    market = "OVER 1.5 GOALS"
                else:
                    market = "OVER 2.5 GOALS"

                prematch_list.append({
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
                print("MATCH ERROR:", e)

        # =====================================
        # SORT
        # =====================================
        prematch_list = sorted(
            prematch_list,
            key=lambda x: (x["score"], x["odd"]),
            reverse=True
        )

        return prematch_list[:3]

    except Exception as e:
        print("GET MATCHES ERROR:", e)
        return []

# =========================================================
# TODAY COMMAND
# =========================================================
def today(update: Update, context: CallbackContext):

    matches = get_best_matches("today")

    if not matches:

        update.message.reply_text("❌ Няма намерени мачове.")
        return

    msg = "📈 TOP TODAY MATCHES\n"

    for game in matches:

        msg += f"""

🌍 {game['country']}
🏆 {game['league']}

🏟 {game['home']} vs {game['away']}
⏰ {game['time']}

🎯 {game['market']}

📊 Score: {game['score']}/4
📈 Odd: {game['odd']}
"""

    update.message.reply_text(msg)

# =========================================================
# NIGHT COMMAND
# =========================================================
def night(update: Update, context: CallbackContext):

    matches = get_best_matches("night")

    if not matches:

        update.message.reply_text("❌ Няма намерени нощни мачове.")
        return

    msg = "🌙 TOP NIGHT MATCHES\n"

    for game in matches:

        msg += f"""

🌍 {game['country']}
🏆 {game['league']}

🏟 {game['home']} vs {game['away']}
⏰ {game['time']}

🎯 {game['market']}

📊 Score: {game['score']}/4
📈 Odd: {game['odd']}
"""

    update.message.reply_text(msg)

# =========================================================
# LIVE SYSTEM
# =========================================================
async def live_loop():

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

                    if country in BLOCKED_COUNTRIES:
                        continue

                    minute = m["fixture"]["status"]["elapsed"] or 0

                    if minute < 20 or minute > 75:
                        continue

                    home_goals = m["goals"]["home"] or 0
                    away_goals = m["goals"]["away"] or 0

                    # =====================================
                    # STATS
                    # =====================================
                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture}",
                        headers=HEADERS
                    ).json()

                    stats = sr.get("response", [])

                    if len(stats) < 2:
                        continue

                    h_stats = stats[0]["statistics"]
                    a_stats = stats[1]["statistics"]

                    home_attacks = int(h_stats[0]["value"] or 0)
                    away_attacks = int(a_stats[0]["value"] or 0)

                    home_shots = int(h_stats[2]["value"] or 0)
                    away_shots = int(a_stats[2]["value"] or 0)

                    total_attacks = home_attacks + away_attacks
                    total_shots = home_shots + away_shots

                    pressure = total_attacks / max(1, minute)

                    # =====================================
                    # ODDS
                    # =====================================
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
                    # OVER 1.5
                    # =====================================================
                    over_key = f"OVER15_{fixture}"

                    if over_key not in live_sent:

                        if (
                            total_attacks >= 16
                            and total_shots >= 2
                            and pressure >= 0.40
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

🎯 OVER 1.5 GOALS

📊 Attacks: {total_attacks}
📊 Shots: {total_shots}

📈 Odd: {odd}
"""

                                await bot.send_message(
                                    chat_id=CHAT_ID,
                                    text=msg
                                )

                                live_sent.add(over_key)

                    # =====================================================
                    # UNDER 1.5
                    # =====================================================
                    under_key = f"UNDER15_{fixture}"

                    if under_key not in live_sent:

                        if (
                            minute >= 25
                            and total_attacks <= 12
                            and total_shots <= 1
                            and pressure <= 0.30
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

🎯 UNDER 1.5 GOALS

📊 Attacks: {total_attacks}
📊 Shots: {total_shots}

📈 Odd: {odd}
"""

                                await bot.send_message(
                                    chat_id=CHAT_ID,
                                    text=msg
                                )

                                live_sent.add(under_key)

                except Exception as e:
                    print("LIVE MATCH ERROR:", e)

        except Exception as e:
            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# MAIN
# =========================================================
async def run_live():
    await live_loop()

def main():

    print("🚀 SYSTEM RUNNING")

    updater = Updater(BOT_TOKEN, use_context=True)

    dp = updater.dispatcher

    dp.add_handler(CommandHandler("today", today))
    dp.add_handler(CommandHandler("night", night))

    updater.start_polling()

    loop = asyncio.get_event_loop()
    loop.create_task(run_live())

    updater.idle()

if __name__ == "__main__":
    main()
