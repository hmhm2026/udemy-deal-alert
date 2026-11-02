#!/usr/bin/env python3
"""Udemy Deal Alert.

Fetches public Udemy coupon feeds and sends every NEW 100%-off course that matches
your keywords to Telegram. Uses only the Python standard library.
Secrets (GitHub Secrets / env vars): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Sources come in two kinds:
  * direct  - the feed already contains the Udemy coupon link (real.discount, courson)
  * lazy    - the feed lists titles only; the coupon link needs one more request, so it is
              fetched ONLY for titles that match your keywords (discudemy, udemyfreebies,
              idownloadcoupon)

Usage:
  python check_deals.py          # normal run
  python check_deals.py --test   # send a test Telegram message only
  python check_deals.py --dry    # fetch + filter, print results, no Telegram, no seen.json update
  python check_deals.py --raw    # like --dry but also prints every fetched item
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UDEMY_RE = re.compile(r"https?://(?:www\.)?udemy\.com/course/[A-Za-z0-9_\-]+/?\?[^\s\"'<>]*couponCode=[A-Za-z0-9_.\-]+", re.I)
UDEMY_ENC_RE = re.compile(r"https?%3A%2F%2F(?:www\.)?udemy\.com%2Fcourse%2F[^\s\"'<>&]+", re.I)
# language tags such as [ES] [DE] [PT] [UA] [PL+] and non-Latin scripts (Arabic is kept)
LANG_TAG_RE = re.compile(r"\[(?:ES|DE|FR|PT|BR|PL|UA|RU|RO|ID|IT|TR|NL|JA|KO|ZH|HI|VI|TH)\+?\]", re.I)
FOREIGN_SCRIPT_RE = re.compile(r"[Ѐ-ӿऀ-ॿ฀-๿぀-ヿ一-鿿가-힯]")


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


def http_html(url, referer=None):
    return http_request(url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                      "Referer": referer or url})


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


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", html.unescape(s or "")).strip()


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def deal(title, url, source, discount=100, categories=None, text="", rating=None, ref="", resolve=None):
    return {"title": html.unescape(title or "").strip(), "url": url, "discount": discount,
            "categories": [c for c in (categories or []) if c], "text": (text or "")[:500],
            "rating": rating, "source": source, "ref": ref, "resolve": resolve}


def lazy_deal(title, ref, source, resolve):
    """Listing item whose Udemy coupon link is fetched later (only if the title matches)."""
    return deal(title, "", source, 100, [], "", None, ref, resolve)


def deal_ref(d):
    return coupon_key(d["url"]) if d["url"] else d["ref"]


class AnchorParser(HTMLParser):
    """Collects <a class="...token..."> anchors with their text and any <img alt>."""

    def __init__(self, class_token):
        super().__init__()
        self.tok, self.items, self._cur = class_token, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and self.tok in (a.get("class") or "").split():
            self._cur = {"href": a.get("href") or "", "text": "", "alt": ""}
        elif tag == "img" and self._cur is not None:
            self._cur["alt"] = a.get("alt") or self._cur["alt"]

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._cur is not None:
            self.items.append(self._cur)
            self._cur = None


def anchors(page_html, class_token):
    p = AnchorParser(class_token)
    p.feed(page_html)
    return p.items


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def redirect_location(url):
    """Return the Location header of a redirect without following it."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        urllib.request.build_opener(_NoRedirect).open(req, timeout=30)
        return ""
    except urllib.error.HTTPError as e:
        return urllib.parse.unquote(e.headers.get("Location") or "")


# ---------- direct sources (feed already has the coupon link) ----------
def source_realdiscount(cfg):
    """real.discount public JSON (free coupons only), several pages."""
    out = []
    for page in range(1, cfg.get("pages", 2) + 1):
        url = ("https://cdn.real.discount/api/courses?page=%d&limit=100&sortBy=sale_start"
               "&store=Udemy&freeOnly=true" % page)
        try:
            data = get_json(url, headers={"Referer": "https://www.real.discount/",
                                          "Origin": "https://www.real.discount"})
        except Exception as e:
            if page == 1:
                raise
            print(f"[realdiscount] page {page} skipped: {e}")
            break
        items = data.get("items") or data.get("results") or data.get("data") or []
        if not items:
            break
        for it in items:
            if str(it.get("store", "")).lower() == "sponsored":
                continue
            link = find_udemy_url(json.dumps(it, ensure_ascii=False).replace("\\/", "/"))
            if not link:
                continue
            cat = it.get("category") or it.get("subcategory") or ""
            out.append(deal(it.get("name") or it.get("title"), link, "real.discount", 100,
                            [cat] if isinstance(cat, str) else list(cat),
                            strip_tags(it.get("shoer_description") or it.get("description") or ""),
                            to_float(it.get("rating"))))
    return out


