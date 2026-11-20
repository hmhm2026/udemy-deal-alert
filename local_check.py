#!/usr/bin/env python3
"""Local Udemy session check (Playwright). Runs on YOUR PC, never on GitHub.

What it does (check only - it NEVER clicks Enroll or Buy):
  1. Reads the alerts the bot already sent (history.json, last 24 h).
  2. Opens each coupon link in a browser that is signed in to your Udemy account.
  3. Reports: FREE (Enroll button shown), ENROLLED, PAID / ENDED, or BLOCKED.
  4. Prints the result and, if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set, sends a summary.

Usage:
  python local_check.py --login     one time: sign in to Udemy in the opened window
  python local_check.py --cdp       use YOUR real Chrome opened with --remote-debugging-port=9222
  python local_check.py             check the recent alerts
  python local_check.py --max 5     check at most 5 links
"""
import argparse, json, os, random, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "udemy_profile"
HISTORY = HERE / "history.json"
HISTORY_URL = "https://raw.githubusercontent.com/hmhm2026/udemy-deal-alert/main/history.json"
CHECKED = HERE / "local_checked.json"
SHOTS = HERE / "local_shots"
MAX_PER_RUN = 10
WINDOW_HOURS = 24

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_history():
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        with urllib.request.urlopen(HISTORY_URL, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("Could not read history.json:", e)
        return []


def load_checked():
    try:
        return json.loads(CHECKED.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_checked(d):
    CHECKED.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_entries(hist):
    now = datetime.now(timezone.utc)
    out = []
    for h in hist:
        url = h.get("url") or ""
        if not url.startswith("https://www.udemy.com/") and not url.startswith("https://udemy.com/"):
            continue
        try:
            at = datetime.fromisoformat(str(h.get("at")).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now - at <= timedelta(hours=WINDOW_HOURS):
            out.append(h)
    out.sort(key=lambda h: str(h.get("at")), reverse=True)
    return out


def classify(title, body, status):
    """Pure text classification so it can be tested without a browser."""
    t = (title or "").lower()
    b = (body or "").lower()
    if status in (403, 429) or "just a moment" in t or "access denied" in t or "captcha" in b[:3000] \
            or "verify you are human" in b:
        return "BLOCKED"
    if status == 404 or "page not found" in t:
        return "ENDED"
    if "go to course" in b and "enroll now" not in b:
        return "ENROLLED"
    if "enroll now" in b and "buy now" not in b and "add to cart" not in b:
        return "FREE"
    if "buy now" in b or "add to cart" in b:
        return "PAID"
    return "UNKNOWN"


ICON = {"FREE": "✅", "ENROLLED": "📚", "PAID": "❌", "ENDED": "❌", "BLOCKED": "⛔", "UNKNOWN": "❓"}
TEXT = {
    "FREE": "still free for your account - open it now",
    "ENROLLED": "already in your account",
    "PAID": "coupon ended / not free",
    "ENDED": "course page not found",
    "BLOCKED": "Udemy asked for a human check - stopped",
    "UNKNOWN": "could not tell (screenshot saved)",
}


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=20)
    except Exception as e:
        print("Telegram send failed:", e)


def connect_cdp(p):
    """Attach to a normal Chrome that you started yourself (port 9222)."""
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    return browser.contexts[0] if browser.contexts else browser.new_context()


def open_context(p, headless=False):
    PROFILE.mkdir(exist_ok=True)
    kw = dict(user_data_dir=str(PROFILE), headless=headless, viewport={"width": 1280, "height": 900})
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **kw)
    except Exception:
        return p.chromium.launch_persistent_context(**kw)


def do_login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = open_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.udemy.com/join/login-popup/")
        input("Sign in to Udemy in the window, then come back here and press Enter... ")
        ctx.close()
    print("Session saved in", PROFILE)


def do_check(limit, cdp=False):
    from playwright.sync_api import sync_playwright
    entries = recent_entries(load_history())
    done = load_checked()
    todo = [h for h in entries if h["url"] not in done][:limit]
    if not todo:
        print("Nothing new to check (no alerts in the last 24 h, or all checked).")
        return
    SHOTS.mkdir(exist_ok=True)
    lines = []
    with sync_playwright() as p:
        try:
            ctx = connect_cdp(p) if cdp else open_context(p)
        except Exception as e:
            print("Could not connect to Chrome on port 9222. Start it with the command in the instructions first.", str(e)[:100])
            return
        page = ctx.new_page() if cdp else (ctx.pages[0] if ctx.pages else ctx.new_page())
        for i, h in enumerate(todo, 1):
            url, title = h["url"], h.get("title") or "Course"
            status = None
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                status = resp.status if resp else None
                page.wait_for_timeout(3500)
                body = page.inner_text("body")
                result = classify(page.title(), body, status)
            except Exception as e:
                print("  error:", str(e)[:120])
                result = "UNKNOWN"
            if result in ("UNKNOWN", "BLOCKED"):
                try:
                    page.screenshot(path=str(SHOTS / f"{int(time.time())}.png"))
                except Exception:
                    pass
            print(f"[{i}/{len(todo)}] {ICON[result]} {result}: {title[:60]}")
            lines.append(f"{ICON[result]} {title[:70]} - {TEXT[result]}\n{url}")
            if result == "BLOCKED":
                print("Stopped: Udemy asked for a human check. Open the window, solve it yourself, then retry later.")
                break
            done[url] = {"result": result, "at": datetime.now(timezone.utc).isoformat()}
            save_checked(done)
            if i < len(todo):
                time.sleep(random.uniform(8, 20))
        if cdp:
            page.close()
        else:
            ctx.close()
    if lines:
        telegram("Udemy session check\n\n" + "\n\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--cdp", action="store_true")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN)
    a = ap.parse_args()
    if a.login:
        do_login()
    else:
        do_check(min(a.max, MAX_PER_RUN), a.cdp)


if __name__ == "__main__":
    main()
