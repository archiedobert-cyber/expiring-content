"""
FC 26 Expiry Checker
---------------------
Posts a daily Discord digest of expiring SBCs, Objectives, and Evolutions.

Usage:
    python fc26_expiry.py [--force]

State (state.json) tracks whether today's digest has already been sent so a
manual re-run or an accidental double-trigger won't spam duplicate posts.
Pass --force to bypass that check when testing.
"""

import os
import re
import sys
import json
from datetime import datetime, timezone

import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID")
BOT_USERNAME = "Expiring Content Bot"

SBC_URL = "https://www.fut.gg/sbc/category/expiring-soon/"
OBJECTIVES_URL = "https://www.fut.gg/objectives/expiring-soon/"
EVOLUTIONS_URL = "https://www.fut.gg/evolutions/"

STATE_FILE = "state.json"

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


def fetch_evolutions():
    label = "Evolutions"
    print(f"Fetching {label} from {EVOLUTIONS_URL} ...")
    try:
        html = fetch_page(EVOLUTIONS_URL)
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
    return [
        f"• [{item['name']}]({item['url']}) — {format_time_remaining(item['end_time'], now)} left"
        for item in items
    ]


def build_digest_message(sbcs, objectives, evolutions, now):
    lines = []

    lines.append("**🧩 SBCs expiring today**")
    if sbcs:
        lines.extend(format_items(sbcs, now))
    else:
        lines.append(f"Nothing expiring in the next 24 hours — [check what's expiring soon here]({SBC_URL})")
    lines.append("")

    lines.append("**🎯 Objectives expiring today**")
    if objectives:
        lines.extend(format_items(objectives, now))
    else:
        lines.append(f"Nothing expiring in the next 24 hours — [check what's expiring soon here]({OBJECTIVES_URL})")
    lines.append("")

    lines.append("**🧬 Evolutions expiring today**")
    if evolutions:
        lines.extend(format_items(evolutions, now))
    else:
        lines.append(f"Nothing expiring in the next 24 hours — [check what's expiring soon here]({EVOLUTIONS_URL})")

    return "\n".join(lines)


def send_to_discord(content, title):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not configured.")
    payload = {
        "username": BOT_USERNAME,
        "embeds": [{
            "title": title,
            "description": content[:4000],
            "footer": {"text": BOT_USERNAME},
        }],
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def send_role_ping():
    """Sends a short follow-up message pinging the alert role, right after
    the digest embed, so it appears just below it in the channel."""
    if not ROLE_ID:
        print("DISCORD_ROLE_ID not set — skipping role ping.")
        return
    payload = {
        "username": BOT_USERNAME,
        "content": f"<@&{ROLE_ID}>",
        "allowed_mentions": {"parse": ["roles"]},
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    response.raise_for_status()


def main():
    force = "--force" in sys.argv[1:]

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    if not force and state.get("last_digest_date") == today:
        print(f"Digest already sent today ({today}), skipping to avoid a duplicate.")
        print("(Re-run with the 'force' option checked to bypass this for testing.)")
        return

    all_sbcs = fetch_items(SBC_URL, "SBCs", SBC_PATTERN)
    all_objectives = fetch_items(OBJECTIVES_URL, "Objectives", OBJECTIVE_PATTERN, default_url=OBJECTIVES_URL)
    all_evolutions = fetch_evolutions()

    window = DIGEST_WINDOW_HOURS * 3600
    sbcs = filter_within(all_sbcs, now, window)
    objectives = filter_within(all_objectives, now, window)
    evolutions = filter_within(all_evolutions, now, window)
    content = build_digest_message(sbcs, objectives, evolutions, now)
    send_to_discord(content, "🔥 Today's Expiring Content")
    send_role_ping()

    state["last_digest_date"] = today
    save_state(state)
    print("Digest posted successfully.")


if __name__ == "__main__":
    main()