def source_courson(cfg):
    """courson.xyz JSON endpoint used by its 'load more' button (30 per page)."""
    out = []
    for page in range(1, cfg.get("pages", 2) + 1):
        try:
            data = get_json("https://courson.xyz/load-more-coupons",
                            headers={"Content-Type": "application/json", "Referer": "https://courson.xyz/coupons",
                                     "Origin": "https://courson.xyz"},
                            data=json.dumps({"filters": {}, "offset": (page - 1) * 30}).encode())
        except Exception as e:
            if page == 1:
                raise
            print(f"[courson] page {page} skipped: {e}")
            break
        coupons = data.get("coupons", [])
        if not coupons:
            break
        for c in coupons:
            slug, code = c.get("id_name"), c.get("coupon_code")
            if not slug or not code:
                continue
            cats = [c.get("category"), c.get("subcategory"), c.get("topic")]
            headline = c.get("headline") or ""
            title = headline if 0 < len(headline) <= 100 else slug.replace("-", " ").title()
            out.append(deal(title, f"https://www.udemy.com/course/{slug}/?couponCode={code}",
                            "courson", 100, [x for x in cats if isinstance(x, str)],
                            headline + " " + (c.get("description") or ""), to_float(c.get("rating"))))
    return out


# ---------- lazy sources (titles first, coupon link only for matching titles) ----------
def source_discudemy(cfg):
    out = []
    for page in range(1, cfg.get("pages_html", 3) + 1):
        try:
            page_html = http_html(f"https://www.discudemy.com/all/{page}", "https://www.discudemy.com")
        except Exception:
            if page == 1:
                raise
            break
        found = anchors(page_html, "card-header")
        if not found:
            break
        for it in found:
            gid = it["href"].rstrip("/").split("/")[-1]
            title = html.unescape(it["text"]).strip()
            if gid and title:
                out.append(lazy_deal(title, f"discudemy:{gid}", "discudemy",
                                     lambda g=gid: find_udemy_url(http_html(f"https://www.discudemy.com/go/{g}",
                                                                           "https://www.discudemy.com"))))
    return out


