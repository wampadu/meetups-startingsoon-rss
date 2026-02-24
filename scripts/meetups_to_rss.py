# scripts/meetups_to_rss.py
# Generates an RSS feed (public/feed.xml) for Meetup "Starting Soon" online events.
# Uses Playwright to render the lazy-loaded DOM, extracts event title/time/attendees/link,
# and filters to events that appear to start within ~WINDOW_MINUTES (best-effort).
#
# Also publishes debug artifacts so you can inspect what the GitHub runner saw:
#   public/debug.html, public/debug.png, public/debug.json

import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

MEETUP_URL = "https://www.meetup.com/find/?dateRange=startingSoon&source=EVENTS&eventType=online"

OUT_DIR = "public"
FEED_PATH = os.path.join(OUT_DIR, "feed.xml")
DEBUG_HTML = os.path.join(OUT_DIR, "debug.html")
DEBUG_PNG = os.path.join(OUT_DIR, "debug.png")
DEBUG_JSON = os.path.join(OUT_DIR, "debug.json")

LOCAL_TZ = ZoneInfo("America/Toronto")

# Meetup "startingSoon" often does not provide strict timestamps on every card,
# so we use a wider window and a fallback that keeps "starting soon"/relative items.
WINDOW_MINUTES = 90
MAX_ITEMS = 50

FEED_TITLE = "Meetup (Online) — Starting Soon (Next ~90 Minutes)"
FEED_LINK = MEETUP_URL
FEED_DESCRIPTION = (
    "Auto-generated RSS for Meetup online events starting soon. "
    "Prefers next ~90 minutes when timestamps exist; otherwise uses relative-text fallbacks."
)


def esc(s: str) -> str:
    return html.escape((s or "").strip())


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def extract_attendees(card_text: str):
    """
    Best-effort attendee extraction from card text.
    Matches patterns like: "12 attendees", "12 going", "12 RSVPs", "12 attending".
    """
    if not card_text:
        return None
    t = " ".join(card_text.split())
    m = re.search(r"\b(\d{1,6})\s*(attendees|going|rsvps|people|attending)\b", t, re.I)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m2 = re.search(r"\battendees?\s*[:\-]?\s*(\d{1,6})\b", t, re.I)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            return None
    return None


def parse_dt(dt_attr: str, when_text: str):
    """
    Parse event start time using:
      1) <time datetime="..."> attribute if present (best)
      2) relative strings like "in 30 minutes"
      3) "Today"/"Tomorrow" date strings

    Returns timezone-aware datetime in LOCAL_TZ or None.
    """
    # 1) datetime attribute (often ISO)
    if dt_attr:
        try:
            dt = dateparser.parse(dt_attr)
            if dt:
                return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
        except Exception:
            pass

    t = (when_text or "").strip()
    if not t:
        return None

    base = now_local()
    t = re.sub(r"\s+", " ", t)

    # 2) relative: "in 15 minutes", "in 1 hour"
    rel = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours)\b", t, re.I)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        return base + (timedelta(hours=n) if "hour" in unit else timedelta(minutes=n))

    # 3) Today/Tomorrow normalization
    if re.search(r"\btoday\b", t, re.I):
        t = re.sub(r"\btoday\b", base.strftime("%Y-%m-%d"), t, flags=re.I)
    if re.search(r"\btomorrow\b", t, re.I):
        t = re.sub(r"\btomorrow\b", (base + timedelta(days=1)).strftime("%Y-%m-%d"), t, flags=re.I)

    # best-effort parse
    try:
        dt = dateparser.parse(t)
        if not dt:
            return None
        return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
    except Exception:
        return None


def within_window(dt: datetime | None, when_text: str) -> bool:
    """
    Smart filter:
      - If we have a parsed datetime: keep only if within WINDOW_MINUTES.
      - If not: keep if it looks like relative time within window or says "starting soon".
      - Final fallback: keep anyway because the page itself is "startingSoon" and we want a useful feed.
    """
    if dt:
        start = now_local()
        end = start + timedelta(minutes=WINDOW_MINUTES)
        return start <= dt <= end

    t = (when_text or "").lower().strip()

    if "starting soon" in t:
        return True

    m = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours)\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        minutes = n * 60 if "hour" in unit else n
        return minutes <= WINDOW_MINUTES

    # fallback: keep (otherwise you can get kept=0 even with many extracted cards)
    return True


