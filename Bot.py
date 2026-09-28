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
from datetime import datetime, timezone

import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID")
TEST_MODE = os.environ.get("TEST_MODE") == "1"
TEST_URL = os.environ.get("TEST_URL", "").strip()
BOT_USERNAME = "Expiring Content Bot"

SBC_URL = "https://www.fut.gg/sbc/category/expiring-soon/"
OBJECTIVES_URL = "https://www.fut.gg/objectives/expiring-soon/"
EVOLUTIONS_URL = "https://www.fut.gg/evolutions/"

STATE_FILE = "state.json"

# ---------------------------------------------------------------------------
# LOOK & FEEL - edit the text/emojis here
# ---------------------------------------------------------------------------
# Big title shown above the card (Discord's "# " = largest heading size)
HEADER = "# 🔥📆 **TODAY'S EXPIRING CONTENT** 📆🔥"

# Section titles (Discord's "## " = next biggest size) and the page each
# "more info" link goes to.
SBC_TITLE = "🧩 SBCs expiring today"
OBJECTIVES_TITLE = "🎯 Objectives expiring today"
EVOLUTIONS_TITLE = "🧬 Evolutions expiring today"
MORE_INFO_TEXT = "more info"

EMPTY_TEXT = "Nothing expiring in the next 24 hours"
CARD_COLOUR = 0x2ECC71  # the green bar down the side of the card

# A Discord embed holds 4096 characters in total, so each section's list is
# trimmed to this many characters and ends with "…and N more" if it runs long.
MAX_LIST_CHARS = 1200
# ---------------------------------------------------------------------------

# SBCs: a slug field sits shortly before the real top-level name (nested
# reward objects never have their own slug), then endTime, then a real
# per-item url.
SBC_PATTERN = re.compile(
    r'slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)".{0,300}?endTime:"(?P<end_time>[^"]+)"'
    r'.{0,700}?url:"(?P<url>[^"]+)"',
    re.DOTALL,
)

# Objectives: same slug-before-name anchor, but no reliable per-item url
# field (they use a slug instead) — so we only extract name + endTime here,
# and link every objective to the category page instead of a specific item.
OBJECTIVE_PATTERN = re.compile(
    r'slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)".{0,300}?endTime:"(?P<end_time>[^"]+)"',
    re.DOTALL,
)

# Evolutions: url, slug and name sit tightly together at the start of each
# entry, but a large variable-length "trending players" list separates name
# from endTime — too variable for a fixed window, so we locate the anchor
# with regex and then search forward for the nearest endTime after it.
EVOLUTION_ANCHOR = re.compile(
    r'url:"(?P<url>/evolutions/[^"]+)".{0,60}?slug:"[^"]+".{0,60}?name:"(?P<name>[^"]+)"',
    re.DOTALL,
)
EVOLUTION_MAX_GAP = 8000  # generous cap so the forward search can't run away

DIGEST_WINDOW_HOURS = 24      # "expiring today" — covers everything from
                               # this run until the next daily run


def fetch_page(url):
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def parse_end_time(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def extract_items(html, pattern, default_url=None):
    """Pull name/endTime (and url, if the pattern captures one) entries out
    of the raw HTML. If a real url isn't captured, default_url is used for
    every item instead (e.g. linking to the category page)."""
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
            continue  # the same entry can appear twice in the embedded data
        seen.add(dedup_key)
        items.append({"name": name, "end_time": end_dt, "url": url})
    return items


def extract_evolutions(html):
    """Evolutions need a different approach from SBCs/Objectives: find each
    entry's name/url anchor with regex, then plain-search forward for its
    endTime rather than trying to bound a variable-length gap in the regex
    itself (the trending-players list in between can be huge)."""
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


def format_items(items, now):
    """Soonest-expiring first, e.g.  • [Name](link) — 8h 53m left"""
    items = sorted(items, key=lambda i: i["end_time"])
    return [
        f"• [{item['name']}]({item['url']}) — {format_time_remaining(item['end_time'], now)} left"
        for item in items
    ]


def fit_lines(lines, max_chars):
    """Keep as many whole lines as fit; end with '…and N more' if some don't."""
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


def build_section(title, more_info_url, items, now):
    """## Title
    [more info](url)
    • item — time left ..."""
    lines = [f"## {title}", f"[{MORE_INFO_TEXT}]({more_info_url})"]
    if items:
        lines.extend(fit_lines(format_items(items, now), MAX_LIST_CHARS))
    else:
        lines.append(EMPTY_TEXT)
    return "\n".join(lines)


def build_payload(sbcs, objectives, evolutions, now):
    # A section passed as None is left out completely (used when testing one page).
    sections = [
        (SBC_TITLE, SBC_URL, sbcs),
        (OBJECTIVES_TITLE, OBJECTIVES_URL, objectives),
        (EVOLUTIONS_TITLE, EVOLUTIONS_URL, evolutions),
    ]
    description = "\n\n".join(
        build_section(title, more_info_url, items, now)
        for title, more_info_url, items in sections
        if items is not None
    )
    return {
        "username": BOT_USERNAME,
        "content": HEADER,
        "embeds": [{"description": description, "color": CARD_COLOUR}],
    }


def send_to_discord(payload):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not configured.")
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def send_role_ping():
    """Sends a short follow-up message pinging the alert role, right after
    the digest, so it appears just below it in the channel."""
    if not ROLE_ID:
        print("DISCORD_ROLE_ID not set — skipping role ping.")
        return
    payload = {
        "username": BOT_USERNAME,
        "content": f"<@&{ROLE_ID}>",
        "allowed_mentions": {"roles": [ROLE_ID]},  # can only ping this one role
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def fetch_test_sections(url, now):
    """For TEST_URL: read just that one page and return (sbcs, objectives,
    evolutions) with the other two set to None so they're left out of the post.
    The section is judged from the address; anything already expired is skipped,
    but the 24-hour window is NOT applied so you can see the whole page."""
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
        payload = build_payload(sbcs, objectives, evolutions, now)
        if dry_run:
            print("DRY RUN - nothing posted. Message would be:")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        send_to_discord(payload)
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
    payload = build_payload(sbcs, objectives, evolutions, now)

    if dry_run:
        print("DRY RUN - nothing posted. Message would be:")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    send_to_discord(payload)
    send_role_ping()

    state["last_digest_date"] = today
    save_state(state)
    print("Digest posted successfully.")


if __name__ == "__main__":
    main()
