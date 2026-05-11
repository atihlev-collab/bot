import os
os.system("pip install requests python-telegram-bot==13.15")

import asyncio
import logging
import threading
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext
)

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
# TOP LEAGUES
# =========================================================
TOP_LEAGUES = [
    "Premier League",
    "Champions League",
    "Europa League",
    "Conference League",
    "La Liga",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "Eredivisie",
    "Primeira Liga",
    "MLS",
    "Brasileirao",
    "Copa Libertadores"
]

# =========================================================
# STORAGE
# =========================================================
history = {}
sent_signals = set()

# =========================================================
# BLOCK CHECK
# =========================================================
def blocked(country, league):

    text = f"{country} {league}".lower()

    if any(x in text for x in BLOCKED_WORDS):
        return True

    if any(x in text for x in BAD_LEAGUES):
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
# UNIQUE SIGNAL
# =========================================================
def signal_exists(fixture_id, market):

    return f"{fixture_id}_{market}" in sent_signals

def save_signal(fixture_id, market):

    sent_signals.add(
        f"{fixture_id}_{market}"
    )

# =========================================================
# TODAY COMMAND
# =========================================================
def today(update: Update, context: CallbackContext):

    try:

        r = requests.get(
            "https://v3.football.api-sports.io/fixtures?next=150",
            headers=HEADERS,
            timeout=20
        ).json()

        matches = r.get("response", [])

        results = []

        for m in matches:

            try:

                fixture_id = m["fixture"]["id"]

                league = m["league"]["name"]
                country = m["league"]["country"]

                if blocked(country, league):
                    continue

                if not any(
                    x.lower() in league.lower()
                    for x in TOP_LEAGUES
                ):
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]

                date = datetime.fromisoformat(
                    m["fixture"]["date"].replace("Z", "+00:00")
                ).astimezone(TZ)

                # =====================================================
                # ODDS
                # =====================================================
                orr = requests.get(
                    f"https://v3.football.api-sports.io/odds?fixture={fixture_id}",
                    headers=HEADERS,
                    timeout=20
                ).json()

                odds_response = orr.get(
                    "response",
                    []
                )

                # =====================================================
                # FALLBACK IF NO ODDS
                # =====================================================
                if not odds_response:

                    if (
                        "Bundesliga" in league
                        or "Eredivisie" in league
                    ):

                        results.append({
                            "league": league,
                            "country": country,
                            "home": home,
                            "away": away,
                            "time": date.strftime("%H:%M"),
                            "pick": "OVER 2.5 GOALS",
                            "odd": "N/A",
                            "score": 8
                        })

                    elif (
                        "Serie A" in league
                        or "Ligue 1" in league
                    ):

                        results.append({
                            "league": league,
                            "country": country,
                            "home": home,
                            "away": away,
                            "time": date.strftime("%H:%M"),
                            "pick": "UNDER 2.5 GOALS",
                            "odd": "N/A",
                            "score": 7
                        })

                    elif (
                        "Premier League" in league
                    ):

                        results.append({
                            "league": league,
                            "country": country,
                            "home": home,
                            "away": away,
                            "time": date.strftime("%H:%M"),
                            "pick": "GOAL GOAL",
                            "odd": "N/A",
                            "score": 9
                        })

                    continue

                best_pick = None
                best_odd = None
                best_score = 0

                for bookmaker in odds_response:

                    bookmakers = bookmaker.get(
                        "bookmakers",
                        []
                    )

                    for b in bookmakers:

                        markets = b.get(
                            "bets",
                            []
                        )

                        for market in markets:

                            market_name = market.get(
                                "name",
                                ""
                            )

                            values = market.get(
                                "values",
                                []
                            )

                            # =================================================
                            # OVER/UNDER 2.5
                            # =================================================
                            if (
                                "Goals Over/Under" in market_name
                            ):

                                for v in values:

                                    value = v.get(
                                        "value",
                                        ""
                                    )

                                    odd = float(
                                        v.get(
                                            "odd",
                                            0
                                        )
                                    )

                                    if (
                                        value == "Over 2.5"
                                        and 1.70 <= odd <= 2.20
                                    ):

                                        score = 10

                                        if (
                                            "Premier League" in league
                                            or "Bundesliga" in league
                                            or "Champions League" in league
                                        ):
                                            score += 3

                                        if score > best_score:

                                            best_score = score
                                            best_pick = "OVER 2.5 GOALS"
                                            best_odd = odd

                                    if (
                                        value == "Under 2.5"
                                        and 1.60 <= odd <= 2.00
                                    ):

                                        score = 8

                                        if (
                                            "Serie A" in league
                                            or "Ligue 1" in league
                                        ):
                                            score += 3

                                        if score > best_score:

                                            best_score = score
                                            best_pick = "UNDER 2.5 GOALS"
                                            best_odd = odd

                            # =================================================
                            # GOAL GOAL
                            # =================================================
                            if (
                                "Both Teams Score" in market_name
                            ):

                                for v in values:

                                    value = v.get(
                                        "value",
                                        ""
                                    )

                                    odd = float(
                                        v.get(
                                            "odd",
                                            0
                                        )
                                    )

                                    if (
                                        value == "Yes"
                                        and 1.65 <= odd <= 2.10
                                    ):

                                        score = 9

                                        if (
                                            "Bundesliga" in league
                                            or "Eredivisie" in league
                                            or "MLS" in league
                                        ):
                                            score += 3

                                        if score > best_score:

                                            best_score = score
                                            best_pick = "GOAL GOAL"
                                            best_odd = odd

                            # =================================================
                            # MATCH WINNER
                            # =================================================
                            if (
                                "Match Winner" in market_name
                            ):

                                for v in values:

                                    value = v.get(
                                        "value",
                                        ""
                                    )

                                    odd = float(
                                        v.get(
                                            "odd",
                                            0
                                        )
                                    )

                                    if (
                                        value == "Home"
                                        and 1.50 <= odd <= 2.20
                                    ):

                                        score = 7

                                        if (
                                            "Manchester" in home
                                            or "Real Madrid" in home
                                            or "Bayern" in home
                                            or "Arsenal" in home
                                        ):
                                            score += 4

                                        if score > best_score:

                                            best_score = score
                                            best_pick = "1"
                                            best_odd = odd

                                    if (
                                        value == "Away"
                                        and 1.50 <= odd <= 2.20
                                    ):

                                        score = 7

                                        if (
                                            "Manchester" in away
                                            or "Real Madrid" in away
                                            or "Bayern" in away
                                            or "Arsenal" in away
                                        ):
                                            score += 4

                                        if score > best_score:

                                            best_score = score
                                            best_pick = "2"
                                            best_odd = odd

                if best_pick:

                    results.append({
                        "league": league,
                        "country": country,
                        "home": home,
                        "away": away,
                        "time": date.strftime("%H:%M"),
                        "pick": best_pick,
                        "odd": best_odd,
                        "score": best_score
                    })

            except:
                pass

        results = sorted(
            results,
            key=lambda x: x["score"],
            reverse=True
        )

        results = results[:3]

        if not results:

            update.message.reply_text(
                "❌ Няма намерени value мачове."
            )

            return

        msg = "📈 TODAY BEST ODDS\n"

        for g in results:

            msg += f"""

🌍 {g['country']}
🏆 {g['league']}

🏟 {g['home']} vs {g['away']}
⏰ {g['time']}

🎯 {g['pick']}
💰 Odd: {g['odd']}
"""

        update.message.reply_text(msg)

    except Exception as e:

        print("TODAY ERROR:", e)

        update.message.reply_text(
            "❌ Грешка при today."
        )

