import os
import re
import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


MEETUP_URL = "https://www.meetup.com/find/?dateRange=startingSoon&source=EVENTS&eventType=online"

# RSS metadata
FEED_TITLE = "Meetup (Online) — Starting Soon (Next Hour)"
FEED_LINK = MEETUP_URL
FEED_DESCRIPTION = "Auto-generated RSS for Meetup online events starting within the next hour."

OUT_DIR = "public"
OUT_FILE = os.path.join(OUT_DIR, "feed.xml")

LOCAL_TZ = ZoneInfo("America/Toronto")
WINDOW_MINUTES = 60
MAX_ITEMS = 40


@dataclass
class EventItem:
    title: str
    link: str
    when_text: str
    start_dt: datetime | None
    attendees: int | None


def esc(s: str) -> str:
    return html.escape((s or "").strip())


def rfc2822(dt: datetime) -> str:
    # Ensure timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def parse_attendees(text: str) -> int | None:
    if not text:
        return None
    # Common patterns: "12 attendees", "12 going", "12 RSVPs", "12 people attending"
    m = re.search(r"\b(\d{1,5})\s*(attendees|going|rsvps|people|attending)\b", text, re.I)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def parse_start_datetime(text: str) -> datetime | None:
    """
    Best-effort parsing of Meetup time strings.
    Handles:
      - ISO datetime found in <time datetime="...">
      - "Today 7:30 PM", "Tomorrow 9:00 AM"
      - Some relative patterns like "in 30 minutes" (if present)
    """
    if not text:
        return None

    t = re.sub(r"\s+", " ", text).strip()

    # Relative: "in 30 minutes", "in 1 hour"
    rel = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours)\b", t, re.I)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        base = now_local()
        if "hour" in unit:
            return base + timedelta(hours=n)
        return base + timedelta(minutes=n)

    # Replace "Today"/"Tomorrow" with actual dates for easier parsing
    base = now_local()
    if re.search(r"\btoday\b", t, re.I):
        t2 = re.sub(r"\btoday\b", base.strftime("%Y-%m-%d"), t, flags=re.I)
    elif re.search(r"\btomorrow\b", t, re.I):
        t2 = re.sub(r"\btomorrow\b", (base + timedelta(days=1)).strftime("%Y-%m-%d"), t, flags=re.I)
    else:
        t2 = t

    try:
        dt = dateparser.parse(t2)
        if dt is None:
            return None
        # Assume local tz if none
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt = dt.astimezone(LOCAL_TZ)
        return dt
    except Exception:
        return None


def within_next_hour(dt: datetime | None) -> bool:
    if dt is None:
        return False
    start = now_local()
    end = start + timedelta(minutes=WINDOW_MINUTES)
    return start <= dt <= end


def build_rss(items: list[EventItem]) -> str:
    last_build = rfc2822(datetime.now(timezone.utc))

    rss_items = []
    for it in items:
        title = esc(it.title)
        link = esc(it.link)
        when_text = esc(it.when_text)
        attendees_text = ""
        if it.attendees is not None:
            attendees_text = f"<p><b>Attendees:</b> {it.attendees}</p>"

        when_block = f"<p><b>Time:</b> {when_text}</p>" if when_text else ""
        desc = f"<![CDATA[{when_block}{attendees_text}<p><a href=\"{link}\">Open event</a></p>]]>"

        pubdate = ""
        if it.start_dt:
            pubdate = f"<pubDate>{rfc2822(it.start_dt.astimezone(timezone.utc))}</pubDate>"

        rss_items.append(
            f"""<item>
  <title>{title}</title>
  <link>{link}</link>
  <guid isPermaLink="true">{link}</guid>
  {pubdate}
  <description>{desc}</description>
</item>"""
        )

    body = "\n".join(rss_items)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{esc(FEED_TITLE)}</title>
  <link>{esc(FEED_LINK)}</link>
  <description>{esc(FEED_DESCRIPTION)}</description>
  <lastBuildDate>{last_build}</lastBuildDate>
  <ttl>15</ttl>
{body}
</channel>
</rss>
"""


def normalize_link(href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("/"):
        return "https://www.meetup.com" + href
    return href


def scrape_events() -> list[EventItem]:
    """
    Render the page (lazy-loaded) and extract event cards best-effort.
    Meetup DOM changes often, so we:
      - locate anchors linking to /events/
      - get nearest container text for time/attendees
      - also try <time datetime="..."> when available
    """
    found: list[EventItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 2000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
        )

        page.goto(MEETUP_URL, wait_until="domcontentloaded", timeout=120000)

        # Let JS render initial cards
        page.wait_for_timeout(2500)

        # Scroll to trigger lazy-load
        for _ in range(10):
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(1100)

        # Primary: anchors that look like event links
        anchors = page.locator("a[href*='/events/']")
        total = min(anchors.count(), 120)

        seen_links = set()

        for i in range(total):
            a = anchors.nth(i)
            href = normalize_link(a.get_attribute("href") or "")
            if not href or href in seen_links:
                continue

            # Container around the anchor (event card region)
            container = a.locator("xpath=ancestor::*[self::article or self::li or self::div][1]")

            # Title: try aria-label, then inner text line 1
            title = (a.get_attribute("aria-label") or "").strip()
            if not title:
                try:
                    title = (a.inner_text() or "").strip().split("\n")[0].strip()
                except Exception:
                    title = ""

            if not title or len(title) < 3:
                continue

            container_text = ""
            try:
                container_text = (container.inner_text() or "").strip()
            except Exception:
                container_text = title

            # Try to get a precise datetime from a <time> element inside the container
            start_dt = None
            when_text = ""

            try:
                time_el = container.locator("time").first
                if time_el.count() > 0:
                    dt_attr = (time_el.get_attribute("datetime") or "").strip()
                    when_text = (time_el.inner_text() or "").strip()
                    if dt_attr:
                        try:
                            parsed = dateparser.parse(dt_attr)
                            if parsed:
                                if parsed.tzinfo is None:
                                    parsed = parsed.replace(tzinfo=LOCAL_TZ)
                                else:
                                    parsed = parsed.astimezone(LOCAL_TZ)
                                start_dt = parsed
                        except Exception:
                            pass
            except Exception:
                pass

            # If no <time datetime>, heuristically pick a line that looks like time/day
            if start_dt is None:
                lines = [ln.strip() for ln in container_text.split("\n") if ln.strip()]
                # Look for any line that includes AM/PM or weekday
                for ln in lines[:15]:
                    if re.search(r"\b(AM|PM)\b", ln) or re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", ln, re.I):
                        when_text = ln
                        break
                start_dt = parse_start_datetime(when_text)

            attendees = parse_attendees(container_text)

            seen_links.add(href)

            found.append(
                EventItem(
                    title=title,
                    link=href,
                    when_text=when_text,
                    start_dt=start_dt,
                    attendees=attendees,
                )
            )

        browser.close()

    # Filter to next hour
    upcoming = [e for e in found if within_next_hour(e.start_dt)]

    # Sort by start time
    upcoming.sort(key=lambda x: x.start_dt or datetime.max.replace(tzinfo=LOCAL_TZ))

    return upcoming[:MAX_ITEMS]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    try:
        items = scrape_events()
    except PlaywrightTimeoutError:
        items = []

    rss = build_rss(items)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"Wrote {OUT_FILE} with {len(items)} items (next {WINDOW_MINUTES} minutes).")


if __name__ == "__main__":
    main()
