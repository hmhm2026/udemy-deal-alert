#!/usr/bin/env python3
"""Udemy Deal Alert - Phase 1.

Fetches public Udemy coupon feeds, filters by your categories/discount,
and sends new matches to Telegram. Uses only the Python standard library.

Secrets are read from environment variables (GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Usage:
  python check_deals.py          # normal run
  python check_deals.py --test   # send a test Telegram message only
  python check_deals.py --dry    # fetch + filter, print results, no Telegram, no seen.json update
  python check_deals.py --raw    # like --dry but also prints every fetched coupon (for debugging sources)
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UDEMY_RE = re.compile(r"https?://(?:www\.)?udemy\.com/course/[A-Za-z0-9_\-]+/?\?[^\s\"'<>]*couponCode=[A-Za-z0-9_.\-]+", re.I)
UDEMY_ENC_RE = re.compile(r"https?%3A%2F%2F(?:www\.)?udemy\.com%2Fcourse%2F[^\s\"'<>&]+", re.I)


# ---------- helpers ----------
def http_request(url, headers=None, data=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}") from None


def get_json(url, headers=None, data=None):
    body = http_request(url, headers=headers, data=data)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError(f"not JSON from {urllib.parse.urlparse(url).netloc}; starts with: {body[:120]!r}") from None


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def find_udemy_url(text):
    """Return a clean udemy.com course URL with couponCode found inside text, or ''."""
    if not text:
        return ""
    text = html.unescape(str(text))
    m = UDEMY_RE.search(text)
    if m:
        return m.group(0)
    m = UDEMY_ENC_RE.search(text)  # affiliate wrapper such as linksynergy ...&murl=<encoded udemy url>
    if m:
        decoded = urllib.parse.unquote(m.group(0))
        if "couponCode=" in decoded:
            return decoded
    return ""


def coupon_key(url):
    """Stable id: course slug + coupon code."""
    p = urllib.parse.urlparse(url)
    slug = p.path.strip("/").split("/")[-1].lower()
    code = urllib.parse.parse_qs(p.query).get("couponCode", [""])[0]
    return f"{slug}|{code}"


def discount_from_text(text):
    m = re.search(r"(\d{2,3})\s*%\s*(?:off|discount)", text or "", re.I)
    if m:
        return min(int(m.group(1)), 100)
    if re.search(r"\bfree\b|\b100%", text or "", re.I):
        return 100
    return None


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", html.unescape(s or "")).strip()


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def deal(title, url, source, discount=100, categories=None, text="", rating=None):
    return {"title": html.unescape(title or "").strip(), "url": url, "discount": discount,
            "categories": [c for c in (categories or []) if c], "text": (text or "")[:500],
            "rating": rating, "source": source}


# ---------- sources (each returns a list of deal dicts) ----------
def source_realdiscount():
    """real.discount public JSON (free coupons only)."""
    data = get_json(
        "https://cdn.real.discount/api/courses?page=1&limit=100&sortBy=sale_start&store=Udemy&freeOnly=true",
        headers={"Referer": "https://www.real.discount/", "Origin": "https://www.real.discount"})
    items = data.get("items") or data.get("results") or data.get("data") or []
    out = []
    for it in items:
        if str(it.get("store", "")).lower() == "sponsored":
            continue
        url = find_udemy_url(json.dumps(it, ensure_ascii=False).replace("\\/", "/"))
        if not url:
            continue
        cat = it.get("category") or it.get("subcategory") or ""
        out.append(deal(it.get("name") or it.get("title"), url, "real.discount", 100,
                        [cat] if isinstance(cat, str) else list(cat),
                        strip_tags(it.get("shoer_description") or it.get("description") or ""),
                        to_float(it.get("rating"))))
    return out


def _wp_posts(host, params, source, link_fields=()):
    """Generic WordPress REST feed: title.rendered + a udemy link in content / acf fields."""
    api = f"https://{host}/wp-json/wp/v2/posts?" + urllib.parse.urlencode(params)
    posts = get_json(api, headers={"Referer": f"https://{host}/"})
    out = []
    for p in posts:
        title = strip_tags((p.get("title") or {}).get("rendered", ""))
        content = (p.get("content") or {}).get("rendered", "")
        blob = content + " " + json.dumps(p.get("acf") or {}) + " " + (p.get("link") or "")
        url = find_udemy_url(blob.replace("\\/", "/"))
        if not url:
            continue
        out.append(deal(re.sub(r"\s*[\(\[]?\s*(?:free course|100%\s*off)[\)\]]?\s*$", "", title, flags=re.I),
                        url, source, discount_from_text(title + " " + strip_tags(content)[:300]) or 100,
                        [], strip_tags(content)))
    return out


def source_coursejoiner():
    return _wp_posts("www.coursejoiner.com", {"categories": 74, "per_page": 50}, "coursejoiner")


def source_tutorialbar():
    return _wp_posts("www.tutorialbar.com", {"categories": 55, "per_page": 50}, "tutorialbar")


def source_courson():
    """courson.xyz JSON endpoint used by its 'load more' button."""
    data = get_json("https://courson.xyz/load-more-coupons",
                    headers={"Content-Type": "application/json", "Referer": "https://courson.xyz/coupons",
                             "Origin": "https://courson.xyz"},
                    data=json.dumps({"filters": {}, "offset": 0}).encode())
    out = []
    for c in data.get("coupons", []):
        slug, code = c.get("id_name"), c.get("coupon_code")
        if not slug or not code:
            continue
        cats = [c.get("category"), c.get("subcategory"), c.get("topic")]
        out.append(deal(c.get("headline") or slug, f"https://www.udemy.com/course/{slug}/?couponCode={code}",
                        "courson", 100, [x for x in cats if isinstance(x, str)],
                        c.get("description") or "", to_float(c.get("rating"))))
    return out


SOURCES = {
    "realdiscount": source_realdiscount,
    "coursejoiner": source_coursejoiner,
    "tutorialbar": source_tutorialbar,
    "courson": source_courson,
}


# ---------- filtering ----------
def matches(d, cfg):
    disc = d["discount"]
    if disc is None or disc < cfg.get("min_discount", 100):
        return False
    min_rating = cfg.get("min_rating", 0)
    if min_rating and d["rating"] is not None and d["rating"] < min_rating:
        return False
    hay = " ".join([d["title"], " ".join(d["categories"]), d["text"]]).lower()
    for bad in cfg.get("exclude_keywords", []):
        if bad.lower() in hay:
            return False
    kws = [k.lower() for k in cfg.get("categories", [])]
    if not kws:
        return True
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", hay) for k in kws)


# ---------- telegram ----------
def tg_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.")
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Telegram error HTTP {e.code} (check token / chat id)") from None


def format_deal(d):
    disc = "FREE (100% off)" if d["discount"] == 100 else f"{d['discount']}% off"
    cat = ", ".join(c for c in d["categories"] if c)[:80]
    lines = [f"🎓 <b>{html.escape(d['title'])}</b>", f"💸 {disc}"]
    if cat:
        lines.append(f"🏷 {html.escape(cat)}")
    if d["rating"]:
        lines.append(f"⭐ {d['rating']:.1f}")
    lines.append(f"🔗 {html.escape(d['url'])}")
    lines.append(f"<i>via {d['source']}</i>")
    return "\n".join(lines)


# ---------- main ----------
def main():
    if "--test" in sys.argv:
        tg_send("✅ Udemy Deal Alert is connected and working.")
        print("Test message sent.")
        return

    dry = "--dry" in sys.argv or "--raw" in sys.argv
    raw = "--raw" in sys.argv
    cfg = load_json(CONFIG_FILE, {})
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)

    names = cfg.get("sources", list(SOURCES))
    all_deals, errors = [], []
    for name in names:
        fn = SOURCES.get(name)
        if not fn:
            print(f"[{name}] unknown source, skipped")
            continue
        try:
            got = fn()
            print(f"[{name}] fetched {len(got)} coupons")
            if raw:
                for d in got:
                    print("   *", d["title"][:70], "|", d["categories"], "|", d["url"])
            all_deals.extend(got)
        except Exception as e:  # keep going if one source is down
            print(f"[{name}] ERROR: {e}")
            errors.append(name)

    new, batch_keys = [], set()
    for d in all_deals:
        k = coupon_key(d["url"])
        if k in seen_set or k in batch_keys:
            continue
        if matches(d, cfg):
            new.append(d)
            batch_keys.add(k)
    print(f"{len(new)} new matching deals (out of {len(all_deals)} coupons fetched)")

    if dry:
        for d in new:
            print("-", d["title"], "|", d["discount"], "|", d["categories"], "|", d["url"])
        return

    max_send = cfg.get("max_messages_per_run", 10)
    for d in new[:max_send]:
        tg_send(format_deal(d))
    if len(new) > max_send:
        tg_send(f"… and {len(new) - max_send} more matching deals were found. They will not be repeated.")

    # mark ALL fetched coupons as seen (matching or not) so nothing is repeated later
    seen = list(dict.fromkeys(seen + [coupon_key(d["url"]) for d in all_deals]))[-3000:]
    SEEN_FILE.write_text(json.dumps(seen, indent=0), encoding="utf-8")

    if names and len(errors) == len(names):
        raise SystemExit("All sources failed.")


if __name__ == "__main__":
    main()
