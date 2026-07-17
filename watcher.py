#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Beach booking availability watcher (GitHub Actions).

Polls the public availability page and sends notifications (ntfy push +
email via formsubmit.co) when any listed date shows open places.

Env vars:
  NTFY_TOPIC    ntfy.sh topic for push notifications
  ALERT_EMAILS  comma-separated recipient emails (formsubmit.co)
  LOOP_MINUTES  how long this run keeps polling (default 200)
  TEST_ALERT    "true" to send a test notification at startup
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

URL = "https://app.stintinospiagge.it/prenotazioni/1/1?lang=en"
NORMAL_INTERVAL = 30           # seconds
FAST_INTERVAL = 5              # seconds, during the morning release window
FAST_WINDOW_UTC = ((5, 55), (6, 15))   # 08:55-09:15 TR
REALERT_COOLDOWN = 3 * 60 * 60  # per-date re-alert while slots stay open

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ALERT_EMAILS = [e.strip() for e in os.environ.get("ALERT_EMAILS", "").split(",") if e.strip()]
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "200"))
TEST_ALERT = os.environ.get("TEST_ALERT", "").lower() == "true"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

CARD_RE = re.compile(
    r'date-title">\s*\w+\s+(\d{2}/\d{2})\s*</div>.*?class="places[^"]*">\s*(\d+)',
    re.S,
)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}", flush=True)


def fetch_counts():
    result = subprocess.run(
        ["curl", "-s", "--max-time", "25", "-A", USER_AGENT,
         "-H", "Accept-Language: en", URL],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit code {result.returncode}")
    html = result.stdout
    counts = dict((d, int(n)) for d, n in CARD_RE.findall(html))
    if not counts and "date-card" not in html:
        snippet = re.sub(r"\s+", " ", html[:200])
        raise RuntimeError(f"unexpected page format (blocked?): {snippet}")
    return counts


def send_ntfy(title, message, priority="urgent"):
    if not NTFY_TOPIC:
        return
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20",
         "-H", f"Title: {title}", "-H", f"Priority: {priority}", "-H", "Tags: beach_umbrella",
         "-d", message, f"https://ntfy.sh/{NTFY_TOPIC}"],
        capture_output=True, text=True,
    )
    ok = '"event":"message"' in r.stdout
    log(f"ntfy push: {'OK' if ok else 'FAILED ' + r.stdout[:150]}")


def send_email(subject, message):
    payload = json.dumps({"name": "Pelosa Bot", "message": message, "_subject": subject})
    for email in ALERT_EMAILS:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20", "-X", "POST",
             f"https://formsubmit.co/ajax/{email}",
             "-H", "Content-Type: application/json",
             "-H", "Accept: application/json",
             "-H", "Origin: https://pelosa-watch.example.com",
             "-H", "Referer: https://pelosa-watch.example.com/",
             "-d", payload],
            capture_output=True, text=True,
        )
        ok = '"success":"true"' in r.stdout.replace(" ", "")
        log(f"email -> {email[:4]}***: {'OK' if ok else 'FAILED ' + r.stdout[:150]}")


def create_issue(title, body):
    """GitHub issue as a notification channel: GitHub emails repo watchers."""
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return
    payload = json.dumps({"title": title, "body": body})
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20", "-X", "POST",
         f"https://api.github.com/repos/{repo}/issues",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Accept: application/vnd.github+json",
         "-d", payload],
        capture_output=True, text=True,
    )
    ok = '"number"' in r.stdout
    log(f"github issue: {'OK' if ok else 'FAILED ' + r.stdout[:150]}")


def alert(open_dates, counts):
    detail = "  ".join(f"{d}: {n} yer" for d, n in sorted(counts.items()))
    dates_str = ", ".join(sorted(open_dates))
    msg = (f"YER ACILDI! Tarih(ler): {dates_str}. Durum: {detail}. "
           f"HEMEN GIR (60 saniye form suresi var): {URL}")
    send_ntfy("YER ACILDI — La Pelosa", msg)
    send_email("🏖 YER AÇILDI — La Pelosa — HEMEN BAK", msg)
    create_issue(f"🏖 YER AÇILDI: {dates_str}", msg)


def current_interval():
    t = datetime.now(timezone.utc)
    (h1, m1), (h2, m2) = FAST_WINDOW_UTC
    start = t.replace(hour=h1, minute=m1, second=0)
    end = t.replace(hour=h2, minute=m2, second=0)
    return FAST_INTERVAL if start <= t <= end else NORMAL_INTERVAL


def main():
    deadline = time.time() + LOOP_MINUTES * 60
    log(f"Watcher started. Loop budget: {LOOP_MINUTES} min. "
        f"Emails: {len(ALERT_EMAILS)} recipient(s). ntfy: {'yes' if NTFY_TOPIC else 'no'}.")

    if TEST_ALERT:
        log("TEST_ALERT enabled — sending test notifications.")
        send_ntfy("Pelosa Bot Test", "Test bildirimi — bot bulutta calisiyor.", priority="default")
        send_email("Pelosa Bot Test", "Test maili — bot bulutta calisiyor. Bu maili aldiysan e-posta bildirimleri aktif.")

    last_positive = {}     # date -> count of last poll
    last_alert_at = {}     # date -> timestamp of last alert
    consecutive_errors = 0
    error_alerted = False

    while time.time() < deadline:
        try:
            counts = fetch_counts()
            consecutive_errors = 0
            summary = "  ".join(f"{d}: {n}" for d, n in sorted(counts.items()))
            log(summary if summary else "no dates listed on page")

            now = time.time()
            newly_open, still_open = [], []
            for d, n in counts.items():
                if n > 0:
                    if last_positive.get(d, 0) == 0:
                        newly_open.append(d)
                    elif now - last_alert_at.get(d, 0) >= REALERT_COOLDOWN:
                        still_open.append(d)
                elif last_positive.get(d, 0) > 0:
                    log(f"{d} sold out again.")
                last_positive[d] = n

            if newly_open or still_open:
                for d in newly_open + still_open:
                    last_alert_at[d] = now
                log(f">>> ALERT: newly open {newly_open}, re-alert {still_open}")
                alert(newly_open + still_open, counts)

        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR ({consecutive_errors}): {e}")
            if consecutive_errors >= 10 and not error_alerted:
                send_ntfy("Pelosa Bot SORUN", f"Bot 10 kez ust uste hata aldi: {e}", priority="high")
                send_email("⚠️ Pelosa Bot sorunu", f"Bot sayfayi cekemiyor (10 ardisik hata): {e}")
                error_alerted = True

        time.sleep(current_interval())

    log("Loop budget exhausted; exiting (next scheduled run takes over).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
