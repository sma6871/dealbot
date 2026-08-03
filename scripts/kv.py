"""Minimal Cloudflare KV client over the REST API.

The Worker and this script share state through KV, since a Git repo can't act
as a live database for two writers.
"""

import json
import os
from urllib.parse import quote

import requests

TIMEOUT = 30


def _base():
    account = os.environ["CF_ACCOUNT_ID"]
    namespace = os.environ["CF_KV_NAMESPACE_ID"]
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{account}"
        f"/storage/kv/namespaces/{namespace}"
    )


def _headers():
    return {"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"}


def get_json(key, default=None):
    """Fetch and parse a key. A missing key returns the default, not an error."""
    r = requests.get(
        f"{_base()}/values/{quote(key, safe='')}", headers=_headers(), timeout=TIMEOUT
    )
    if r.status_code == 404:
        return default
    r.raise_for_status()
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        return default


def put_json(key, value, expiration_ttl=None):
    """Store a value as JSON. expiration_ttl is in seconds, minimum 60."""
    params = {}
    if expiration_ttl:
        params["expiration_ttl"] = max(60, int(expiration_ttl))
    r = requests.put(
        f"{_base()}/values/{quote(key, safe='')}",
        headers=_headers(),
        params=params,
        files={
            "value": (None, json.dumps(value, ensure_ascii=False)),
            "metadata": (None, "{}"),
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return True


def delete(key):
    r = requests.delete(
        f"{_base()}/values/{quote(key, safe='')}", headers=_headers(), timeout=TIMEOUT
    )
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True
