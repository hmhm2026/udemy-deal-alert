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
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
STATE_FILE = ROOT / "state.json"   # source health + weekly stats (committed by the workflow)
HISTORY_FILE = ROOT / "history.json"  # recent alerts, shown on the GitHub Pages page
TRACKED_FILE = ROOT / "tracked.json"  # coupons retained for periodic validity checks
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


def _zero_price_in_json(value):
    """Look for an explicit zero price in Udemy page JSON, not generic mentions of 'free'."""
    if isinstance(value, dict):
        for key in ("price_text", "priceText", "discount_price_text", "discountPriceText"):
            v = value.get(key)
            if isinstance(v, str) and v.strip().lower() in {"free", "$0.00", "0.00", "0"}:
                return True
        # Udemy's page payloads and JSON-LD commonly expose an Offer price or a discount price.
        for key in ("discount_price", "discountPrice", "price", "offers"):
            if key in value:
                v = value[key]
                if key == "offers":
                    if _zero_price_in_json(v):
                        return True
                elif isinstance(v, (int, float)) and v == 0:
                    return True
                elif isinstance(v, str) and v.strip() in {"0", "0.0", "0.00"}:
                    return True
                elif isinstance(v, dict):
                    amount = v.get("amount")
                    if amount is not None and str(amount).strip() in {"0", "0.0", "0.00"}:
                        return True
                    if _zero_price_in_json(v):
                        return True
        return any(_zero_price_in_json(v) for k, v in value.items() if k not in {
            "price", "discount_price", "discountPrice", "offers"})
    if isinstance(value, list):
        return any(_zero_price_in_json(v) for v in value)
    return False


def _positive_price_in_json(value):
    if isinstance(value, dict):
        for key in ("discount_price", "discountPrice", "price", "offers"):
            if key not in value:
                continue
            v = value[key]
            if key == "offers":
                if _positive_price_in_json(v):
                    return True
            elif isinstance(v, (int, float)) and v > 0:
                return True
            elif isinstance(v, str) and re.fullmatch(r"\d+(?:\.\d+)?", v.strip()) and float(v) > 0:
                return True
            elif isinstance(v, dict):
                amount = v.get("amount")
                if amount is not None:
                    try:
                        if float(amount) > 0:
                            return True
                    except (TypeError, ValueError):
                        pass
                if _positive_price_in_json(v):
                    return True
        return any(_positive_price_in_json(v) for k, v in value.items()
                   if k not in {"price", "discount_price", "discountPrice", "offers"})
    if isinstance(value, list):
        return any(_positive_price_in_json(v) for v in value)
    return False


_PAGE_BLOCKED = False      # set when Udemy refuses the course page (HTTP 403), so we stop asking this run
_API_DEAD = False          # set when the price API fails with a network/HTTP error


def _udemy_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def udemy_price_check(url, course_id=None):
    """Ask Udemy's public price API. Returns ACTIVE_FREE, NOT_FREE or None (could not tell)."""
    global _API_DEAD
    if _API_DEAD:
        return None
    parsed = urllib.parse.urlparse(url)
    code = urllib.parse.parse_qs(parsed.query).get("couponCode", [""])[0]
    slug = parsed.path.strip("/").split("/")[-1]
    try:
        if not course_id:
            info = _udemy_json(f"https://www.udemy.com/api-2.0/courses/{urllib.parse.quote(slug)}/?fields[course]=id")
            course_id = info.get("id")
        if not course_id:
            return None
        params = {"course_ids": course_id, "fields[pricing_result]": "price,discount_price"}
        if code:
            params["couponCode"] = code
        q = urllib.parse.urlencode(params)
        data = _udemy_json(f"https://www.udemy.com/api-2.0/pricing/?{q}")
        res = (data.get("courses") or {}).get(str(course_id)) or {}
        disc = res.get("discount_price")
        if isinstance(disc, dict) and disc.get("amount") is not None:
            return "ACTIVE_FREE" if float(disc["amount"]) == 0 else "NOT_FREE"
        price = res.get("price")
        if isinstance(price, dict) and price.get("amount") is not None and float(price["amount"]) == 0:
            return "ACTIVE_FREE"          # the course itself costs nothing
        if isinstance(price, dict) and price.get("amount") is not None and float(price["amount"]) > 0:
            return "NOT_FREE"             # price shown, no coupon discount applied: coupon is dead
        return None
    except urllib.error.HTTPError as exc:
        print(f"[verify] price API refused: HTTP {exc.code}")
        _API_DEAD = exc.code in (401, 403, 429)
        return None
    except Exception as exc:
        print(f"[verify] price API error: {type(exc).__name__}")
        return None


def verify_udemy_coupon(url, course_id=None):
    """Verify a coupon with Udemy. Returns ACTIVE_FREE, NOT_FREE, or UNVERIFIED."""
    global _PAGE_BLOCKED
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.hostname not in {"udemy.com", "www.udemy.com"} or not query.get("couponCode"):
        return "UNVERIFIED"
    api = udemy_price_check(url, course_id)
    if api:
        return api
    if _PAGE_BLOCKED:
        return "UNVERIFIED"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=25) as response:
            final_url = response.geturl()
            page = response.read().decode("utf-8", errors="replace")
        final = urllib.parse.urlparse(final_url)
        if final.hostname not in {"udemy.com", "www.udemy.com"}:
            return "UNVERIFIED"
    except urllib.error.HTTPError as exc:
        # Keep the coupon URL private (it contains a redeemable coupon code),
        # but expose the HTTP status so blocked/rate-limited requests are diagnosable.
        host = urllib.parse.urlparse(url).netloc or "unknown host"
        print(f"[verify] Udemy page refused: HTTP {exc.code} (not asking again this run)")
        _PAGE_BLOCKED = _PAGE_BLOCKED or exc.code in (401, 403, 429)
        return "UNVERIFIED"
    except urllib.error.URLError as exc:
        host = urllib.parse.urlparse(url).netloc or "unknown host"
        print(f"[verify] connection error ({host}): {exc.reason}")
        return "UNVERIFIED"
    except Exception as exc:
        print(f"[verify] unexpected check error: {type(exc).__name__}: {exc}")
        return "UNVERIFIED"

    lowered = html.unescape(page).lower()
    expired_phrases = (
        "coupon code has expired", "coupon has expired", "coupon code is expired",
        "coupon code is invalid", "coupon is not valid", "coupon code is no longer valid",
        "coupon code has reached its redemption limit", "coupon has reached its redemption limit",
        "sorry, this coupon is no longer available",
    )
    if any(phrase in lowered for phrase in expired_phrases):
        return "NOT_FREE"

    # Parse JSON-LD blocks and common serialized page payloads. No clear zero-price evidence
    # means we fail closed: never label an unverified course as free.
    positive_price = False
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.I | re.S):
        try:
            payload = json.loads(html.unescape(raw))
            if _zero_price_in_json(payload):
                return "ACTIVE_FREE"
            positive_price |= _positive_price_in_json(payload)
        except Exception:
            pass
    if re.search(r'["\'](?:price_text|priceText|discount_price_text|discountPriceText)["\']\s*:\s*["\']free["\']', lowered):
        return "ACTIVE_FREE"
    if re.search(r'["\'](?:discount_price|discountPrice)["\']\s*:\s*\{[^{}]{0,300}["\']amount["\']\s*:\s*0(?:\.0+)?(?:[,}])', lowered):
        return "ACTIVE_FREE"
    if positive_price or re.search(r'["\'](?:discount_price|discountPrice)["\']\s*:\s*\{[^{}]{0,300}["\']amount["\']\s*:\s*(?!0(?:\.0+)?(?:[,}]))\d+(?:\.\d+)?', lowered):
        return "NOT_FREE"
    return "UNVERIFIED"


