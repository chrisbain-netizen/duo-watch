#!/usr/bin/env python3
"""
iPhone Duo pickup-availability bot for Apple Stores near a ZIP code.

- Checks Apple's store-pickup availability endpoint for each part number
- Keeps only stores within MAX_MILES of ZIP
- Alerts (console + optional ntfy.sh push) only when a store NEWLY has stock
- Run once (for cron) or with --loop to check every 15 minutes

Requires: Python 3.8+ and `pip install requests`

NOTE: Apple has no public API for this. The endpoint below is the same one
apple.com's own "Check availability" box uses; it is unofficial and could
change without notice.
"""

import argparse
import json
import smtplib
from email.message import EmailMessage
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ----------------------------- CONFIG ---------------------------------------
ZIP_CODE = "30028"
MAX_MILES = 45

# Apple US part number to watch (verified against apple.com).
# Other configs, if you ever want them: 256GB Star White MK1E4LL/A,
# 512GB Night Sky MK1H4LL/A, 1TB Night Sky MK1L4LL/A, 2TB Night Sky MK1R4LL/A.
PART_NUMBERS = {
    "MK1F4LL/A": "iPhone Duo 256GB Night Sky",
}

# Push notifications: pick any private topic name, install the free ntfy app,
# subscribe to the same topic. Leave empty to disable.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Email alerts. Sending needs an SMTP login: for Gmail, turn on 2-step verification,
# create an App Password (myaccount.google.com/apppasswords), then set:
#   export SMTP_USER="you@gmail.com"   export SMTP_PASSWORD="your-16-char-app-password"
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# Send a "bot is failing" alert after this many failed checks in a row
# (3 checks is roughly 45 minutes at a 15-minute schedule).
FAILURE_ALERT_AFTER = 3

APPLE_BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"

CHECK_INTERVAL_SECONDS = 15 * 60  # every 15 minutes
STATE_FILE = Path(__file__).with_name("iphone_duo_state.json")
# -----------------------------------------------------------------------------

URL = "https://www.apple.com/shop/retail/pickup-message"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def parse_miles(store):
    """Apple returns distance as e.g. '12.3 mi' (storeDistanceWithUnit) or a number."""
    raw = store.get("storeDistanceWithUnit") or store.get("storedistance")
    if raw is None:
        return None
    try:
        return float(str(raw).lower().replace("mi", "").replace("miles", "").strip())
    except ValueError:
        return None


def fetch_stores(part):
    params = {
        "pl": "true",
        "mts.0": "regular",
        "mts.1": "compact",
        "parts.0": part,
        "location": ZIP_CODE,
    }
    r = requests.get(URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["body"].get("stores", [])


def check_once():
    """Return (in_stock, errors).

    in_stock: {store_key: {...}} for every in-range store with stock right now.
    errors:   list of problems; non-empty means this check could not be trusted.
    """
    in_stock = {}
    errors = []
    for part, label in PART_NUMBERS.items():
        try:
            stores = fetch_stores(part)
        except Exception as e:  # network, blocked by Apple, JSON shape change, etc.
            msg = f"{label}: {type(e).__name__}: {e}"
            log(f"ERROR checking {part}: {e}")
            errors.append(msg)
            continue

        if not stores:
            msg = f"{label}: Apple returned no stores (bad part number or API changed?)"
            log(msg)
            errors.append(msg)
            continue

        for s in stores:
            miles = parse_miles(s)
            if miles is None or miles > MAX_MILES:
                continue
            avail = s.get("partsAvailability", {}).get(part, {})
            if avail.get("pickupDisplay") == "available":
                key = f"{s.get('storeNumber')}|{part}"
                in_stock[key] = {
                    "store": s.get("storeName"),
                    "city": s.get("city"),
                    "state": s.get("state"),
                    "miles": miles,
                    "part": part,
                    "label": label,
                    "pickup": avail.get("pickupSearchQuote", "Available"),
                }
    return in_stock, errors


def send_email(title, body, link=True):
    if not (SMTP_USER and SMTP_PASSWORD and EMAIL_TO):
        return  # email is optional; set SMTP_USER, SMTP_PASSWORD and EMAIL_TO to enable
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    footer = "\n\nBuy/reserve: https://www.apple.com/shop/buy-iphone/iphone-duo" if link else ""
    msg.set_content(body + footer)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        log(f"Email sent to {EMAIL_TO}")
    except Exception as e:
        log(f"Email failed: {e}")


def notify(title, body, priority="urgent", click=APPLE_BUY_URL):
    log(f"ALERT: {title}\n{body}")
    send_email(title, body, link=bool(click))
    if NTFY_TOPIC:
        headers = {"Title": title, "Priority": priority}
        if click:
            headers["Click"] = click
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            log(f"ntfy failed: {e}")


def load_state():
    """State: stores already alerted on, plus a consecutive-failure counter."""
    state = {"in_stock": [], "failures": 0, "failure_alerted": False}
    try:
        raw = json.loads(STATE_FILE.read_text())
        if isinstance(raw, list):  # older state-file format
            state["in_stock"] = raw
        elif isinstance(raw, dict):
            state.update(raw)
    except Exception:
        pass
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def run():
    state = load_state()
    current, errors = check_once()

    if errors:
        # Keep the old in_stock list so we don't re-alert when things recover.
        state["failures"] += 1
        log(f"Check failed ({state['failures']} in a row).")
        if state["failures"] >= FAILURE_ALERT_AFTER and not state["failure_alerted"]:
            notify(
                "iPhone Duo bot is having problems",
                f"{state['failures']} checks in a row failed, so you may miss restocks.\n"
                + "\n".join(errors[:3]),
                priority="high",
                click=None,
            )
            state["failure_alerted"] = True
        save_state(state)
        return

    # Healthy check
    if state["failure_alerted"]:
        notify(
            "iPhone Duo bot is working again",
            "Checks are succeeding again.",
            priority="default",
            click=None,
        )
    state["failures"] = 0
    state["failure_alerted"] = False

    new_keys = set(current) - set(state["in_stock"])
    if new_keys:
        lines = [
            f"- {current[k]['label']} at {current[k]['store']} "
            f"({current[k]['city']}, {current[k]['state']}) - "
            f"{current[k]['miles']:.1f} mi - {current[k]['pickup']}"
            for k in sorted(new_keys, key=lambda k: current[k]["miles"])
        ]
        notify("iPhone Duo available for pickup!", "\n".join(lines))
    else:
        log(
            f"Checked {len(PART_NUMBERS)} config(s) within {MAX_MILES} mi of {ZIP_CODE}: "
            f"{'still in stock at ' + str(len(current)) + ' store(s)' if current else 'nothing available'}."
        )

    # Saving the current set means a store that sells out and restocks alerts again.
    state["in_stock"] = sorted(current)
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-push", action="store_true", help="send a test notification and exit")
    ap.add_argument("--loop", action="store_true", help="run forever, checking every 15 minutes")
    args = ap.parse_args()

    if args.test_push:
        notify("iPhone Duo bot test", "If you can read this, push alerts work.")
        return

    if not args.loop:
        run()
        return

    log(f"Watching {len(PART_NUMBERS)} config(s), {MAX_MILES} mi of {ZIP_CODE}, every 15 min. Ctrl+C to stop.")
    while True:
        try:
            run()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            log(f"Unexpected error: {e}")
        # small jitter so you're not hitting Apple on the exact same second each time
        time.sleep(CHECK_INTERVAL_SECONDS + random.randint(0, 60))


if __name__ == "__main__":
    main()
