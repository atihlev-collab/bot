# SPORT-ONLY MAIN — 13:10 BG
# Football is NOT started here.
# Sport scanner runs once daily, strictly 13:10-13:15 BG.
# Fixture window remains controlled inside the sport scanner:
# 12:00 BG today -> 12:00 BG next day.

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# IMPORTANT:
# The deployed scanner must be named sport_top3.py.
# This avoids the previous ModuleNotFoundError for sport_top3_V6_FINAL.
import sport_top3

from config import BOT_TOKEN, CHAT_ID

TZ = ZoneInfo("Europe/Sofia")

# STRICT DAILY LAUNCH: 11:40 -> 11:45 BG ONLY.
SPORT_START_MINUTES = 13 * 60 + 10
SPORT_LAUNCH_END_MINUTES = 13 * 60 + 15

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


def _run_sport(day):
    global SPORT_RUNNING, SPORT_DONE_DAY

    try:
        print(f"SPORT THREAD START | day={day}", flush=True)

        result = sport_top3.run_sport_top3_daily_scanner(send_telegram)

        if result:
            SPORT_DONE_DAY = day
            print(f"SPORT THREAD SUCCESS | day={day}", flush=True)
        else:
            print(f"SPORT THREAD EMPTY | day={day}", flush=True)

    except sport_top3.APIQuotaExceeded as exc:
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
        "SPORT SCHEDULER READY | daily trigger 13:10-13:15 BG ONLY | "
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
        # Never launch before 13:10 and never catch up after 13:15.
        in_launch_window = (
            SPORT_START_MINUTES <= minutes <= SPORT_LAUNCH_END_MINUTES
        )

        if in_launch_window and SPORT_DONE_DAY != day and not SPORT_RUNNING:
            if (
                hasattr(sport_top3, "_quota_locked")
                and sport_top3._quota_locked("sport")
            ):
                print(f"SPORT QUOTA ALREADY LOCKED | day={day}", flush=True)
                SPORT_DONE_DAY = day
            else:
                SPORT_RUNNING = True
                print(
                    f"SPORT TRIGGER | day={day} | launch=13:10-13:15 BG",
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
