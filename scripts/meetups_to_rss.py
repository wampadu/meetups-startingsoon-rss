import os
import re
import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright


MEETUP_URL = "https://www.meetup.com/find/?dateRange=startingSoon&source=EVENTS&eventType=online"

OUT_DIR = "public"
OUT_FILE = os.path.join(OUT_DIR, "feed.xml")

LOCAL_TZ = ZoneInfo("America/Toronto")
WINDOW_MINUTES = 60
MAX_ITEMS = 50

FEED_TITLE = "Meetup (Online) — Starting Soon (Next Hour)"
FEED_LINK = MEETUP_URL
FEED_DESCRIPTION = "Auto-generated RSS for Meetup online events starting within the next hour."

def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)

def rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

def esc(s: str) -> str:
    return html.escape((s or "").strip())

def extract_attendees_from_text(text: str):
    """
    Best-effort attendee extraction from a card's text content.
    Examples we may see:
      - "12 attendees"
      - "12 going"
      - "12 RSVPs"
      - "Attendees 12"
    """
    if not text:
        return None
    t = " ".join(text.split())
    m = re.search(r"\b(\d{1,6})\s*(attendees|going|rsvps|people|attending)\b", t, re.I)
    if m:
        try:
            return int(m.group(1))
        except:
            return None
    m2 = re.search(r"\battendees?\s*[:\-]?\s*(\d{1,6})\b", t, re.I)
    if m2:
        try:
            return int(m2.group(1))
        except:
            return None
    return None

def parse_start_dt(dt_attr: str, when_text: str):
    """
    Try to produce a timezone-aware local datetime.

    Priority:
      1) <time datetime="..."> attribute (usually ISO with timezone)
      2) text parsing (Today/Tomorrow/clock formats)
      3) relative text "in 30 minutes" / "in 1 hour"
    """
    base = now_local()

    # 1) datetime attribute - handle modern Meetup format with timezone
    if dt_attr:
        try:
            dt = dateparser.parse(dt_attr)
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=LOCAL_TZ)
                else:
                    dt = dt.astimezone(LOCAL_TZ)
                return dt
        except Exception as e:
            print(f"DEBUG: Failed to parse dt_attr '{dt_attr}': {e}")
            pass

    t = (when_text or "").strip()
    if not t:
        return None
    t_clean = re.sub(r"\s+", " ", t)

    # 3) relative
    rel = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours)\b", t_clean, re.I)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        if "hour" in unit:
            return base + timedelta(hours=n)
        return base + timedelta(minutes=n)

    # 2) Today/Tomorrow substitution
    if re.search(r"\btoday\b", t_clean, re.I):
        t_clean = re.sub(r"\btoday\b", base.strftime("%Y-%m-%d"), t_clean, flags=re.I)
    elif re.search(r"\btomorrow\b", t_clean, re.I):
        t_clean = re.sub(r"\btomorrow\b", (base + timedelta(days=1)).strftime("%Y-%m-%d"), t_clean, flags=re.I)

    # parse
    try:
        dt = dateparser.parse(t_clean)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt = dt.astimezone(LOCAL_TZ)
        return dt
    except Exception as e:
        print(f"DEBUG: Failed to parse when_text '{t_clean}': {e}")
        return None

def is_within_next_hour(start_dt: datetime | None, when_text: str) -> bool:
    """
    True if start_dt is within the next WINDOW_MINUTES.
    If start_dt is None, allow obvious "starting soon"/relative text as fallback.
    """    
    if start_dt:
        start = now_local()
        end = start + timedelta(minutes=WINDOW_MINUTES)
        is_within = start <= start_dt <= end
        if not is_within:
            print(f"DEBUG: {start_dt} not within {start} to {end}")
        return is_within

    # fallback if parsing failed
    t = (when_text or "").lower()
    if "starting soon" in t:
        return True
    if re.search(r"\bin\s+\d+\s+minutes?\b", t):
        return True
    if re.search(r"\bin\s+1\s+hour\b", t):
        return True
    return False

def build_rss(items):
    last_build = rfc2822(datetime.now(timezone.utc))

    rss_items = []
    for it in items:
        title = esc(it.get("title", ""))
        link = esc(it.get("url", ""))
        when_text = esc(it.get("when_text", ""))
        attendees = it.get("attendees")

        desc_parts = []
        if when_text:
            desc_parts.append(f"<p><b>Time:</b> {when_text}</p>")
        if attendees is not None:
            desc_parts.append(f"<p><b>Attendees:</b> {attendees}</p>")
        desc_parts.append(f"<p><a href=\"{link}\">Open event</a></p>")

        desc = "<![CDATA[" + "".join(desc_parts) + "]]>"

        pubdate = ""
        if it.get("start_dt_utc"):
            pubdate = f"<pubDate>{it['start_dt_utc']}</pubDate>"

        rss_items.append(f"<item>\n  <title>{title}</title>\n  <link>{link}</link>\n  <guid isPermaLink=\"true\">{link}</guid>\n  {pubdate}\n  <description>{desc}</description>\n</item>")

    body = "\n".join(rss_items)

    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\">
