# SPORT-ONLY MAIN
# Football is NOT started here.
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import sport_top3
from config import BOT_TOKEN, CHAT_ID

TZ = ZoneInfo("Europe/Sofia")
SPORT_START_MINUTES = 10 * 60
SPORT_DONE_DAY = None
SPORT_RUNNING = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", force=True)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=20)
    r.raise_for_status()
    print(f"SPORT TELEGRAM SENT | status={r.status_code}", flush=True)


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
        print(f"SPORT THREAD END | day={day} | running={SPORT_RUNNING} | done={SPORT_DONE_DAY}", flush=True)


def main_loop():
    global SPORT_RUNNING, SPORT_DONE_DAY
    print("SPORT-ONLY SYSTEM START", flush=True)
    print("FOOTBALL DISABLED HERE — football runs in the separate system", flush=True)
    print("SPORT SCHEDULER READY | daily trigger after 10:00 BG | window 12:00 -> next day 12:00", flush=True)

    while True:
        now = datetime.now(TZ)
        day = now.date().isoformat()
        minutes = now.hour * 60 + now.minute

        print(f"SPORT SCHEDULER TICK | {now:%Y-%m-%d %H:%M:%S} | minutes={minutes} | running={SPORT_RUNNING} | done={SPORT_DONE_DAY}", flush=True)

        if minutes >= SPORT_START_MINUTES and SPORT_DONE_DAY != day and not SPORT_RUNNING:
            if hasattr(sport_top3, "_quota_locked") and sport_top3._quota_locked("sport"):
                print(f"SPORT QUOTA ALREADY LOCKED | day={day}", flush=True)
                SPORT_DONE_DAY = day
            else:
                SPORT_RUNNING = True
                print(f"SPORT TRIGGER | day={day}", flush=True)
                threading.Thread(target=_run_sport, args=(day,), daemon=True).start()

        time.sleep(30)


if __name__ == "__main__":
    main_loop()