def build_rss(items):
    last_build = rfc2822(datetime.now(timezone.utc))

    rss_items = []
    for it in items:
        title = esc(it.get("title", ""))
        link = esc(it.get("url", ""))
        when_text = esc(it.get("when_text", ""))
        attendees = it.get("attendees")

        desc = []
        if when_text:
            desc.append(f"<p><b>Time:</b> {when_text}</p>")
        if attendees is not None:
            desc.append(f"<p><b>Attendees:</b> {attendees}</p>")
        desc.append(f"<p><a href=\"{link}\">Open event</a></p>")

        pubdate = it.get("pubdate", last_build)

        rss_items.append(f"""<item>
  <title>{title}</title>
  <link>{link}</link>
  <guid isPermaLink="true">{link}</guid>
  <pubDate>{pubdate}</pubDate>
  <description><![CDATA[{''.join(desc)}]]></description>
</item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{esc(FEED_TITLE)}</title>
  <link>{esc(FEED_LINK)}</link>
  <description>{esc(FEED_DESCRIPTION)}</description>
  <lastBuildDate>{last_build}</lastBuildDate>
  <ttl>60</ttl>
{chr(10).join(rss_items)}
</channel>
</rss>
"""


def scrape_rendered_dom():
    """
    Render the lazy Meetup page on the runner and extract event-like cards via evaluate().
    Writes debug.html/debug.png/debug.json so you can inspect what loaded.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            viewport={"width": 1400, "height": 2200},
            locale="en-CA",
            timezone_id="America/Toronto",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
        )

        # Reduce obvious automation flags a bit
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        page = context.new_page()

        try:
            page.goto(MEETUP_URL, wait_until="domcontentloaded", timeout=120000)
        except PlaywrightTimeoutError:
            pass

        # Allow React to hydrate
        page.wait_for_timeout(6000)

        # Scroll to load more cards
        prev_height = 0
        stable = 0
        for _ in range(25):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1200)
            try:
                h = page.evaluate("document.body.scrollHeight")
            except Exception:
                h = prev_height
            if h == prev_height:
                stable += 1
            else:
                stable = 0
                prev_height = h
            if stable >= 5:
                break

        os.makedirs(OUT_DIR, exist_ok=True)

        # Debug artifacts (what the runner sees)
        try:
            with open(DEBUG_HTML, "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass

        try:
            page.screenshot(path=DEBUG_PNG, full_page=True)
        except Exception:
            pass

        raw = page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll("a[href*='/events/']"));
              const out = [];
              const seen = new Set();

              function absUrl(href) {
                try { return new URL(href, location.origin).toString(); }
                catch(e) { return href || ""; }
              }

              for (const a of anchors) {
                const url = absUrl(a.getAttribute("href") || a.href || "");
                if (!url || seen.has(url)) continue;
                seen.add(url);

                const card = a.closest("article") || a.closest("li") || a.closest("div");

                let title =
                  (card && card.querySelector("h3") && card.querySelector("h3").innerText) ||
                  a.getAttribute("aria-label") ||
                  a.innerText ||
                  "";

                title = (title || "").trim();
                if (!title || title.length < 3) continue;

                const timeEl = card ? card.querySelector("time") : null;
                const whenText = (timeEl && timeEl.innerText ? timeEl.innerText : "").trim();
                const dtAttr = (timeEl && timeEl.getAttribute("datetime") ? timeEl.getAttribute("datetime") : "").trim();

                const cardText = (card && card.innerText) ? card.innerText : (a.innerText || "");

                out.push({ title, url, whenText, dtAttr, cardText });
              }

              return {
                pageTitle: document.title,
                url: location.href,
                countAnchors: anchors.length,
                extracted: out.length,
                bodySnippet: (document.body && document.body.innerText) ? document.body.innerText.slice(0, 800) : "",
                events: out
              };
            }
            """
        )

        # Save debug JSON too
        try:
            with open(DEBUG_JSON, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        browser.close()
        return raw


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    raw = scrape_rendered_dom()

    count_anchors = raw.get("countAnchors", 0)
    extracted = raw.get("extracted", 0)
    events = raw.get("events", [])
    body_snip = (raw.get("bodySnippet") or "").lower()

    # If the runner got blocked, you'll usually see these signals
    blocked_signals = ["verify", "captcha", "robot", "unusual traffic", "enable javascript"]
    is_blocked = (count_anchors == 0 and extracted == 0) or any(s in body_snip for s in blocked_signals)

    items = []

    if is_blocked:
        items.append({
            "title": "⚠️ Meetup blocked or page did not render on GitHub runner",
            "url": MEETUP_URL,
            "when_text": f"anchors={count_anchors}, extracted={extracted}. Open /debug.png and /debug.html on GitHub Pages.",
            "attendees": None,
            "pubdate": rfc2822(datetime.now(timezone.utc)),
        })
    else:
        for e in events:
            title = (e.get("title") or "").strip()
            url = (e.get("url") or "").strip()
            when_text = (e.get("whenText") or "").strip()
            dt_attr = (e.get("dtAttr") or "").strip()
            card_text = e.get("cardText") or ""

            start_dt = parse_dt(dt_attr, when_text)
            if not within_window(start_dt, when_text):
                continue

            attendees = extract_attendees(card_text)

            pub = rfc2822(start_dt.astimezone(timezone.utc)) if start_dt else rfc2822(datetime.now(timezone.utc))

            items.append({
                "title": title,
                "url": url,
                "when_text": when_text if when_text else "Starting soon (time not provided on card)",
                "attendees": attendees,
                "pubdate": pub,
                "sort_dt": start_dt.isoformat() if start_dt else None,
            })

        # Sort known-datetime first, unknown last
        far = datetime.max.replace(tzinfo=LOCAL_TZ)

        def sort_key(it):
            if it.get("sort_dt"):
                try:
                    dt = dateparser.parse(it["sort_dt"])
                    return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
                except Exception:
                    return far
            return far

        items.sort(key=sort_key)
        items = items[:MAX_ITEMS]

        if not items:
            # Keep feed non-empty with a helpful diagnostic
            items.append({
                "title": "ℹ️ Events were extracted but time filtering kept 0",
                "url": MEETUP_URL,
                "when_text": f"anchors={count_anchors}, extracted={extracted}, kept=0. Check /debug.json for whenText/dtAttr.",
                "attendees": None,
                "pubdate": rfc2822(datetime.now(timezone.utc)),
            })

    rss = build_rss(items)
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        f.write(rss)

    # Logs in GitHub Actions
    print(f"[INFO] pageTitle={raw.get('pageTitle')}")
    print(f"[INFO] anchors={count_anchors}, extracted={extracted}, rss_items={len(items)}")
    print(f"[INFO] wrote: {FEED_PATH}")
    print(f"[INFO] debug: {DEBUG_HTML}, {DEBUG_PNG}, {DEBUG_JSON}")


if __name__ == "__main__":
    main()
```0
