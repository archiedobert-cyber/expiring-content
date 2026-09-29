"""
Expiring Content Checker  (redesigned layout)
----------------------------------------------
Posts a daily Discord digest of expiring SBCs, Objectives, and Evolutions.

Usage:
    python expiring.py [--force] [--dry-run]

Secrets / environment variables (set these as GitHub repository secrets):
    DISCORD_WEBHOOK_URL   the webhook the digest is posted to
    DISCORD_ROLE_ID       the role to ping under the digest (optional)

Test options (the two boxes on the workflow's "Run workflow" button):
    TEST_MODE=1           same as --force: send even if today's digest was
                          already sent
    TEST_URL=<page link>  post just that one page's section (an SBC,
                          Objectives or Evolutions page) and ignore the 24-hour
                          window. Nothing is saved to state.json, so it never
                          affects the real daily post.

State (state.json) tracks whether today's digest has already been sent so a
manual re-run or an accidental double-trigger won't spam duplicate posts.
Pass --force to bypass that check when testing.
Pass --dry-run (or set DRY_RUN=1) to print the message instead of posting it;
a dry run never touches state.json.
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID")
TEST_MODE = os.environ.get("TEST_MODE") == "1"
TEST_URL = os.environ.get("TEST_URL", "").strip()

BASE = "https://www.fut.gg"
SBC_URL = "https://www.fut.gg/sbc/category/expiring-soon/"
OBJECTIVES_URL = "https://www.fut.gg/objectives/expiring-soon/"
EVOLUTIONS_URL = "https://www.fut.gg/evolutions/"

STATE_FILE = "state.json"

# ---------------------------------------------------------------------------
# LOOK & FEEL - edit the text/emojis here
# ---------------------------------------------------------------------------
HEADER = "# 🔥📆 **TODAY'S EXPIRING CONTENT** 📆🔥"

SBC_TITLE = "🧩 SBCs"
OBJECTIVES_TITLE = "🎯 Objectives"
EVOLUTIONS_TITLE = "🧬 Evolutions"
MORE_INFO_TEXT = "more info"

EMPTY_TEXT = "Nothing expiring in the next 24 hours"
CARD_COLOUR = 0x2ECC71  # the green bar down the side of the card

# Each section's list is trimmed to this many characters and ends with
# "…and N more" if it runs long (a card holds 4096 characters in total).
MAX_LIST_CHARS = 1200

# ---------------------------------------------------------------------------

SBC_PATTERN = re.compile(
    r'slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)".{0,300}?endTime:"(?P<end_time>[^"]+)"'
    r'.{0,700}?url:"(?P<url>[^"]+)"',
    re.DOTALL,
)

OBJECTIVE_PATTERN = re.compile(
    r'slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)".{0,300}?endTime:"(?P<end_time>[^"]+)"',
    re.DOTALL,
)

EVOLUTION_ANCHOR = re.compile(
    r'url:"(?P<url>/evolutions/[^"]+)".{0,60}?slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)"',
    re.DOTALL,
)
EVOLUTION_MAX_GAP = 8000

DIGEST_WINDOW_HOURS = 24

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_page(url):
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def parse_end_time(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# SBC images  (same logic as the "New SBC" bot)
# ---------------------------------------------------------------------------
def page_text(page):
    """Raw page HTML with escaped JSON quotes un-escaped, for regex searching."""
    return str(page).replace('\\"', '"')


def find_player_card_image(page):
    """The actual player card artwork, when the SBC's reward is a player."""
    m = re.search(r'cardImageUrl:"([^"]+)"', page_text(page))
    return m.group(1).replace("\\/", "/") if m else None


def find_player_rating(page):
    """The overall rating of the reward player (e.g. 84), or 0 if not found.
    Uses the same page data the New SBC bot reads for 'Name - OVR - Rarity'."""
    m = re.search(r'overall:(\d+),commonName:"', page_text(page))
    return int(m.group(1)) if m else 0


IMAGE_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazy",
    "data-image",
    "data-url",
)