def source_udemyfreebies(cfg):
    out = []

    def resolve(slug):
        req = urllib.request.Request(f"https://www.udemyfreebies.com/out/{slug}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:  # follows the redirect to Udemy
            return find_udemy_url(r.geturl()) or find_udemy_url(r.read().decode("utf-8", "replace"))

    for page in range(1, cfg.get("pages_html", 3) + 1):
        try:
            page_html = http_html(f"https://www.udemyfreebies.com/free-udemy-courses/{page}")
        except Exception:
            if page == 1:
                raise
            break
        found = anchors(page_html, "theme-img")
        if not found:
            break
        for it in found:
            parts = it["href"].split("/")
            slug = parts[4] if len(parts) > 4 else ""
            title = html.unescape(it["alt"] or it["text"]).strip()
            if slug and title:
                out.append(lazy_deal(title, f"udemyfreebies:{slug}", "udemyfreebies", lambda s=slug: resolve(s)))
    return out


def source_idownloadcoupon(cfg):
    out = []
    for page in range(1, cfg.get("pages_html", 3) + 1):
        try:
            posts = get_json("https://idownloadcoupon.com/wp-json/wp/v2/product?"
                             + urllib.parse.urlencode({"product_cat": 15, "per_page": 100, "page": page}))
        except Exception:
            if page == 1:
                raise
            break
        if not posts:
            break
        for p in posts:
            pid = p.get("id")
            title = strip_tags((p.get("title") or {}).get("rendered", ""))
            if pid and title:
                out.append(lazy_deal(title, f"idownloadcoupon:{pid}", "idownloadcoupon",
                                     lambda i=pid: find_udemy_url(redirect_location(f"https://idownloadcoupon.com/udemy/{i}/"))))
    return out


SOURCES = {
    "realdiscount": source_realdiscount,
    "courson": source_courson,
    "discudemy": source_discudemy,
    "udemyfreebies": source_udemyfreebies,
    "idownloadcoupon": source_idownloadcoupon,
}


# ---------- filtering ----------
def haystack(d, cfg):
    slug = urllib.parse.urlparse(d["url"]).path.strip("/").split("/")[-1].replace("-", " ") if d["url"] else ""
    parts = [d["title"], " ".join(d["categories"]), slug]
    if cfg.get("match_description", False):
        parts.append(d["text"])
    return " ".join(parts).lower()


def is_free(d, cfg):
    """Passes the basic rules: discount, rating, exclusions, language."""
    disc = d["discount"]
    if disc is None or disc < cfg.get("min_discount", 100):
        return False
    min_rating = cfg.get("min_rating", 0)
    if min_rating and d["rating"] is not None and d["rating"] < min_rating:
        return False
    hay = haystack(d, cfg)
    if any(bad.lower() in hay for bad in cfg.get("exclude_keywords", [])):
        return False
    if cfg.get("skip_other_languages", True):
        if LANG_TAG_RE.search(d["title"]) or FOREIGN_SCRIPT_RE.search(d["title"]):
            return False
    return True


def is_interest(d, cfg):
    kws = [k.lower() for k in cfg.get("categories", [])]
    hay = haystack(d, cfg)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", hay) for k in kws)


def in_digest_categories(d, cfg):
    allowed = [c.lower() for c in cfg.get("digest_categories", [])]
    if not allowed:
        return True
    return any(c.lower() in allowed for c in d["categories"])


# ---------- telegram ----------
def tg_send(text, silent=False, preview=True):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.")
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false" if preview else "true",
        "disable_notification": "true" if silent else "false",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Telegram error HTTP {e.code} (check token / chat id)") from None
    time.sleep(0.5)  # stay well under Telegram rate limits


def format_deal(d):
    cat = ", ".join(c for c in d["categories"] if c)[:80]
    lines = [f"🎓 <b>{html.escape(d['title'])}</b>", "💸 FREE (100% off)"]
    if cat:
        lines.append(f"🏷 {html.escape(cat)}")
    if d["rating"]:
        lines.append(f"⭐ {d['rating']:.1f}")
    lines.append(f"🔗 {html.escape(d['url'])}")
    lines.append(f"<i>via {d['source']}</i>")
    return "\n".join(lines)


def format_digest(items, start, total, extra_dropped):
    lines = [f"📚 <b>More free courses</b> ({start + 1}-{start + len(items)} of {total})", ""]
    for i, d in enumerate(items, start + 1):
        cat = d["categories"][0] if d["categories"] else ""
        title = html.escape(d["title"][:90])
        href = html.escape(d["url"], quote=True)
        lines.append(f'{i}. <a href="{href}">{title}</a>' + (f" · {html.escape(cat)}" if cat else ""))
    if extra_dropped:
        lines += ["", f"<i>+{extra_dropped} more not shown (limit per run)</i>"]
    return "\n".join(lines)


# ---------- main ----------
def main():
    if "--test" in sys.argv:
        tg_send("✅ Udemy Deal Alert is connected and working.")
        print("Test message sent.")
        return

    raw = "--raw" in sys.argv
    dry = "--dry" in sys.argv or raw
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
            got = fn(cfg)
            print(f"[{name}] fetched {len(got)} items")
            if raw:
                for d in got:
                    print("   *", d["title"][:70], "|", d["categories"], "|", d["url"] or d["ref"])
            all_deals.extend(got)
        except Exception as e:  # keep going if one source is down
            print(f"[{name}] ERROR: {e}")
            errors.append(name)

    # new, unique, passing the basic rules. Lazy items are resolved only when their title matches.
    new, batch_keys, newly_seen = [], set(), set()
    resolves, fails = 0, {}
    max_res = cfg.get("max_resolves_per_run", 25)
    for d in all_deals:
        ref = deal_ref(d)
        if ref in seen_set or ref in batch_keys:
            continue
        batch_keys.add(ref)
        newly_seen.add(ref)
        if d["url"]:                      # direct source
            if is_free(d, cfg):
                new.append(d)
            continue
        # lazy source
        if not (is_free(d, cfg) and is_interest(d, cfg)):
            continue
        if resolves >= max_res or fails.get(d["source"], 0) >= 3:
            newly_seen.discard(ref)       # try again on the next run
            continue
        resolves += 1
        try:
            url = d["resolve"]()
        except Exception as e:
            fails[d["source"]] = fails.get(d["source"], 0) + 1
            print(f"[{d['source']}] could not resolve '{d['title'][:50]}': {e}")
            newly_seen.discard(ref)
            continue
        time.sleep(0.3)
        if not url:
            continue
        key = coupon_key(url)
        newly_seen.add(key)
        if key in seen_set or key in batch_keys:
            continue
        batch_keys.add(key)
        d["url"] = url
        if is_free(d, cfg):
            new.append(d)

    loud = [d for d in new if is_interest(d, cfg)]
    others = [d for d in new if not is_interest(d, cfg)]
    max_loud = cfg.get("max_messages_per_run", 10)
    overflow = loud[max_loud:]
    loud = loud[:max_loud]

    digest = []
    if cfg.get("notify_all_free", True):
        digest = [d for d in overflow + others if in_digest_categories(d, cfg)]
    max_digest = cfg.get("max_digest_per_run", 40)
    dropped = max(0, len(digest) - max_digest)
    digest = digest[:max_digest]
    print(f"{len(new)} new free coupons -> {len(loud)} interest alerts + {len(digest)} in digest "
          f"(of {len(all_deals)} items fetched, {resolves} links resolved)")

    if dry:
        for d in loud:
            print("INTEREST:", d["title"], "|", d["source"], "|", d["url"])
        for d in digest:
            print("digest  :", d["title"], "|", d["source"], "|", d["url"])
        return

    for d in loud:
        tg_send(format_deal(d))

    chunk = cfg.get("digest_chunk", 10)
    for i in range(0, len(digest), chunk):
        part = digest[i:i + chunk]
        last = i + chunk >= len(digest)
        tg_send(format_digest(part, i, len(digest), dropped if last else 0), silent=True, preview=False)

    # remember everything we looked at so nothing is repeated later
    seen = list(dict.fromkeys(seen + sorted(newly_seen)))[-8000:]
    SEEN_FILE.write_text(json.dumps(seen, indent=0), encoding="utf-8")

    if names and len(errors) == len(names):
        raise SystemExit("All sources failed.")


if __name__ == "__main__":
    main()