MIN_REVIEWS_FOR_RATING = 20     # a rating counts only when at least this many people gave it
MIN_AGE_DAYS_FOR_STUDENTS = 30  # a low student count counts only for courses older than this


def quality_limits(cfg, state):
    r = state.get("min_rating")
    st = state.get("min_students")
    return (float(cfg.get("min_rating", 0) or 0) if r is None else float(r),
            int(cfg.get("min_students", 0) or 0) if st is None else int(st))


def quality_ok(d, limits, now):
    """Drop a course only when it is PROVEN weak. Unknown or brand-new data always passes."""
    min_rating, min_students = limits
    meta = d.get("meta") or {}
    rating, reviews = d.get("rating"), meta.get("reviews")
    if min_rating and rating and reviews is not None and reviews >= MIN_REVIEWS_FOR_RATING and rating < min_rating:
        return False, f"rating {rating:.1f} from {int(reviews)} reviews is under {min_rating:g}"
    students, created = meta.get("students"), meta.get("created_at")
    if (min_students and students and created and students < min_students
            and (now - created).days >= MIN_AGE_DAYS_FOR_STUDENTS):
        return False, f"{int(students)} students on a course older than {MIN_AGE_DAYS_FOR_STUDENTS} days (min {min_students})"
    return True, ""


def quality_lines(d):
    """Lines that let you judge a course yourself: rating, students, instructor, or 'new'."""
    meta = d.get("meta") or {}
    reviews, students = meta.get("reviews"), meta.get("students")
    out = []
    if d.get("rating") and reviews:
        out.append(f"⭐ {d['rating']:.1f} ({int(reviews):,} reviews)" + (f" · 👥 {int(students):,} students" if students else ""))
    elif reviews is not None or students is not None or meta.get("created_at"):
        out.append("🆕 New course - no ratings yet" + (f" · 👥 {int(students):,} students" if students else ""))
    elif d.get("rating"):
        out.append(f"⭐ {d['rating']:.1f}")
    if meta.get("instructor"):
        out.append(f"👤 {meta['instructor']}")
    return out


def assess(d, cfg, now):
    """Decide whether a coupon may be sent. Returns (verdict, note): verdict is SEND, SKIP (dead, remember it)
    or WAIT (cannot confirm now; try again next run). Udemy's own answer wins; otherwise the feed's own
    freshness data is used; otherwise nothing is sent."""
    meta = d.get("meta") or {}
    if meta.get("no_coupon"):                 # listed as free with no coupon: only Udemy's own price can confirm it
        v = udemy_price_check(d["url"], meta.get("course_id"))
        if v == "ACTIVE_FREE":
            return "SEND", "✅ Verified free on Udemy"
        return ("SKIP", "") if v == "NOT_FREE" else ("WAIT", "")
    v = verify_udemy_coupon(d["url"], meta.get("course_id"))
    if v == "ACTIVE_FREE":
        return "SEND", "✅ Verified free on Udemy"
    if v == "NOT_FREE":
        return "SKIP", ""
    if cfg.get("send_unverified", False):
        return "SEND", "⚠️ Not verified - may have expired"
    fresh = cfg.get("fresh_minutes", 120)
    if meta.get("uses_left") is not None:                      # courson: Udemy data refreshed by the site
        ref_t = meta.get("refreshed_at")
        age = (now - ref_t).total_seconds() / 60 if ref_t else None
        if d["discount"] < 100 or meta["uses_left"] <= 0:
            return "SKIP", ""
        if age is not None and age <= max(fresh, 180):
            return "SEND", f"ℹ️ Feed shows {int(meta['uses_left'])} of {int(meta['max_uses'] or 0)} uses left"
        return "SKIP", ""
    if meta.get("listed_at"):                                   # real.discount: only the listing time is known
        age = (now - meta["listed_at"]).total_seconds() / 60
        if meta.get("sale_price") not in (None, 0.0):
            return "SKIP", ""
        if age <= fresh:
            return "SEND", "ℹ️ Udemy check unavailable; the listing is recent"
        return "SKIP", ""
    return "WAIT", ""


def get_json(url, headers=None, data=None):
    body = http_request(url, headers=headers, data=data)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError(f"not JSON from {urllib.parse.urlparse(url).netloc}; starts with: {body[:120]!r}") from None