# =========================================================
# NIGHT COMMAND
# =========================================================
def night(update: Update, context: CallbackContext):

    update.message.reply_text(
        "🌙 NIGHT command active."
    )

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

                    fixture = str(
                        m["fixture"]["id"]
                    )

                    status = m["fixture"]["status"]["short"]

                    if status in ["FT", "AET", "PEN"]:

                        history.pop(fixture, None)

                        continue

                    minute = (
                        m["fixture"]["status"]["elapsed"]
                        or 0
                    )

                    if minute < 20 or minute > 75:
                        continue

                    country = m["league"]["country"]
                    league = m["league"]["name"]

                    if blocked(country, league):
                        continue

                    home = m["teams"]["home"]["name"]
                    away = m["teams"]["away"]["name"]

                    gh = m["goals"]["home"] or 0
                    ga = m["goals"]["away"] or 0

                    goals = gh + ga

                    sr = requests.get(
                        f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture}",
                        headers=HEADERS,
                        timeout=10
                    ).json()

                    stats = sr.get("response", [])

                    if len(stats) < 2:
                        continue

                    hs = stats[0]["statistics"]
                    as_ = stats[1]["statistics"]

                    ha = get_stat(hs, "Attacks")
                    aa = get_stat(as_, "Attacks")

                    hsh = get_stat(
                        hs,
                        "Shots on Goal"
                    )

                    ash = get_stat(
                        as_,
                        "Shots on Goal"
                    )

                    if fixture not in history:

                        history[fixture] = []

                    last_minute = None

                    if len(history[fixture]) > 0:
                        last_minute = history[fixture][-1]["minute"]

                    if last_minute != minute:

                        history[fixture].append({
                            "minute": minute,
                            "ha": ha,
                            "aa": aa,
                            "hsh": hsh,
                            "ash": ash
                        })

                    history[fixture] = history[fixture][-25:]

                    hist = history[fixture]

                    # =================================================
                    # OVER 2.5
                    # =================================================
                    if not signal_exists(fixture, "OVER25"):

                        over_ticks = 0

                        for h in hist:

                            if (
                                h["hsh"] >= 2
                                and h["ash"] >= 2
                                and (
                                    h["ha"] + h["aa"]
                                ) >= 15
                            ):
                                over_ticks += 1

                        if (
                            goals <= 2
                            and over_ticks >= 6
                        ):

                            save_signal(
                                fixture,
                                "OVER25"
                            )

                            msg = f"""
🔥 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 OVER 2.5 GOALS
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                    # =================================================
                    # NEXT GOAL HOME
                    # =================================================
                    if not signal_exists(fixture, "NEXTHOME"):

                        home_ticks = 0

                        for h in hist:

                            if (
                                h["ha"] >= h["aa"] + 3
                                and h["hsh"] >= 1
                            ):
                                home_ticks += 1

                        if home_ticks >= 5:

                            save_signal(
                                fixture,
                                "NEXTHOME"
                            )

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 NEXT GOAL HOME
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                    # =================================================
                    # NEXT GOAL AWAY
                    # =================================================
                    if not signal_exists(fixture, "NEXTAWAY"):

                        away_ticks = 0

                        for h in hist:

                            if (
                                h["aa"] >= h["ha"] + 3
                                and h["ash"] >= 1
                            ):
                                away_ticks += 1

                        if away_ticks >= 5:

                            save_signal(
                                fixture,
                                "NEXTAWAY"
                            )

                            msg = f"""
🚨 LIVE SIGNAL

🌍 {country}
🏆 {league}

🏟 {home} vs {away}
⏱ {minute}'
⚽ {gh}:{ga}

🎯 NEXT GOAL AWAY
"""

                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=msg
                            )

                except Exception as e:

                    print("MATCH ERROR:", e)

        except Exception as e:

            print("LIVE ERROR:", e)

        await asyncio.sleep(LIVE_INTERVAL)

# =========================================================
# THREAD
# =========================================================
def start_live_loop():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        live_loop()
    )

# =========================================================
# MAIN
# =========================================================
def main():

    print("🚀 LIVE SYSTEM RUNNING")

    updater = Updater(
        token=BOT_TOKEN,
        use_context=True
    )

    dp = updater.dispatcher

    dp.add_handler(
        CommandHandler("today", today)
    )

    dp.add_handler(
        CommandHandler("night", night)
    )

    print("✅ COMMANDS LOADED")

    updater.start_polling(
        drop_pending_updates=True
    )

    print("✅ POLLING STARTED")

    live_thread = threading.Thread(
        target=start_live_loop,
        daemon=True
    )

    live_thread.start()

    print("✅ LIVE THREAD STARTED")

    updater.idle()

if __name__ == "__main__":
    main()
