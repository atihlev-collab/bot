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

# =========================================================
# CONFIG
# =========================================================
HEADERS = {
    "x-apisports-key": API_KEY
}

TZ = ZoneInfo("Europe/Sofia")

LIVE_INTERVAL = 60

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN)

live_sent = set()

# =========================================================
# BLOCKED
# =========================================================
BLOCKED_WORDS = [
    "russia",
    "russian",
    "belarus",
    "belarusian"
]

# =========================================================
# HELPERS
# =========================================================
def blocked(country, league):

    text = f"{country} {league}".lower()

    return any(word in text for word in BLOCKED_WORDS)

# =========================================================
# GET STAT
# =========================================================
def get_stat(stats, name):

    try:

        for s in stats:

            if s["type"] == name:

                value = s["value"]

                if value is None:
                    return 0

                if isinstance(value, str):
                    value = value.replace("%", "")

                return int(value)

    except:
        pass

    return 0

# =========================================================
# GET BEST MATCHES
# =========================================================
def get_best_matches(mode="today"):

    prematch_list = []

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=100",
            headers=HEADERS
        ).json()

        matches = r.get("response", [])

        for m in matches:

            try:

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                # =====================================
                # TODAY
                # =====================================
                if mode == "today":

                    if hour < 8 or hour > 23:
                        continue

                # =====================================
                # NIGHT
                # =====================================
                if mode == "night":

                    if hour >= 8 and hour <= 23:
                        continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                country = m["league"]["country"]
                league_name = m["league"]["name"]

                # =====================================
                # BLOCKED
                # =====================================
                if blocked(country, league_name):
                    continue

                # =====================================
                # BASIC SCORE
                # =====================================
                score = 1

                # =====================================
                # ODDS
                # =====================================
                odd = 1.50
                market = "OVER 1.5 GOALS"

                fixture = m["fixture"]["id"]

                try:

                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fixture}",
                        headers=HEADERS
                    ).json()

                    odds_response = od.get("response", [])

                    if odds_response:

                        odds_map = {}

                        for b in odds_response[0]["bookmakers"]:

                            for bet in b["bets"]:

                                for v in bet["values"]:

                                    odds_map[v["value"]] = float(v["odd"])

                        if odds_map.get("Over 2.5"):

                            odd = odds_map.get("Over 2.5")

                            if odd >= 1.60:
                                market = "OVER 2.5 GOALS"

                        elif odds_map.get("Over 1.5"):

                            odd = odds_map.get("Over 1.5")
                            market = "OVER 1.5 GOALS"

                except:
                    pass

                prematch_list.append({
                    "home": home,
                    "away": away,
                    "country": country,
                    "league": league_name,
                    "time": date.strftime("%H:%M"),
                    "market": market,
                    "odd": odd,
                    "score": score
                })

            except Exception as e:
                print("MATCH ERROR:", e)

        # =====================================
        # SORT
        # =====================================
        prematch_list = sorted(
            prematch_list,
            key=lambda x: x["odd"],
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

📈 Odd: {game['odd']}
"""

    update.message.reply_text(msg)

# =========================================================
# LIVE LOOP
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

                    country = m["league"]["country"]
                    league_name = m["league"]["name"]

                    # =====================================
                    # BLOCKED
                    # =====================================
                    if blocked(country, league_name):
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

                    # =====================================
                    # REAL STATS
                    # =====================================
                    home_attacks = get_stat(h_stats, "Attacks")
                    away_attacks = get_stat(a_stats, "Attacks")

                    home_shots = get_stat(h_stats, "Shots on Goal")
                    away_shots = get_stat(a_stats, "Shots on Goal")

                    total_attacks = home_attacks + away_attacks
                    total_shots = home_shots + away_shots

                    pressure = total_attacks / max(1, minute)

                    # =====================================
                    # OVER 1.5
                    # =====================================
                    over_key = f"OVER15_{fixture}"

                    if over_key not in live_sent:

                        if (
                            total_attacks >= 10
                            and total_shots >= 1
                            and pressure >= 0.25
                        ):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            live_sent.add(over_key)

                    # =====================================
                    # UNDER 1.5
                    # =====================================
                    under_key = f"UNDER15_{fixture}"

                    if under_key not in live_sent:

                        if (
                            minute >= 25
                            and total_attacks <= 8
                            and total_shots == 0
                            and pressure <= 0.20
                        ):

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
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            live_sent.add(under_key)

                    # =====================================
                    # NEXT GOAL HOME
                    # =====================================
                    next_home_key = f"NEXTHOME_{fixture}"

                    if next_home_key not in live_sent:

                        if (
                            home_attacks >= away_attacks + 6
                            and home_shots >= away_shots + 1
                            and home_shots >= 2
                        ):

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 NEXT GOAL HOME

📊 Home attacks: {home_attacks}
📊 Away attacks: {away_attacks}

📊 Home shots: {home_shots}
📊 Away shots: {away_shots}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            live_sent.add(next_home_key)

                    # =====================================
                    # NEXT GOAL AWAY
                    # =====================================
                    next_away_key = f"NEXTAWAY_{fixture}"

                    if next_away_key not in live_sent:

                        if (
                            away_attacks >= home_attacks + 6
                            and away_shots >= home_shots + 1
                            and away_shots >= 2
                        ):

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league_name}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {home_goals}:{away_goals}

🎯 NEXT GOAL AWAY

📊 Home attacks: {home_attacks}
📊 Away attacks: {away_attacks}

📊 Home shots: {home_shots}
📊 Away shots: {away_shots}
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                            live_sent.add(next_away_key)

                except Exception as e:
                    print("LIVE MATCH ERROR:", e)

        except Exception as e:
            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# ASYNC
# =========================================================
async def run_live():
    await live_loop()

# =========================================================
# MAIN
# =========================================================
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