def _parse_iso(v):
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def track_expiry(history, now, max_checks=10, window_hours=48, check_fn=None):
    """Re-check recently sent coupons and record when each one stops being free.

    last_ok  = last time Udemy confirmed it free (starts as the send time)
    gone_at  = first time Udemy said it is no longer free
    ended    = gone_at, written only after a SECOND consecutive "not free" answer (avoids false alarms)
    Returns True when history.json should be rewritten.
    """
    check = check_fn or udemy_price_check
    cand = []
    for h in history:
        if not isinstance(h, dict) or not h.get("url") or h.get("ended"):
            continue
        at = _parse_iso(h.get("at"))
        if at and (now - at) <= timedelta(hours=window_hours):
            cand.append(h)
    if not cand:
        return False
    cand.sort(key=lambda h: str(h.get("at")))
    if len(cand) > max_checks:                      # rotate so every coupon is checked in turn
        off = (int(now.timestamp() // 300) * max_checks) % len(cand)
        cand = (cand[off:] + cand[:off])[:max_checks]
    changed = False
    stamp = now.isoformat(timespec="seconds")
    for h in cand:
        v = check(h["url"])
        if v == "ACTIVE_FREE":
            last = _parse_iso(h.get("last_ok") or h.get("at"))
            if h.get("gone_at"):
                h.pop("gone_at", None)
                changed = True
            if not last or (now - last) >= timedelta(minutes=10):
                h["last_ok"] = stamp
                changed = True
        elif v == "NOT_FREE":
            if h.get("gone_at"):
                h["ended"] = h["gone_at"]
            else:
                h["gone_at"] = stamp
            changed = True
    return changed


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


def parse_time(v):
    """'2026-11-19 09:43:07' or 'Thu, 19 Nov 2026 09:41:22 GMT' (both UTC) -> aware datetime, or None."""
    if not v or not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        if t.tzinfo:
            return t.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def deal(title, url, source, discount=100, categories=None, text="", rating=None, ref="", resolve=None):
    return {"title": html.unescape(title or "").strip(), "url": url, "discount": discount,
            "categories": [c for c in (categories or []) if c], "text": (text or "")[:500],
            "rating": rating, "source": source, "ref": ref, "resolve": resolve,
            "evidence": "", "trust_free": True, "meta": {}, "note": ""}


def lazy_deal(title, ref, source, resolve, evidence="", trust_free=True):
    """Listing item whose Udemy coupon link is fetched later (only if the title matches).

    evidence   - listing text that may state the discount (e.g. '25% OFF')
    trust_free - True if the site only lists 100% coupons; False if it mixes in partial discounts
    """
    d = deal(title, "", source, 100, [], "", None, ref, resolve)
    d["evidence"], d["trust_free"] = evidence or "", trust_free
    return d


def discount_percents(text):
    """All percentages that are written as a discount ('25% off', 'discount 80%', '100 % OFF')."""
    t = html.unescape(text or "")
    found = re.findall(r"(\d{1,3})\s*%\s*(?:off|discount)", t, re.I)
    found += re.findall(r"(?:off|discount)\s*(?:of\s*)?:?\s*(\d{1,3})\s*%", t, re.I)
    return [int(x) for x in found]


def listing_says_free(d):
    """Decide from the listing text only (before spending a request on the coupon link)."""
    text = d["title"] + " " + d["evidence"]
    pcts = discount_percents(text)
    if 100 in pcts:
        return True
    if pcts:                      # only partial discounts mentioned, e.g. 25% off
        return False
    if re.search(r"\bfree\b", text, re.I):
        return True
    return d["trust_free"]        # no information: trust sites that list only free coupons


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
            dd = deal(it.get("name") or it.get("title"), link, "real.discount", 100,
                      [cat] if isinstance(cat, str) else list(cat),
                      strip_tags(it.get("shoer_description") or it.get("description") or ""),
                      to_float(it.get("rating")))
            dd["meta"] = {"listed_at": parse_time(it.get("sale_start")), "sale_price": to_float(it.get("sale_price"))}
            out.append(dd)
    return out


def _as_list(v):
    """A list of short strings from a feed field that may be a list, a Python-style list text, or empty."""
    try:
        if isinstance(v, str):
            v = ast_literal(v)
        return [str(x).strip() for x in (v or []) if str(x).strip()][:12]
    except Exception:
        return []


def _instructor_name(v):
    """Instructor label from courson's 'instructors' field (a list of {name: profile_url}), or ''."""
    try:
        if isinstance(v, str):
            v = ast_literal(v)
        names = [k for item in v for k in (item.keys() if isinstance(item, dict) else [str(item)])]
        return ", ".join(names)[:70]
    except Exception:
        return ""


def ast_literal(text):
    import ast
    return ast.literal_eval(text)


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
            cats = [c.get("category"), c.get("subcategory"), c.get("topic"), c.get("primary_category"), c.get("primary_subcategory")]
            headline = c.get("headline") or ""
            title = headline if 0 < len(headline) <= 100 else slug.replace("-", " ").title()
            pct = to_float(c.get("coupon_discount_percent"))
            dd = deal(title, f"https://www.udemy.com/course/{slug}/?couponCode={code}",
                      "courson", pct if pct is not None else 100, [x for x in cats if isinstance(x, str)],
                      headline + " " + (c.get("description") or ""), to_float(c.get("rating")))
            dd["meta"] = {"course_id": c.get("id"), "uses_left": to_float(c.get("coupon_uses_remaining")),
                          "max_uses": to_float(c.get("coupon_max_uses")),
                          "refreshed_at": parse_time(c.get("last_udemy_refresh")),
                          "reviews": to_float(c.get("num_reviews")), "students": to_float(c.get("num_subscribers")),
                          "created_at": parse_time(c.get("created_date")),
                          "instructor": _instructor_name(c.get("instructors")),
                          "labels": _as_list(c.get("labels"))}
            out.append(dd)
    return out


# ---------- lazy sources (titles first, coupon link only for matching titles) ----------
def fetch_feed_text(url):
    """RSS/Atom text; some feeds arrive gzip-compressed."""
    import gzip
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,*/*",
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def source_comidoc(cfg):
    """Comidoc RSS. The free feed has no coupon codes, so only courses it lists as free with
    'no coupon needed' are used (Udemy's price is checked before anything is sent)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(fetch_feed_text(cfg.get("comidoc_feed", "https://comidoc.com/rss.xml")))
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        desc = strip_tags(it.findtext("description") or "")
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/udemy/([A-Za-z0-9_\-]+)", urllib.parse.urlparse(link).path)
        if not title or not m:
            continue
        slug = m.group(1)
        encoded = next((k.text or "" for k in it if k.tag.endswith("encoded")), "")
        coupon_url = find_udemy_url(encoded or (it.findtext("description") or ""))
        if coupon_url:
            dd = deal(title, coupon_url, "comidoc", 100, [], desc)
        elif re.search(r"no coupon needed", desc, re.I):
            dd = deal(title, f"https://www.udemy.com/course/{slug}/", "comidoc", 100, [], desc)
            dd["meta"] = {"no_coupon": True}
        else:
            continue                    # discounted but not free, or a coupon whose code the free feed hides
        dd["meta"] = {**dd.get("meta", {}), "listed_at": parse_time(it.findtext("pubDate"))}
        out.append(dd)
    return out


DISCUDEMY_HOSTS = ("www.couponami.com", "www.discudemy.com")   # DiscUdemy is now called Couponami


def source_discudemy(cfg):
    out, host = [], None
    for page in range(1, cfg.get("pages_html", 3) + 1):
        found = []
        for h in ([host] if host else list(DISCUDEMY_HOSTS)):
            try:
                found = anchors(http_html(f"https://{h}/all/{page}", f"https://{h}"), "card-header")
            except Exception as e:
                print(f"[discudemy] {h} page {page}: {e}")
                found = []
            if found:
                host = h
                break
        if not found:
            if page == 1:
                raise RuntimeError("no listing from couponami.com or discudemy.com")
            break
        for it in found:
            gid = it["href"].rstrip("/").split("/")[-1]
            title = html.unescape(it["text"]).strip()
            if gid and title:
                def resolve(g=gid, first=host):
                    for h in [first] + [x for x in DISCUDEMY_HOSTS if x != first]:
                        try:
                            got = find_udemy_url(http_html(f"https://{h}/go/{g}", f"https://{h}"))
                        except Exception:
                            got = ""
                        if got:
                            return got
                    return ""
                out.append(lazy_deal(title, f"discudemy:{gid}", "discudemy", resolve))
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
    use_cat = True  # the category id may have changed on the site; fall back to no category if empty
    for page in range(1, cfg.get("pages_html", 3) + 1):
        params = {"per_page": 100, "page": page}
        if use_cat:
            params["product_cat"] = 15
        try:
            posts = get_json("https://idownloadcoupon.com/wp-json/wp/v2/product?" + urllib.parse.urlencode(params))
            if page == 1 and use_cat and not posts:
                use_cat = False
                posts = get_json("https://idownloadcoupon.com/wp-json/wp/v2/product?"
                                 + urllib.parse.urlencode({"per_page": 100, "page": 1}))
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
                evidence = strip_tags(((p.get("excerpt") or {}).get("rendered", "")) + " "
                                      + ((p.get("content") or {}).get("rendered", "")))
                # this site also lists partial discounts, so a coupon counts as free only if it says so
                out.append(lazy_deal(title, f"idownloadcoupon:{pid}", "idownloadcoupon",
                                     lambda i=pid: find_udemy_url(redirect_location(f"https://idownloadcoupon.com/udemy/{i}/")),
                                     evidence=evidence, trust_free=False))
    return out


# ---------- phase 5: social sources (direct: the post contains the Udemy coupon link) ----------
def _post_discount(text):
    pcts = discount_percents(text)
    return 100 if (100 in pcts or not pcts) else max(pcts)


def _open_and_find(url):
    """Open a post page (following redirects) and return the Udemy coupon link found in it or its final URL."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return find_udemy_url(r.geturl()) or find_udemy_url(r.read().decode("utf-8", "replace"))


def _external_link(chunk):
    for h in re.findall(r'href="(https?://[^"]+)"', chunk):
        h = html.unescape(h)
        host = urllib.parse.urlparse(h).netloc.lower()
        if host and not host.endswith(("t.me", "telegram.me", "telegram.org", "example.com", "udemy.com")):
            return h
    return ""


def source_telegram(cfg):
    """Public Telegram channels through their web preview (t.me/s/<channel>), newest posts only.

    A post that carries the Udemy coupon link is used directly. A post that only links to the channel's
    own website becomes a lazy item: the page is opened later, only if the title matches your keywords."""
    out = []
    channels = cfg.get("telegram_channels", ["udemy4u"])
    for ch in channels:
        try:
            page = http_html(f"https://t.me/s/{urllib.parse.quote(ch)}", "https://t.me/")
        except Exception as e:
            print(f"[telegram:{ch}] skipped: {e}")
            if len(channels) <= 1:
                raise
            continue
        for chunk in page.split("tgme_widget_message_wrap")[1:]:
            m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
            text = strip_tags(re.sub(r"<br\s*/?>", "\n", m.group(1))) if m else ""
            title = next((ln.strip(" -*|:#") for ln in text.split("\n") if len(ln.strip()) > 8), "")
            t = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
            listed = parse_time(t.group(1)) if t else None
            link = find_udemy_url(chunk)
            if link:
                if not title:
                    title = urllib.parse.urlparse(link).path.strip("/").split("/")[-1].replace("-", " ").title()
                dd = deal(title[:120], link, f"telegram:{ch}", _post_discount(text), [], text)
            else:
                ext = _external_link(chunk)
                pid = re.search(r'data-post="([^"]+)"', chunk)
                if not ext or not title or not pid:
                    continue
                dd = lazy_deal(title[:120], f"tg:{pid.group(1)}", f"telegram:{ch}",
                               lambda u=ext: _open_and_find(u))
            dd["meta"] = {"listed_at": listed}
            out.append(dd)
    return out


def source_reddit(cfg):
    """Reddit subreddits through their public RSS feed."""
    import xml.etree.ElementTree as ET
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for sub in cfg.get("reddit_subs", ["udemyfreebies"]):
        try:
            body = http_request(f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/new/.rss?limit=100",
                                headers={"Accept": "application/atom+xml,application/xml,*/*"})
            root = ET.fromstring(body)
        except Exception as e:
            print(f"[reddit:{sub}] skipped: {e}")
            if len(cfg.get("reddit_subs", [])) <= 1:
                raise
            continue
        for ent in root.findall("a:entry", ns):
            title = (ent.findtext("a:title", "", ns) or "").strip()
            content = ent.findtext("a:content", "", ns) or ""
            link = find_udemy_url(content) or find_udemy_url(" ".join(
                x.get("href", "") for x in ent.findall("a:link", ns)))
            if not link:
                continue
            clean = re.sub(r"^\s*[\[(][^\])]{0,25}[\])]\s*", "", title) or title
            dd = deal(clean[:120], link, f"reddit:{sub}", _post_discount(title + " " + strip_tags(content)), [],
                      strip_tags(content))
            dd["meta"] = {"listed_at": parse_time(ent.findtext("a:updated", "", ns))}
            out.append(dd)
    return out


SOURCES = {
    "realdiscount": source_realdiscount,
    "courson": source_courson,
    "discudemy": source_discudemy,
    "udemyfreebies": source_udemyfreebies,
    "idownloadcoupon": source_idownloadcoupon,
    "comidoc": source_comidoc,
    "telegram": source_telegram,
    "reddit": source_reddit,
}


# ---------- filtering ----------
def haystack(d, cfg):
    slug = urllib.parse.urlparse(d["url"]).path.strip("/").split("/")[-1].replace("-", " ") if d["url"] else ""
    parts = [d["title"], " ".join(d["categories"]), slug, " ".join((d.get("meta") or {}).get("labels") or [])]
    if cfg.get("match_description", False):
        parts.append(d["text"])
    return " ".join(parts).lower()


def is_free(d, cfg):
    """Passes the basic rules: discount, rating, exclusions, language."""
    disc = d["discount"]
    if disc is None or disc < cfg.get("min_discount", 100):
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
def tg_send(text, silent=False, preview=True, button=None):
    """Send a Telegram message. button=(label, url) adds one inline button under it."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.")
    fields = {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false" if preview else "true",
        "disable_notification": "true" if silent else "false",
    }
    if button:
        fields["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": button[0], "url": button[1]}]]})
    for attempt in (1, 2):
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=urllib.parse.urlencode(fields).encode())
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 400 and "reply_markup" in fields and attempt == 1:
                del fields["reply_markup"]      # Telegram refused the button: send the message without it
                print("[telegram] button refused, sending without it")
                continue
            raise SystemExit(f"Telegram error HTTP {e.code} (check token / chat id)") from None
    time.sleep(0.5)  # stay well under Telegram rate limits


def age_text(t, now):
    """'5 min ago', '3 h ago', '2 days ago' for a UTC datetime, or '' if unknown."""
    if not t:
        return ""
    mins = max(0, int((now - t).total_seconds() // 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def format_deal(d, now=None):
    now = now or datetime.now(timezone.utc)
    cat = ", ".join(c for c in d["categories"] if c)[:80]
    lines = [f"🎓 <b>{html.escape(d['title'])}</b>", "💸 FREE (100% off)"]
    if cat:
        lines.append(f"🏷 {html.escape(cat)}")
    lines.extend(html.escape(x) for x in quality_lines(d))
    meta = d.get("meta") or {}
    when = age_text(meta.get("listed_at"), now)
    if when:
        lines.append(f"🕒 Listed {when}")
    lines.append("✅ Free course - no coupon needed" if (d.get("meta") or {}).get("no_coupon")
                 else "⚡ Free coupons run out fast - open it now")
    lines.append(f"🔗 {html.escape(d['url'])}")
    if d.get("note"):
        lines.append(html.escape(d["note"]))
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


# ---------- phase 1: source health + weekly summary ----------
def load_state():
    s = load_json(STATE_FILE, {})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("health", {})      # {source: {"fails": n, "alerted": bool, "last_error": str}}
    s.setdefault("sent", [])        # alerts sent since the last summary: [{"t": title, "s": source}]
    s.setdefault("last_summary", "")
    s.setdefault("kw_add", [])          # phase 2: keywords added from Telegram (/add)
    s.setdefault("kw_remove", [])       # phase 2: config keywords switched off from Telegram (/remove)
    s.setdefault("paused_until", "")    # phase 2: "" | "forever" | ISO time in UTC
    s.setdefault("tg_offset", 0)        # phase 2: next Telegram update id to read
    s.setdefault("awaiting", "")        # phase 2: "add" | "remove" after a bare /add or /remove
    s.setdefault("min_rating", None)    # phase 6: None = use config.json, 0 = off
    s.setdefault("min_students", None)  # phase 6: None = use config.json, 0 = off
    s.setdefault("stats_refs", {})      # this week: testing courses seen -> what happened to each
    s.setdefault("await_until", "")     # phase 2: ISO time (UTC) when that wait expires
    return s


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def update_health(state, results, cfg, notify):
    """Count consecutive bad runs per source; alert once after N, and again when it recovers.

    Healthy runs change nothing, so they never create a commit. Returns True if state changed."""
    limit = cfg.get("health_alert_after_runs", 12)   # 12 runs ~ 1 hour at a 5-minute schedule
    changed = False
    for name, (count, err) in results.items():
        h = state["health"].setdefault(name, {"fails": 0, "alerted": False, "last_error": ""})
        if err or count == 0:
            h["fails"] += 1
            h["last_error"] = err or "returned 0 items"
            changed = True
            if h["fails"] >= limit and not h["alerted"]:
                notify(f"⚠️ Source <b>{html.escape(name)}</b> has failed {h['fails']} runs in a row.\n"
                       f"Last problem: {html.escape(h['last_error'])[:200]}")
                h["alerted"] = True
        elif h["fails"] or h["alerted"]:
            if h["alerted"]:
                notify(f"✅ Source <b>{html.escape(name)}</b> is working again.")
            h["fails"], h["alerted"], h["last_error"] = 0, False, ""
            changed = True
    return changed


OUTCOME_ICON = {"sent": "✅", "expired": "⏱", "quality": "⭐", "waiting": "⏳"}
OUTCOME_TEXT = {"sent": "sent", "expired": "coupon already ended", "quality": "filtered by quality",
                "waiting": "could not be confirmed"}


def record(state, ref, title, outcome, now):
    """Remember what happened to a keyword-matching course (once per course; the outcome can still change)."""
    refs = state["stats_refs"]
    if refs.get(ref, {}).get("o") == outcome:
        return
    refs.pop(ref, None)
    refs[ref] = {"t": title[:80], "o": outcome, "at": now.isoformat(timespec="minutes")}
    while len(refs) > 400:
        refs.pop(next(iter(refs)))


def stats_line(state):
    c = Counter(v.get("o") for v in state.get("stats_refs", {}).values())
    total = sum(c.values())
    if not total:
        return "🔎 No testing courses matched your keywords in this period."
    return (f"🔎 Testing courses found: {total}\n"
            f"✅ sent {c['sent']} · ⏱ already ended {c['expired']} · "
            f"⭐ filtered {c['quality']} · ⏳ unconfirmed {c['waiting']}")


def missed_text(state, limit=10):
    rows = [v for v in state.get("stats_refs", {}).values() if v.get("o") != "sent"][-limit:]
    if not rows:
        return "Nothing missed: every matching course was either sent or none matched."
    lines = ["🕵️ <b>Matching courses that were NOT sent</b> (newest last)"]
    for v in rows:
        lines.append(f"{OUTCOME_ICON.get(v['o'], '•')} {html.escape(v['t'])} - {OUTCOME_TEXT.get(v['o'], v['o'])}")
    return "\n".join(lines)


def build_summary(state, cfg):
    sent = state.get("sent", [])
    lines = ["📊 <b>Weekly summary</b>", f"Courses sent: {len(sent)}"]
    by_source = Counter(x.get("s", "?") for x in sent)
    if by_source:
        lines.append("By source: " + ", ".join(f"{s} {n}" for s, n in by_source.most_common()))
    for x in sent[-15:]:
        lines.append(f"• {html.escape(x.get('t', ''))}")
    if len(sent) > 15:
        lines.append(f"<i>(+{len(sent) - 15} more)</i>")
    lines.append("")
    lines.append(stats_line(state))
    lines.append("")
    lines.append("<b>Sources</b>")
    for name in cfg.get("sources", list(SOURCES)):
        h = state["health"].get(name, {})
        lines.append(f"{'⚠️' if h.get('fails') else '✅'} {html.escape(name)}"
                     + (f" (failing for {h['fails']} runs)" if h.get("fails") else ""))
    return "\n".join(lines)


def maybe_weekly_summary(state, cfg, notify, now=None):
    """Send the summary when due. The first ever run only records today's date. Returns True if state changed."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    last = state.get("last_summary")
    if not last:
        state["last_summary"] = today
        return True
    if (date.fromisoformat(today) - date.fromisoformat(last)).days < cfg.get("summary_every_days", 7):
        return False
    if now.hour < cfg.get("summary_hour_utc", 7):      # 7 UTC = 9:00 in Egypt
        return False
    notify(build_summary(state, cfg))
    state["sent"], state["last_summary"], state["stats_refs"] = [], today, {}
    return True


# ---------- phase 2: control the bot from Telegram ----------
MAX_KW_LEN = 60
MAX_ADDED = 200
HELP_TEXT = (
    "🤖 <b>Commands</b>\n"
    "/list - show the keywords in use\n"
    "/add playwright, cypress - add keywords (comma separated)\n"
    "/remove jira - remove a keyword\n"
    "(or send just /add or /remove, then send the keywords as your next message)\n"
    "/pause 6h - mute alerts for 6 hours (also 2d; plain /pause = until /resume)\n"
    "/resume - turn alerts back on\n"
    "/minrating 3.5 - skip courses with a proven low rating (0 = off)\n"
    "/minstudents 100 - skip old courses with few students (0 = off)\n"
    "/stats - how many testing courses were found this week and what happened to them\n"
    "/missed - matching courses that were NOT sent, and why\n"
    "/status - pause state, filters and source health\n\n"
    "Commands are read every few minutes, so the reply is not instant."
)


def norm_kw(s):
    return re.sub(r"\s+", " ", (s or "").strip().strip("\"'`").lower())


def parse_keywords(arg):
    return [k for k in (norm_kw(p) for p in re.split(r"[,;\n،]", arg or "")) if k]


def effective_keywords(cfg, state):
    """config.json keywords, minus the ones removed from Telegram, plus the ones added from Telegram."""
    removed = set(state.get("kw_remove", []))
    out = [k for k in cfg.get("categories", []) if k.lower() not in removed]
    have = {k.lower() for k in out}
    for k in state.get("kw_add", []):
        if k not in have:
            out.append(k)
            have.add(k)
    return out


def parse_duration(arg, now):
    """'6h', '2d' or '6' (hours) -> UTC datetime, capped at 30 days. None if it is not a duration."""
    m = re.fullmatch(r"(\d{1,3})\s*([hHdD]?)", (arg or "").strip())
    if not m or int(m.group(1)) == 0:
        return None
    hours = int(m.group(1)) * (24 if m.group(2).lower() == "d" else 1)
    return now + timedelta(hours=min(hours, 30 * 24))


def pause_status(state, now):
    p = state.get("paused_until") or ""
    if p == "forever":
        return True
    if not p:
        return False
    try:
        return now < datetime.fromisoformat(p)
    except ValueError:
        return False


def awaiting_mode(state, now):
    """'add' or 'remove' while the bot is waiting for keywords after a bare /add or /remove, else ''."""
    mode = state.get("awaiting") or ""
    try:
        ok = mode in ("add", "remove") and now < datetime.fromisoformat(state.get("await_until") or "")
    except ValueError:
        ok = False
    return mode if ok else ""


def start_awaiting(state, mode, now):
    state["awaiting"], state["await_until"] = mode, (now + timedelta(hours=6)).isoformat()
    return ("✍️ Now send the keyword(s) as a normal message, for example "
            "<code>playwright, cypress</code>\nOr send /cancel."), True


def clear_awaiting(state):
    state["awaiting"], state["await_until"] = "", ""


def wrap_words(items, width=300):
    lines, cur = [], ""
    for it in items:
        piece = it if not cur else ", " + it
        if cur and len(cur) + len(piece) > width:
            lines.append(cur + ",")
            cur, piece = "", it
        cur += piece
    if cur:
        lines.append(cur)
    return lines


def send_long(text, silent=False):
    """Telegram rejects messages over 4096 characters, so split on line boundaries."""
    chunk, size = [], 0
    for line in text.split("\n"):
        if chunk and size + len(line) + 1 > 3800:
            tg_send("\n".join(chunk), silent=silent, preview=False)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        tg_send("\n".join(chunk), silent=silent, preview=False)


def handle_command(text, state, cfg, now):
    """Run one command from the owner. Returns (reply_html, state_changed)."""
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    base = {k.lower() for k in cfg.get("categories", [])}
    esc = lambda items: html.escape(", ".join(items))

    if cmd == "/cancel":
        return "👌 Cancelled.", False

    if cmd in ("/start", "/help"):
        return HELP_TEXT, False

    if cmd == "/list":
        eff = effective_keywords(cfg, state)
        lines = [f"📋 <b>Keywords in use ({len(eff)})</b>"] + [html.escape(l) for l in wrap_words(eff)]
        if state["kw_add"]:
            lines += ["", "<b>Added by you:</b> " + esc(state["kw_add"])]
        if state["kw_remove"]:
            lines += ["", "<b>Removed by you:</b> " + esc(state["kw_remove"])]
        return "\n".join(lines), False

    if cmd == "/add":
        if not arg:
            return start_awaiting(state, "add", now)
        kws = parse_keywords(arg)
        if not kws:
            return "Usage: <code>/add playwright, cypress</code>", False
        added, restored, already, bad = [], [], [], []
        for k in kws:
            if len(k) > MAX_KW_LEN:
                bad.append(k[:20] + "…")
            elif k in state["kw_remove"]:
                state["kw_remove"].remove(k)
                restored.append(k)
            elif k in {x.lower() for x in effective_keywords(cfg, state)}:
                already.append(k)
            elif len(state["kw_add"]) >= MAX_ADDED:
                bad.append(k)
            else:
                state["kw_add"].append(k)
                added.append(k)
        out = []
        if added:
            out.append("✅ Added: " + esc(added))
        if restored:
            out.append("✅ Switched back on: " + esc(restored))
        if already:
            out.append("ℹ️ Already in the list: " + esc(already))
        if bad:
            out.append(f"⚠️ Not added (longer than {MAX_KW_LEN} characters or too many added): " + esc(bad))
        return "\n".join(out), bool(added or restored)

    if cmd == "/remove":
        if not arg:
            return start_awaiting(state, "remove", now)
        kws = parse_keywords(arg)
        if not kws:
            return "Usage: <code>/remove jira</code>", False
        removed, missing = [], []
        for k in kws:
            if k in state["kw_add"]:
                state["kw_add"].remove(k)
                removed.append(k)
            elif k in base and k not in state["kw_remove"]:
                state["kw_remove"].append(k)
                removed.append(k)
            else:
                missing.append(k)
        out = []
        if removed:
            out.append("🗑 Removed: " + esc(removed))
        if missing:
            out.append("ℹ️ Not in the list: " + esc(missing))
        if not effective_keywords(cfg, state):
            out.append("⚠️ The list is empty now, so nothing will be sent until you /add a keyword.")
        return "\n".join(out), bool(removed)

    if cmd == "/pause":
        if not arg:
            state["paused_until"] = "forever"
            return "⏸ Alerts paused until you send /resume.", True
        until = parse_duration(arg, now)
        if not until:
            return "Usage: <code>/pause 6h</code> or <code>/pause 2d</code> (or just /pause)", False
        state["paused_until"] = until.isoformat()
        return f"⏸ Alerts paused until {until:%Y-%m-%d %H:%M} UTC. Send /resume to turn them on earlier.", True

    if cmd in ("/minrating", "/minstudents"):
        name = cmd[1:].replace("min", "min_")
        if not arg:
            cur = quality_limits(cfg, state)[0 if cmd == "/minrating" else 1]
            return f"Current {cmd[1:]}: {cur:g} (0 = off). Example: <code>{cmd} {'3.5' if cmd == '/minrating' else '100'}</code>", False
        try:
            val = float(arg.replace(",", "."))
        except ValueError:
            return f"Usage: <code>{cmd} {'3.5' if cmd == '/minrating' else '100'}</code> (0 turns it off)", False
        if cmd == "/minrating":
            if not 0 <= val <= 5:
                return "The rating must be between 0 and 5.", False
            state["min_rating"] = val
            note = (f"Courses with a rating under {val:g} are skipped, but only when at least "
                    f"{MIN_REVIEWS_FOR_RATING} people rated them. New courses without ratings are always sent.") if val else ""
        else:
            if val < 0 or val > 1000000:
                return "Send a whole number like 100 (0 turns it off).", False
            state["min_students"] = int(val)
            note = (f"Courses with fewer than {int(val)} students are skipped, but only when they are older than "
                    f"{MIN_AGE_DAYS_FOR_STUDENTS} days. New courses are always sent.") if val else ""
        return (f"✅ {cmd[1:]} set to {val:g}. " + note) if val else f"✅ {cmd[1:]} is off.", True

    if cmd == "/stats":
        return stats_line(state), False

    if cmd == "/missed":
        return missed_text(state), False

    if cmd == "/resume":
        if not state.get("paused_until"):
            return "▶️ Alerts are already on.", False
        state["paused_until"] = ""
        return "▶️ Alerts are on again.", True

    if cmd == "/status":
        lines = []
        if pause_status(state, now):
            until = state["paused_until"]
            lines.append("⏸ Paused" + ("" if until == "forever" else f" until {until[:16].replace('T', ' ')} UTC"))
        else:
            lines.append("▶️ Alerts are on")
        eff = effective_keywords(cfg, state)
        lines.append(f"Keywords: {len(eff)} ({len(state['kw_add'])} added, {len(state['kw_remove'])} removed)")
        mr, ms = quality_limits(cfg, state)
        lines.append(f"Filters: min rating {mr:g}, min students {ms} (0 = off)")
        for name in cfg.get("sources", list(SOURCES)):
            h = state["health"].get(name, {})
            lines.append(f"{'⚠️' if h.get('fails') else '✅'} {html.escape(name)}"
                         + (f" (failing for {h['fails']} runs)" if h.get("fails") else ""))
        return "\n".join(lines), False

    return "Unknown command. Send /help to see the list.", False


def fetch_updates(offset):
    """New messages sent to the bot, or None if they could not be read. Never prints the token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    url = (f"https://api.telegram.org/bot{token}/getUpdates?"
           + urllib.parse.urlencode({"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"[telegram] could not read commands: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"[telegram] could not read commands: {type(e).__name__}")
        return None
    if not data.get("ok"):
        print("[telegram] could not read commands: not ok")
        return None
    return data.get("result", [])


def process_commands(state, cfg, now, notify, updates):
    """Handle commands from the owner only (messages from anyone else are ignored). Returns True if state changed."""
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    changed, last_id = False, None
    for u in updates:
        uid = u.get("update_id")
        if isinstance(uid, int):
            last_id = uid if last_id is None else max(last_id, uid)
        msg = u.get("message") or {}
        if not owner or str((msg.get("chat") or {}).get("id")) != owner or str((msg.get("from") or {}).get("id")) != owner:
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            if state.get("awaiting"):       # any new command ends the wait (a bare /add or /remove starts it again)
                clear_awaiting(state)
                changed = True
        else:
            mode = awaiting_mode(state, now)
            if not (mode and text):         # a normal message only counts right after a bare /add or /remove
                continue
            clear_awaiting(state)
            changed = True
            text = f"/{mode} {text}"
        reply, ch = handle_command(text, state, cfg, now)
        changed |= ch
        notify(reply)
    if last_id is not None:
        state["tg_offset"] = last_id + 1
        changed = True
    return changed


def diagnose_fields():
    """--fields: show what the feeds really contain (expiry? uses left?) and test ways to check Udemy."""
    def show(name, item):
        print(f"\n=== {name}: fields ===")
        for k, v in item.items():
            print(f"  {k}: {str(v)[:110]!r}")
    try:
        data = get_json("https://cdn.real.discount/api/courses?page=1&limit=3&sortBy=sale_start&store=Udemy&freeOnly=true",
                        headers={"Referer": "https://www.real.discount/", "Origin": "https://www.real.discount"})
        items = data.get("items") or data.get("results") or data.get("data") or []
        print("real.discount top-level keys:", list(data.keys()))
        for it in items[:2]:
            show("real.discount", it)
    except Exception as e:
        print("real.discount ERROR:", e)
    link = ""
    try:
        data = get_json("https://courson.xyz/load-more-coupons",
                        headers={"Content-Type": "application/json", "Referer": "https://courson.xyz/coupons",
                                 "Origin": "https://courson.xyz"},
                        data=json.dumps({"filters": {}, "offset": 0}).encode())
        for c in (data.get("coupons") or [])[:2]:
            show("courson", c)
        c0 = (data.get("coupons") or [{}])[0]
        if c0.get("id_name") and c0.get("coupon_code"):
            link = f"https://www.udemy.com/course/{c0['id_name']}/?couponCode={c0['coupon_code']}"
    except Exception as e:
        print("courson ERROR:", e)
    if link:
        print("\n=== Udemy check attempts (status only) ===")
        slug = urllib.parse.urlparse(link).path.strip("/").split("/")[-1]
        tests = {
            "course page": link,
            "api-2.0 course": f"https://www.udemy.com/api-2.0/courses/{slug}/?fields[course]=id,price,is_paid",
        }
        for label, u in tests.items():
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    print(f"  {label}: HTTP {r.status}, {len(r.read())} bytes")
            except urllib.error.HTTPError as e:
                print(f"  {label}: HTTP {e.code}")
            except Exception as e:
                print(f"  {label}: {type(e).__name__}")
        print("  price API verdict (ACTIVE_FREE / NOT_FREE / None):", udemy_price_check(link, c0.get("id")))


def diagnose_social():
    """--social: for each Telegram channel, show how many posts it has and which link domains they use."""
    cfg = load_json(CONFIG_FILE, {})
    for ch in cfg.get("telegram_channels", ["udemy4u"]):
        try:
            page = http_html(f"https://t.me/s/{urllib.parse.quote(ch)}", "https://t.me/")
        except Exception as e:
            print(f"[{ch}] ERROR: {e}")
            continue
        chunks = page.split("tgme_widget_message_wrap")[1:]
        doms = Counter()
        for c in chunks:
            for h in re.findall(r'href="(https?://[^"]+)"', c):
                doms[urllib.parse.urlparse(html.unescape(h)).netloc] += 1
        with_coupon = sum(1 for c in chunks if find_udemy_url(c))
        print(f"[{ch}] posts: {len(chunks)}, with Udemy coupon link: {with_coupon}")
        print("   link domains:", dict(doms.most_common(6)))
        try:
            lazy = [d for d in source_telegram({"telegram_channels": [ch]}) if d["resolve"]][:2]
        except Exception as e:
            print("   resolve test: could not read channel:", e)
            continue
        for d in lazy:
            try:
                got = d["resolve"]()
                print("   resolve test:", "COUPON FOUND" if got else "page opened, no coupon link in it", "|", d["title"][:50])
            except urllib.error.HTTPError as e:
                print(f"   resolve test: HTTP {e.code} (site blocks us) | {d['title'][:50]}")
            except Exception as e:
                print(f"   resolve test: {type(e).__name__} | {d['title'][:50]}")


def install_dns_fallback():
    """Used only by --probe: if this computer's DNS fails for a site, ask Google's DNS-over-HTTPS instead.
    Nothing on the computer is changed; it only affects this one run of the script."""
    import socket
    real = socket.getaddrinfo

    def patched(host, port, *args, **kwargs):
        try:
            return real(host, port, *args, **kwargs)
        except socket.gaierror:
            if not isinstance(host, str) or host.replace(".", "").isdigit():
                raise
            try:
                req = urllib.request.Request(f"https://8.8.8.8/resolve?name={urllib.parse.quote(host)}&type=A",
                                             headers={"Accept": "application/dns-json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    answers = json.loads(r.read().decode()).get("Answer") or []
            except Exception as e:
                print(f"   (Google DNS lookup for {host} failed too: {type(e).__name__}: {str(e)[:90]})")
                raise
            ips = [a["data"] for a in answers if a.get("type") == 1]
            if not ips:
                print(f"   (Google DNS has no address for {host})")
                raise
            print(f"   (system DNS failed for {host}; used Google DNS: {ips[0]})")
            return real(ips[0], port, *args, **kwargs)

    socket.getaddrinfo = patched


def probe_feed(url):
    """Download an RSS/Atom feed, decompress it if needed, and count items that carry a Udemy coupon link."""
    import gzip
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,*/*",
                                                   "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "replace")
        root = ET.fromstring(text)
    except Exception as e:
        print(f"  feed {url[:70]}: could not read ({type(e).__name__}: {str(e)[:70]})")
        return
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    coupon_items = sum(1 for _ in re.finditer(r"couponCode=", html.unescape(text)))
    print(f"  feed {url[:70]}: {len(items)} items, 'couponCode=' appears {coupon_items} times")
    for it in items[:2]:
        kids = {k.tag.split('}')[-1]: (k.text or k.attrib.get("href") or "").strip() for k in it}
        print("    item:", {k: v[:90] for k, v in list(kids.items())[:6]})


def diagnose_probe():
    print("probe version 16 (with DNS fallback, feed check, UTF-8 output)")
    install_dns_fallback()
    """--probe: test candidate coupon sites from this computer (reachable? coupon links? RSS? WordPress API?)."""
    cfg = load_json(CONFIG_FILE, {})
    sites = cfg.get("probe_sites") or ["https://comidoc.com/coupons", "https://www.coursesoup.com/",
                                        "https://www.couponami.com/", "https://www.couponami.com/all/1",
                                        "https://www.discudemy.com/all/1"]
    for url in sites:
        host = urllib.parse.urlparse(url).netloc
        print(f"\n=== {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", "replace")
                print(f"  HTTP {r.status}, {len(body)} chars, final URL: {r.geturl()[:80]}")
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} (blocked or missing)")
            continue
        except Exception as e:
            print(f"  not reachable: {type(e).__name__}: {str(e)[:80]}")
            continue
        udemy = re.findall(r'https?://(?:www\.)?udemy\.com/course/[^\s"\'<>]+', html.unescape(body))
        with_code = [u for u in udemy if "couponCode=" in u]
        print(f"  Udemy links on the page: {len(udemy)} (with coupon code: {len(with_code)})")
        rss = re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', body, re.I)
        print("  RSS/Atom links:", rss[:3] or "none")
        for feed in rss[:2]:
            probe_feed(urllib.parse.urljoin(url, feed))
        inner = [urllib.parse.urljoin(url, h) for h in re.findall(r'href="([^"#]+)"', body)
                 if urllib.parse.urlparse(urllib.parse.urljoin(url, h)).netloc == host
                 and re.search(r"/(udemy|course|coupon|free|deal)[^/]*/[^/]+", h)]
        for link in list(dict.fromkeys(inner))[:2]:
            try:
                got = _open_and_find(link)
                print(f"  page test: {'COUPON FOUND' if got else 'opened, no coupon link'} | {link[:70]}")
            except urllib.error.HTTPError as e:
                print(f"  page test: HTTP {e.code} | {link[:70]}")
            except Exception as e:
                print(f"  page test: {type(e).__name__} | {link[:70]}")
        try:
            req = urllib.request.Request(urllib.parse.urljoin(url, "/wp-json/wp/v2/posts?per_page=1"), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                print("  WordPress API: yes (HTTP", r.status, ")")
        except Exception:
            print("  WordPress API: no")


# ---------- main ----------
def main():
    for stream in (sys.stdout, sys.stderr):      # Windows consoles often cannot print every character
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--probe" in sys.argv:
        diagnose_probe()
        return

    if "--social" in sys.argv:
        diagnose_social()
        return

    if "--fields" in sys.argv:
        diagnose_fields()
        return

    if "--test" in sys.argv:
        tg_send("✅ Udemy Deal Alert is connected and working.")
        print("Test message sent.")
        return

    if "--summary" in sys.argv:   # send the weekly summary right now (does not reset anything)
        tg_send(build_summary(load_state(), load_json(CONFIG_FILE, {})), silent=True, preview=False)
        print("Summary sent.")
        return

    raw = "--raw" in sys.argv
    dry = "--dry" in sys.argv or raw
    cfg = load_json(CONFIG_FILE, {})
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    tracked = load_json(TRACKED_FILE, [])
    if not isinstance(tracked, list):
        tracked = []
    tracked_keys = {coupon_key(x.get("url", "")) for x in tracked if isinstance(x, dict) and x.get("url")}

    # phase 2: read commands sent to the bot (not in --dry, so a test run never eats them)
    now = datetime.now(timezone.utc)
    state = load_state()
    if not dry:
        updates = fetch_updates(state["tg_offset"])
        cmd_changed = process_commands(state, cfg, now, send_long, updates) if updates else False
        if state["paused_until"] not in ("", "forever") and not pause_status(state, now):
            state["paused_until"] = ""
            send_long("▶️ The pause is over. Alerts are on again.")
            cmd_changed = True
        if cmd_changed:
            save_state(state)   # saved right away so a later crash cannot replay the same commands
    cfg["categories"] = effective_keywords(cfg, state)

    names = cfg.get("sources", list(SOURCES))
    all_deals, errors, results = [], [], {}
    for name in names:
        fn = SOURCES.get(name)
        if not fn:
            print(f"[{name}] unknown source, skipped")
            continue
        try:
            got = fn(cfg)
            results[name] = (len(got), "")
            print(f"[{name}] fetched {len(got)} items")
            if raw:
                for d in got:
                    print("   *", d["title"][:70], "|", d["categories"], "|", d["url"] or d["ref"])
            all_deals.extend(got)
        except Exception as e:  # keep going if one source is down
            results[name] = (0, str(e))
            print(f"[{name}] ERROR: {e}")
            errors.append(name)

    # Recheck a bounded rotating batch of previously discovered coupons on every run.
    # This prevents expired offers from remaining trusted forever without flooding Udemy.
    max_checks = max(1, int(cfg.get("max_rechecks_per_run", 5)))
    check_batch, remainder = tracked[:max_checks], tracked[max_checks:]
    still_tracked = []
    for old in check_batch:
        if not isinstance(old, dict) or not old.get("url"):
            continue
        verdict = verify_udemy_coupon(old["url"])
        print(f"[recheck] {old.get('title', 'Previously seen course')[:55]}: {verdict}")
        if verdict != "NOT_FREE":
            still_tracked.append(old)
    tracked = remainder + still_tracked

    # new, unique, passing the basic rules. Lazy items are resolved only when their title matches.
    new, batch_keys, newly_seen = [], set(), set()
    resolves, fails, verifies = 0, {}, 0
    stats_before = json.dumps(state["stats_refs"], sort_keys=True)
    qlimits = quality_limits(cfg, state)
    max_ver = cfg.get("max_verifies_per_run", 25)
    max_res = cfg.get("max_resolves_per_run", 25)
    for d in all_deals:
        ref = deal_ref(d)
        if ref in seen_set or ref in batch_keys:
            continue
        batch_keys.add(ref)
        if d["url"]:                      # direct source
            if not (is_free(d, cfg) and is_interest(d, cfg)):
                newly_seen.add(ref)
                continue
            ok, why = quality_ok(d, qlimits, now)
            if not ok:
                print(f"[quality] skipped '{d['title'][:50]}': {why}")
                record(state, ref, d["title"], "quality", now)
                newly_seen.add(ref)
                continue
            if verifies >= max_ver:
                continue                  # over the per-run limit: look again next run
            verifies += 1
            verdict, note = assess(d, cfg, now)
            print(f"[verify] {d['title'][:55]}: {verdict} {note}")
            record(state, ref, d["title"], {"WAIT": "waiting", "SKIP": "expired"}.get(verdict, "sent"), now)
            if verdict == "WAIT":
                continue
            newly_seen.add(ref)
            if verdict == "SKIP":
                continue
            d["note"] = note
            new.append(d)
            if ref not in tracked_keys:
                tracked.append({"title": d["title"], "url": d["url"], "source": d["source"]})
                tracked_keys.add(ref)
            continue
        # lazy source
        if not (is_free(d, cfg) and is_interest(d, cfg)):
            newly_seen.add(ref)
            continue
        if not listing_says_free(d):
            print(f"[{d['source']}] skipped (not 100% off): {d['title'][:60]}")
            newly_seen.add(ref)
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
            newly_seen.add(ref)
            continue
        key = coupon_key(url)
        if key in seen_set or key in batch_keys:
            newly_seen.add(ref)
            continue
        batch_keys.add(key)
        d["url"] = url
        if not is_free(d, cfg):
            newly_seen.add(ref)
            continue
        if verifies >= max_ver:
            newly_seen.discard(ref)
            continue
        verifies += 1
        verdict, note = assess(d, cfg, now)
        print(f"[verify] {d['title'][:55]}: {verdict} {note}")
        record(state, ref, d["title"], {"WAIT": "waiting", "SKIP": "expired"}.get(verdict, "sent"), now)
        if verdict == "WAIT":
            continue                      # cannot confirm now: try again next run
        newly_seen.update((ref, key))
        if verdict == "SKIP":
            continue
        d["note"] = note
        new.append(d)
        if key not in tracked_keys:
            tracked.append({"title": d["title"], "url": url, "source": d["source"]})
            tracked_keys.add(key)

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

    paused = pause_status(state, now)
    if paused:
        print("PAUSED from Telegram: nothing will be sent (new coupons are still marked as seen)")

    if dry:
        for d in loud:
            print("INTEREST:", d["title"], "|", d["source"], "|", d["url"])
        for d in digest:
            print("digest  :", d["title"], "|", d["source"], "|", d["url"])
        return

    if paused:
        loud, digest = [], []
    for d in loud:
        tg_send(format_deal(d, now), button=("Open course", d["url"]))
        state["sent"].append({"t": d["title"][:80], "s": d["source"]})
    state["sent"] = state["sent"][-200:]
    if loud:
        history = load_json(HISTORY_FILE, [])
        history = history if isinstance(history, list) else []
        known = {h.get("url") for h in history if isinstance(h, dict)}
        for d in loud:
            if d["url"] in known:
                continue
            listed = (d.get("meta") or {}).get("listed_at")
            history.append({"title": d["title"], "url": d["url"], "source": d["source"],
                            "at": now.isoformat(timespec="seconds"),
                            "listed": listed.isoformat(timespec="seconds") if listed else "",
                            "note": d.get("note", "")})
        HISTORY_FILE.write_text(json.dumps(history[-300:], indent=1, ensure_ascii=False), encoding="utf-8")
    try:
        hist2 = load_json(HISTORY_FILE, [])
        if isinstance(hist2, list) and track_expiry(hist2, now, int(cfg.get("max_expiry_checks", 10))):
            HISTORY_FILE.write_text(json.dumps(hist2[-300:], indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print("[expiry] check failed:", type(exc).__name__, exc)

    chunk = cfg.get("digest_chunk", 10)
    for i in range(0, len(digest), chunk):
        part = digest[i:i + chunk]
        last = i + chunk >= len(digest)
        tg_send(format_digest(part, i, len(digest), dropped if last else 0), silent=True, preview=False)

    # source health + weekly summary (state.json is written only when something changed)
    state_changed = bool(loud) or json.dumps(state["stats_refs"], sort_keys=True) != stats_before
    state_changed |= update_health(state, results, cfg, lambda m: tg_send(m, preview=False))
    state_changed |= maybe_weekly_summary(state, cfg, lambda m: tg_send(m, silent=True, preview=False))
    if state_changed:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")

    # remember everything we looked at so nothing is repeated later
    seen = list(dict.fromkeys(seen + sorted(newly_seen)))[-8000:]
    SEEN_FILE.write_text(json.dumps(seen, indent=0), encoding="utf-8")
    TRACKED_FILE.write_text(json.dumps(tracked[-8000:], indent=1, ensure_ascii=False), encoding="utf-8")

    if names and len(errors) == len(names):
        raise SystemExit("All sources failed.")


if __name__ == "__main__":
    main()