def find_image(card, url, page=None):
    """Find an SBC-specific image, preferring fut.gg's own SBC artwork."""

    # A player-card reward has its own artwork - use that in preference to
    # any generic SBC icon/thumbnail if we can find it.
    if page is not None:
        player_image = find_player_card_image(page)
        if player_image:
            return player_image

    def is_generic(src):
        src = src.lower()
        return (
            "fut-social" in src
            or "favicon" in src
            or "logo" in src
            or "placeholder" in src
            or "default-image" in src
        )

    def clean(src):
        if not src or src.startswith("data:"):
            return None
        src = urljoin(BASE, src.strip())
        if is_generic(src):
            return None
        return src

    def sources(img):
        """Every image URL an <img> tag might carry (src, lazy-load attrs, srcset)."""
        for attr in IMAGE_ATTRS:
            yield img.get(attr)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            for item in srcset.split(","):
                parts = item.strip().split()
                if parts:
                    yield parts[0]

    def first_image(container, must_contain=None):
        for img in container.find_all("img"):
            for src in sources(img):
                image = clean(src)
                if image and (must_contain is None or must_contain in image):
                    return image
        return None

    # 1) fut.gg's own artwork for this SBC (game-assets.fut.gg/.../sbcs/...)
    image = first_image(card, "/sbcs/")
    if image:
        return image

    if page is None:
        try:
            page = BeautifulSoup(fetch_page(url), "html.parser")
        except requests.RequestException as e:
            print(f"Could not fetch SBC page for image: {e}")

    if page is not None:
        image = first_image(page, "/sbcs/")
        if image:
            return image

    # 2) Any other image on the card
    image = first_image(card)
    if image:
        return image

    if page is not None:
        # 3) Page metadata (Open Graph, then Twitter/X)
        for attr_name, names in (
            ("property", ("og:image", "og:image:url")),
            ("name", ("twitter:image", "twitter:image:src")),
        ):
            for name in names:
                meta = page.find("meta", attrs={attr_name: name})
                if meta:
                    image = clean(meta.get("content"))
                    if image:
                        return image

        # 4) Any image on the SBC page
        return first_image(page)

    return None


def add_sbc_media(items):
    """Open each SBC's own page and work out its picture, exactly like the
    New SBC bot: a player-card reward counts as a player (large image),
    anything else counts as a thumbnail. Only used for the SBC section."""
    for item in items:
        item["media_kind"] = None
        item["media_url"] = None
        item["rating"] = 0
        try:
            page = BeautifulSoup(fetch_page(item["url"]), "html.parser")
        except requests.RequestException as e:
            print(f"Could not fetch SBC page {item['url']}: {e}")
            continue
        is_player = bool(find_player_card_image(page))
        item["rating"] = find_player_rating(page) if is_player else 0
        image = find_image(page, item["url"], page)
        if image:
            item["media_kind"] = "image" if is_player else "thumbnail"
            item["media_url"] = image
        print(f"  media for {item['name']}: {item['media_kind']} {item['media_url']} (rating {item['rating']})")
        time.sleep(0.5)  # be gentle with the site


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def extract_items(html, pattern, default_url=None):
    """Pull name/endTime (and url, if the pattern captures one) entries out
    of the raw HTML."""
    items = []
    seen = set()
    for m in pattern.finditer(html):
        try:
            end_dt = parse_end_time(m.group("end_time"))
        except ValueError:
            continue
        name = m.group("name")
        groups = m.groupdict()
        if groups.get("url"):
            raw_url = groups["url"]
            url = "https://www.fut.gg" + raw_url if raw_url.startswith("/") else raw_url
            dedup_key = url
        else:
            url = default_url
            dedup_key = (name, groups["end_time"])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        items.append({"name": name, "end_time": end_dt, "url": url})
    return items


def extract_evolutions(html):
    items = []
    seen = set()
    for m in EVOLUTION_ANCHOR.finditer(html):
        url_path = m.group("url")
        name = m.group("name")
        start_pos = m.end()
        idx = html.find('endTime:"', start_pos, start_pos + EVOLUTION_MAX_GAP)
        if idx == -1:
            continue
        val_start = idx + len('endTime:"')
        val_end = html.find('"', val_start)
        if val_end == -1:
            continue
        try:
            end_dt = parse_end_time(html[val_start:val_end])
        except ValueError:
            continue
        full_url = "https://www.fut.gg" + url_path
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append({"name": name, "end_time": end_dt, "url": full_url})
    return items


