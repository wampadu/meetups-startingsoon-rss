import os, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright

MEETUP_URL = "https://www.meetup.com/find/?dateRange=startingSoon&source=EVENTS&eventType=online"

OUT_DIR = "public"
FEED_PATH = os.path.join(OUT_DIR, "feed.xml")
DEBUG_HTML = os.path.join(OUT_DIR, "debug.html")
DEBUG_PNG  = os.path.join(OUT_DIR, "debug.png")

LOCAL_TZ = ZoneInfo("America/Toronto")
WINDOW_MINUTES = 60
MAX_ITEMS = 50

def esc(s): return html.escape((s or "").strip())

def rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

def now_local():
    return datetime.now(tz=LOCAL_TZ)

def parse_dt(dt_attr: str, when_text: str):
    # Prefer datetime attribute if present
    if dt_attr:
        try:
            dt = dateparser.parse(dt_attr)
            if dt:
                return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
        except:
            pass

    t = (when_text or "").strip()
    if not t:
        return None

    base = now_local()
    t = re.sub(r"\s+", " ", t)

    # Relative: "in 30 minutes"
    m = re.search(r"\bin\s+(\d{1,3})\s*(minute|minutes|hour|hours)\b", t, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return base + (timedelta(hours=n) if "hour" in unit else timedelta(minutes=n))

    # Today/Tomorrow normalization
    if re.search(r"\btoday\b", t, re.I):
        t = re.sub(r"\btoday\b", base.strftime("%Y-%m-%d"), t, flags=re.I)
    if re.search(r"\btomorrow\b", t, re.I):
        t = re.sub(r"\btomorrow\b", (base + timedelta(days=1)).strftime("%Y-%m-%d"), t, flags=re.I)

    try:
        dt = dateparser.parse(t)
        if not dt:
            return None
        return dt.astimezone(LOCAL_TZ) if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
    except:
        return None

def within_next_hour(dt: datetime | None, when_text: str) -> bool:
    if dt:
        start = now_local()
        end = start + timedelta(minutes=WINDOW_MINUTES)
        return start <= dt <= end

    # fallback if parsing failed but text implies soon
    t = (when_text or "").lower()
    return ("starting soon" in t) or bool(re.search(r"\bin\s+\d+\s+minutes?\b", t))

def extract_attendees(card_text: str):
    if not card_text:
        return None
    t = " ".join(card_text.split())
    m = re.search(r"\b