<channel>\n  <title>{esc(FEED_TITLE)}</title>\n  <link>{esc(FEED_LINK)}</link>\n  <description>{esc(FEED_DESCRIPTION)}</description>\n  <lastBuildDate>{last_build}</lastBuildDate>\n  <ttl>60</ttl>\n{body}\n</channel>\n</rss>"""

def scrape_meetup_cards():
    """
    Render the lazy page and extract event cards using modern Meetup DOM structure.
    Handles the current layout with data attributes and time elements with datetime.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 2200})

        print("DEBUG: Loading Meetup page...")
        page.goto(MEETUP_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3000)

        # Scroll to trigger lazy-load
        print("DEBUG: Scrolling to load more events...")
        prev_height = 0
        stable = 0
        for i in range(25):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1200)
            h = page.evaluate("document.body.scrollHeight")
            if h == prev_height:
                stable += 1
            else:
                stable = 0
                prev_height = h
            if stable >= 4:
                print(f"DEBUG: Page stable after {i+1} scrolls")
                break

        # Extract events using modern selectors
        print("DEBUG: Extracting event cards...")
        raw = page.evaluate(
            """
            () => {
              const out = [];
              const seen = new Set();

              function absUrl(href) {
                try { return new URL(href, location.origin).toString(); }
                catch(e) { return href || ""; }
              }

              // Look for event card containers - they have data-eventref attribute
              const cards = Array.from(document.querySelectorAll('[data-eventref]'));
              console.log('Found', cards.length, 'cards with data-eventref');

              for (const card of cards) {
                try {
                  // Get the event link
                  const link = card.querySelector('a[href*="/events/"]');
                  if (!link) continue;

                  const url = absUrl(link.getAttribute('href') || link.href || '');
                  if (!url || seen.has(url)) continue;
                  seen.add(url);

                  // Title: look for h3 tag
                  let title = '';
                  const h3 = card.querySelector('h3');
                  if (h3) {
                    title = h3.innerText.trim();
                  }

                  if (!title || title.length < 3) continue;

                  // Time: extract from <time> element's datetime attribute
                  let whenText = '';
                  let dtAttr = '';
                  const timeEl = card.querySelector('time');
                  if (timeEl) {
                    whenText = (timeEl.innerText || '').trim();
                    dtAttr = (timeEl.getAttribute('datetime') || '').trim();
                  }

                  // Attendees: look for text like "X attendees"
                  const cardText = card.innerText || '';

                  out.push({
                    title,
                    url,
                    whenText,
                    dtAttr,
                    cardText
                  });
                } catch (e) {
                  console.error('Error processing card:', e);
                }
              }

              console.log('Extracted', out.length, 'events');
              return out;
            }
            """
        )

        browser.close()
        return raw

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    raw = scrape_meetup_cards()
    print(f"DEBUG: Found {len(raw)} raw cards from page")

    # Convert + filter
    items = []
    for r in raw:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        when_text = (r.get("whenText") or "").strip()
        dt_attr = (r.get("dtAttr") or "").strip()
        card_text = r.get("cardText") or ""

        if not title or not url:
            continue

        print(f"DEBUG: Processing event: {title}")
        print(f"       whenText: {when_text}")
        print(f"       dtAttr: {dt_attr}")

        attendees = extract_attendees_from_text(card_text)

        start_dt = parse_start_dt(dt_attr, when_text)
        print(f"       parsed_dt: {start_dt}")

        keep = is_within_next_hour(start_dt, when_text)
        if not keep:
            print(f"       FILTERED OUT: Not within next {WINDOW_MINUTES} minutes")
            continue

        print(f"       KEPT: attendees={attendees}")

        start_dt_utc = None
        if start_dt:
            start_dt_utc = rfc2822(start_dt.astimezone(timezone.utc))

        items.append({
            "title": title,
            "url": url,
            "when_text": when_text,
            "attendees": attendees,
            "start_dt": start_dt,
            "start_dt_utc": start_dt_utc,
        })

    # Sort by start_dt (unknown times last)
    far = datetime.max.replace(tzinfo=LOCAL_TZ)
    items.sort(key=lambda x: x["start_dt"] if x["start_dt"] else far)

    # Cap
    items = items[:MAX_ITEMS]

    rss = build_rss(items)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)

    print(f"Wrote {OUT_FILE} with {len(items)} items (next {WINDOW_MINUTES} minutes).")
    # Helpful debug in Actions logs:
    for it in items[:10]:
        print("EVENT:", it["title"], "|", it["when_text"], "| attendees:", it["attendees"], "| start_dt:", it["start_dt"])\n
if __name__ == "__main__":
    main()
