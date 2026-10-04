# SPORT-ONLY MAIN
# Football is handled by the separate football system.
# This process runs ONLY sport_top3.py.

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

# The scheduler keeps these states separately so a failed scanner run
# can be retried instead of being permanently marked as completed.
SPORT_RUN_KEY = None
SPORT_RUNNING = False


def send_telegram(message):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=20,
        )
        if response.status_code == 200:
            return True

        logging.warning(
            "TELEGRAM ERROR HTTP %s | %s",
            response.status_code,
            response.text[:300],
        )
        return False

    except Exception as exc:
        logging.warning("TELEGRAM ERROR: %r", exc)
        return False


def _run_sport_background(day):
    global SPORT_RUNNING, SPORT_RUN_KEY

    try:
        print(
            "SPORT THREAD START | sport_top3.run_sport_top3_daily_scanner()",
            flush=True,
        )

        result = sport_top3.run_sport_top3_daily_scanner(send_telegram)

        # run_sport_top3_daily_scanner() marks its own DB run only after
        # the complete report has been built and sent. Only then mark the
        # scheduler day as completed in memory.
        SPORT_RUN_KEY = day

        print(
            f"SPORT THREAD RETURN | success=True | result={'message' if result else 'empty/already-ran'} | day={day}",
            flush=True,
        )

    except sport_top3.APIQuotaExceeded as exc:
        # A 429 is a deliberate daily stop. Do not retry every 30 seconds.
        logging.warning("SPORT DAILY QUOTA STOPPED: %s", exc)
        SPORT_RUN_KEY = day

    except Exception as exc:
        # Do NOT mark the day as completed. The next scheduler tick can retry.
        logging.exception("SPORT DAILY BACKGROUND ERROR: %s", exc)
        SPORT_RUN_KEY = None

    finally:
        SPORT_RUNNING = False
        print(
            f"SPORT THREAD END | running={SPORT_RUNNING} | done={SPORT_RUN_KEY}",
            flush=True,
        )


def main_loop():
    global SPORT_RUNNING, SPORT_RUN_KEY

    logging.info("SPORT-ONLY SYSTEM START")
    logging.info("FOOTBALL DISABLED HERE — football runs in the separate system")
    logging.info(
        "SPORT DAILY | after 10:00 BG | fixture window 12:00 -> next day 12:00"
    )

    while True:
        now = datetime.now(TZ)
        day = now.date().isoformat()
        minutes = now.hour * 60 + now.minute

        print(
            f"SPORT SCHEDULER TICK | {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"| minutes={minutes} | running={SPORT_RUNNING} | done={SPORT_RUN_KEY}",
            flush=True,
        )

        try:
            # Run once per Bulgaria calendar day, starting at/after 10:00 BG.
            if (
                minutes >= SPORT_START_MINUTES
                and SPORT_RUN_KEY != day
                and not SPORT_RUNNING
            ):
                print(f"SPORT TRIGGER | day={day}", flush=True)

                # sport_top3.py owns the Sport API quota lock.
                if hasattr(sport_top3, "_quota_locked") and sport_top3._quota_locked("sport"):
                    logging.warning(
                        "SPORT DAILY: quota locked for %s; no more Sport API requests today",
                        day,
                    )
                    SPORT_RUN_KEY = day

                else:
                    SPORT_RUNNING = True
                    thread = threading.Thread(
                        target=_run_sport_background,
                        args=(day,),
                        name="sport-daily-scanner",
                        daemon=True,
                    )
                    thread.start()
                    print(
                        f"SPORT THREAD STARTED | alive={thread.is_alive()} | day={day}",
                        flush=True,
                    )

        except Exception as exc:
            logging.exception("SPORT SCHEDULER ERROR: %s", exc)
            SPORT_RUNNING = False
            # Leave SPORT_RUN_KEY unchanged so a real scheduler error
            # does not silently consume the daily run.

        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        main_loop()
    except KeyboardInterrupt:
        print("SPORT SYSTEM STOPPED", flush=True)
    except Exception as exc:
        logging.exception("FATAL SPORT MAIN ERROR: %r", exc)
