import os, time, requests

BRAND_NAME = "EnaffCoins"
BRAND_AVATAR = os.environ.get("BRAND_AVATAR_URL")

def get_destinations(test_url=None):
    if test_url:
        return [(test_url, None)]
    dests = [
        (os.environ.get("DISCORD_WEBHOOK_URL"),   os.environ.get("DISCORD_ROLE_ID")),
        (os.environ.get("DISCORD_WEBHOOK_URL_2"), os.environ.get("DISCORD_ROLE_ID_2")),
    ]
    return [(u, r) for u, r in dests if u]

def _post(url, payload):
    for _ in range(3):
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate limiting")

def post_everywhere(payload, test_url=None, ping=True):
    payload = {"username": BRAND_NAME, **payload}
    if BRAND_AVATAR:
        payload["avatar_url"] = BRAND_AVATAR
    failures = []
    for url, role_id in get_destinations(test_url):
        try:
            _post(url, payload)
            if ping and role_id:
                _post(url, {"username": BRAND_NAME, "content": f"<@&{role_id}>",
                            "allowed_mentions": {"roles": [role_id]}})
        except Exception as e:
            failures.append(str(e))
    if failures:
        raise RuntimeError("Some posts failed: " + "; ".join(failures))