def fetch_evolutions(url=EVOLUTIONS_URL):
    label = "Evolutions"
    print(f"Fetching {label} from {url} ...")
    try:
        html = fetch_page(url)
        print(f"Fetched {label}: {len(html)} chars. Parsing ...")
        items = extract_evolutions(html)
        print(f"Parsed {label}: {len(items)} items found (before time filtering).")
        return items
    except Exception as exc:
        print(f"Warning: failed to fetch {label}: {exc}")
        return []


def fetch_items(url, label, pattern, default_url=None):
    print(f"Fetching {label} from {url} ...")
    try:
        html = fetch_page(url)
        print(f"Fetched {label}: {len(html)} chars. Parsing ...")
        items = extract_items(html, pattern, default_url)
        print(f"Parsed {label}: {len(items)} items found (before time filtering).")
        if not items:
            idxs = [m.start() for m in re.finditer(r'endTime:"', html)]
            print(f"DEBUG {label}: {len(idxs)} total 'endTime:\"' occurrences in fetched HTML.")
            if idxs:
                sample_idxs = {idxs[0], idxs[len(idxs) // 2], idxs[-1]}
                for i, idx in enumerate(sorted(sample_idxs)):
                    snippet = html[max(0, idx - 250):idx + 500]
                    print(f"DEBUG {label} sample {i + 1} (pos {idx}):\n{snippet}\n---")
            else:
                print(f"DEBUG {label}: 'endTime:\"' not found in fetched HTML at all.")
        return items
    except Exception as exc:
        print(f"Warning: failed to fetch {label}: {exc}")
        return []


def filter_within(items, now, window_seconds):
    return [
        item for item in items
        if 0 <= (item["end_time"] - now).total_seconds() <= window_seconds
    ]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_time_remaining(end_dt, now):
    remaining = end_dt - now
    total_minutes = int(remaining.total_seconds() // 60)
    if total_minutes <= 0:
        return "expired"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def item_text(item, now):
    """**Name** — 8h 53m left"""
    return f"**{item['name']}** — {format_time_remaining(item['end_time'], now)} left"


def format_items(items, now):
    items = sorted(items, key=lambda i: i["end_time"])
    return [f"• {item_text(item, now)}" for item in items]


def fit_lines(lines, max_chars):
    kept = []
    used = 0
    for shown, line in enumerate(lines):
        remaining_after = len(lines) - (shown + 1)
        reserve = len(f"\n…and {remaining_after} more") if remaining_after else 0
        if used + len(line) + 1 + reserve > max_chars:
            kept.append(f"…and {len(lines) - shown} more")
            return kept
        kept.append(line)
        used += len(line) + 1
    return kept


def build_section_text(title, more_info_url, items, now):
    """One section of the card: heading, 'more info' link, then the list."""
    lines = [f"## {title}", f"[{MORE_INFO_TEXT}]({more_info_url})"]
    if items:
        lines.extend(fit_lines(format_items(items, now), MAX_LIST_CHARS))
    else:
        lines.append(EMPTY_TEXT)
    return "\n".join(lines)


def pick_card_media(sbcs):
    """The whole card gets ONE picture, taken from the SBC section only.
    Player SBCs always win: if any expiring SBC is a player SBC, the
    highest-rated player's card is shown as the large image under everything
    (if ratings tie, the one expiring soonest wins). Otherwise the
    soonest-expiring SBC's artwork is used as the top-right thumbnail."""
    with_media = [
        i for i in sorted(sbcs or [], key=lambda i: i["end_time"])
        if i.get("media_url")
    ]
    players = [i for i in with_media if i["media_kind"] == "image"]
    if players:
        # max() keeps the first of equal ratings, and the list is already
        # sorted soonest-first, so ties go to the one expiring soonest.
        best = max(players, key=lambda i: i.get("rating", 0))
        return "image", best["media_url"]
    if with_media:
        return "thumbnail", with_media[0]["media_url"]
    return None, None


def build_payloads(sbcs, objectives, evolutions, now):
    """Builds ONE message containing ONE card with every section in it.
    A section passed as None is left out completely (used when testing one
    page). Returns a list so the sending code stays the same."""
    sections = []
    if sbcs is not None:
        sections.append(build_section_text(SBC_TITLE, SBC_URL, sbcs, now))
    if objectives is not None:
        sections.append(build_section_text(OBJECTIVES_TITLE, OBJECTIVES_URL, objectives, now))
    if evolutions is not None:
        sections.append(build_section_text(EVOLUTIONS_TITLE, EVOLUTIONS_URL, evolutions, now))

    embed = {
        "description": "\n\n".join(sections)[:4000],
        "color": CARD_COLOUR,
    }

    kind, media_url = pick_card_media(sbcs)
    if kind == "image":
        embed["image"] = {"url": media_url}          # big, under everything
    elif kind == "thumbnail":
        embed["thumbnail"] = {"url": media_url}      # small, top right

    return [{"content": HEADER, "embeds": [embed]}]


def post(payload):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not configured.")
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def send_payloads(payloads):
    for payload in payloads:
        post(payload)
        time.sleep(1)


def send_role_ping():
    if not ROLE_ID:
        print("DISCORD_ROLE_ID not set — skipping role ping.")
        return
    post({
        "content": f"<@&{ROLE_ID}>",
        "allowed_mentions": {"roles": [ROLE_ID]},
    })


def fetch_test_sections(url, now):
    lowered = url.lower()
    if "/objectives" in lowered:
        items = fetch_items(url, "Objectives", OBJECTIVE_PATTERN, default_url=OBJECTIVES_URL)
        section = "objectives"
    elif "/evolutions" in lowered:
        items = fetch_evolutions(url)
        section = "evolutions"
    else:
        items = fetch_items(url, "SBCs", SBC_PATTERN)
        section = "sbcs"

    items = [i for i in items if i["end_time"] > now]
    if section == "sbcs":
        add_sbc_media(items)
    print(f"Test page treated as {section}: {len(items)} item(s) not yet expired.")
    return (
        items if section == "sbcs" else None,
        items if section == "objectives" else None,
        items if section == "evolutions" else None,
    )


def main():
    args = sys.argv[1:]
    force = "--force" in args or TEST_MODE
    dry_run = "--dry-run" in args or os.environ.get("DRY_RUN") == "1"

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    if TEST_URL:
        print(f"TEST_URL set - posting just this page: {TEST_URL}")
        sbcs, objectives, evolutions = fetch_test_sections(TEST_URL, now)
        payloads = build_payloads(sbcs, objectives, evolutions, now)
        if dry_run:
            print("DRY RUN - nothing posted. Messages would be:")
            print(json.dumps(payloads, indent=2, ensure_ascii=False))
            return
        send_payloads(payloads)
        send_role_ping()
        print("Test post sent. state.json was not changed.")
        return

    if not force and not dry_run and state.get("last_digest_date") == today:
        print(f"Digest already sent today ({today}), skipping to avoid a duplicate.")
        print("(Tick the 'Test' box when you run the workflow to send it again.)")
        return

    all_sbcs = fetch_items(SBC_URL, "SBCs", SBC_PATTERN)
    all_objectives = fetch_items(OBJECTIVES_URL, "Objectives", OBJECTIVE_PATTERN, default_url=OBJECTIVES_URL)
    all_evolutions = fetch_evolutions()

    window = DIGEST_WINDOW_HOURS * 3600
    sbcs = filter_within(all_sbcs, now, window)
    objectives = filter_within(all_objectives, now, window)
    evolutions = filter_within(all_evolutions, now, window)

    add_sbc_media(sbcs)  # only the SBCs actually expiring get their pages opened

    payloads = build_payloads(sbcs, objectives, evolutions, now)

    if dry_run:
        print("DRY RUN - nothing posted. Messages would be:")
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return

    send_payloads(payloads)
    send_role_ping()

    state["last_digest_date"] = today
    save_state(state)
    print("Digest posted successfully.")


if __name__ == "__main__":
    main()
