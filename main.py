import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import threading
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
PREMATCH_INTERVAL = 1800

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN)

live_sent = set()
prematch_sent = set()

# =========================================================
# BLOCKED
# =========================================================
BLOCKED_WORDS = [
    "russia",
    "russian",
    "belarus",
    "belarusian"
]

BAD_LEAGUES = [
    "reserve",
    "reserves",
    "youth",
    "u19",
    "u21",
    "u23",
    "women",
    "friendly"
]

# =========================================================
# BLOCK CHECK
# =========================================================
def blocked(country, league):

    text = f"{country} {league}".lower()

    if any(word in text for word in BLOCKED_WORDS):
        return True

    if any(word in text for word in BAD_LEAGUES):
        return True

    return False

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
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        for m in matches:

            try:

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                hour = date.hour

                if mode == "today":

                    if hour < 8 or hour > 23:
                        continue

                if mode == "night":

                    if hour >= 8 and hour <= 23:
                        continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                country = m["league"]["country"]
                league_name = m["league"]["name"]

                if blocked(country, league_name):
                    continue

                fixture = m["fixture"]["id"]

                odd = 1.50
                market = "OVER 1.5 GOALS"

                # =================================================
                # ODDS
                # =================================================
                try:

                    od = requests.get(
                        f"https://v3.football.api-sports.io/odds?fixture={fixture}",
                        headers=HEADERS,
                        timeout=10
                    ).json()

                    odds_response = od.get("response", [])

                    if odds_response:

                        odds_map = {}

                        for b in odds_response[0]["bookmakers"]:

                            for bet in b["bets"]:

                                for v in bet["values"]:

                                    try:
                                        odds_map[v["value"]] = float(v["odd"])
                                    except:
                                        pass

                        if odds_map.get("Over 2.5"):

                            odd = odds_map.get("Over 2.5")

                            if odd >= 1.70:
                                market = "OVER 2.5 GOALS"

                        elif odds_map.get("Over 1.5"):

                            odd = odds_map.get("Over 1.5")

                except:
                    pass

                prematch_list.append({
                    "fixture": fixture,
                    "home": home,
                    "away": away,
                    "country": country,
                    "league": league_name,
                    "time": date.strftime("%H:%M"),
                    "market": market,
                    "odd": odd
                })

            except:
                pass

        prematch_list = sorted(
            prematch_list,
            key=lambda x: x["odd"],
            reverse=True
        )

        return prematch_list[:3]

    except:
        return []

# =========================================================
# TODAY
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
# NIGHT
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
# PREMATCH LOOP
# =========================================================
async def prematch_loop():

    while True:

        try:

            prematch_sent.clear()

            matches = get_best_matches("today")

            for game in matches:

                fixture = game["fixture"]

                if fixture in prematch_sent:
                    continue

                msg = f"""
📈 PREMATCH SIGNAL

🌍 {game['country']}
🏆 {game['league']}

🏟 {game['home']} vs {game['away']}
⏰ {game['time']}

🎯 {game['market']}
📈 Odd: {game['odd']}
"""

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg
                )

                prematch_sent.add(fixture)

        except Exception as e:
            print("PREMATCH ERROR:", e)

        await asyncio.sleep(PREMATCH_INTERVAL)

# =========================================================
# LIVE LOOP
# =========================================================
async def live_loop():

    while True:

        try:

            r = requests.get(
                "https://v3.football.api-sports.io/fixtures?live=all",
                headers=HEADERS,
                timeout=20
            ).json()

            matches = r.get("response", [])

            for m in matches:

                try:

                    fixture = m["fixture"]["id"]

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    country = m["league"]["country"]
                    league_name = m["league"]["name"]

                    if blocked(country, league_name):
                        continue

                    minute = m["fixture"]["status"]["elapsed"] or 0

                    if minute < 20 or minute > 75:
                        continue

                    home_goals = m["goals"]["home"] or 0
                    away_goals = m["goals"]["away"] or 0

                    total_goals = home_goals + away_goals

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture}",
                        headers=HEADERS,
                        timeout=10
                    ).json()

                    stats = sr.get("response", [])

                    if len(stats) < 2:
                        continue

                    h_stats = stats[0]["statistics"]
                    a_stats = stats[1]["statistics"]

                    home_attacks = get_stat(h_stats, "Attacks")
                    away_attacks = get_stat(a_stats, "Attacks")

                    home_shots = get_stat(h_stats, "Shots on Goal")
                    away_shots = get_stat(a_stats, "Shots on Goal")

                    total_attacks = home_attacks + away_attacks
                    total_shots = home_shots + away_shots

                    pressure = total_attacks / max(1, minute)

                    # =================================================
                    # OVER 1.5
                    # =================================================
                    over_key = f"OVER15_{fixture}"

                    if over_key not in live_sent:

                        if (
                            minute >= 20
                            and minute <= 72
                            and total_attacks >= 7
                            and pressure >= 0.16
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

                    # =================================================
                    # UNDER 1.5
                    # =================================================
                    under_key = f"UNDER15_{fixture}"

                    if under_key not in live_sent:

                        if (
                            minute >= 45
                            and minute <= 65
                            and total_goals == 0
                            and total_attacks <= 3
                            and total_shots == 0
                            and pressure <= 0.08
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

                    # =================================================
                    # NEXT GOAL HOME
                    # =================================================
                    next_home_key = f"NEXTHOME_{fixture}"

                    if next_home_key not in live_sent:

                        if (
                            minute >= 20
                            and minute <= 75
                            and home_attacks >= away_attacks + 3
                            and home_shots >= away_shots
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

                    # =================================================
                    # NEXT GOAL AWAY
                    # =================================================
                    next_away_key = f"NEXTAWAY_{fixture}"

                    if next_away_key not in live_sent:

                        if (
                            minute >= 20
                            and minute <= 75
                            and away_attacks >= home_attacks + 3
                            and away_shots >= home_shots
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
# THREADS
# =========================================================
def start_live_loop():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(live_loop())

def start_prematch_loop():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(prematch_loop())

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

    live_thread = threading.Thread(
        target=start_live_loop
    )

    prematch_thread = threading.Thread(
        target=start_prematch_loop
    )

    live_thread.start()
    prematch_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
