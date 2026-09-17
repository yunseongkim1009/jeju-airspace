"""
Vercel serverless function  ->  GET /api/states

Gives the DEPLOYED site real OpenSky data. The browser can't call OpenSky
directly (no CORS), so this function fetches it server-side and returns
JSON same-origin. It:

  * uses OpenSky OAuth2 (client-credentials) when OPENSKY_CLIENT_ID /
    OPENSKY_CLIENT_SECRET are set as Vercel env vars — otherwise anonymous;
  * caches the last response IN MEMORY so every visitor shares a single
    upstream request per ~8 s (this is what keeps anonymous access under
    OpenSky's rate limit no matter how many people open the page);
  * serves the last good REAL data if OpenSky momentarily rate-limits,
    so the map keeps showing actual aircraft instead of falling to demo.

Stdlib only — no requirements.txt needed.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
STATES_URL = "https://opensky-network.org/api/states/all"
DEFAULT_BBOX = "lamin=33.0&lamax=33.7&lomin=126.0&lomax=127.0"

FRESH_TTL = 8      # seconds: serve cache without re-fetching
STALE_MAX = 300    # seconds: on upstream failure, serve stale cache up to here

CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")

# Module-level state persists while the serverless instance stays warm
_token = {"value": None, "exp": 0.0}
_cache = {}   # query-string -> { "body": bytes, "ts": float }


def _get_token():
    """Cached OAuth2 access token, or None when running anonymously."""
    if not (CLIENT_ID and CLIENT_SECRET):
        return None
    if _token["value"] and time.time() < _token["exp"] - 30:
        return _token["value"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.load(r)
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 1800))
    return _token["value"]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query or DEFAULT_BBOX
        now = time.time()
        cached = _cache.get(query)

        # 1) Fresh cache — return immediately (shared across all visitors)
        if cached and now - cached["ts"] < FRESH_TTL:
            return self._send(200, cached["body"])

        # 2) Fetch from OpenSky (authenticated if creds present)
        try:
            token = _get_token()
            headers = {"User-Agent": "jeju-airspace/1.0"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(STATES_URL + "?" + query, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read()
            if b"too many requests" in raw.lower():
                raise urllib.error.HTTPError(STATES_URL, 429, "rate", None, None)
            data = json.loads(raw or b"{}")
            data["_auth"] = bool(token)
            body = json.dumps(data).encode()
            _cache[query] = {"body": body, "ts": now}
            return self._send(200, body)

        except Exception:
            # 3) Upstream failed — serve last good REAL data if recent enough
            if cached and now - cached["ts"] < STALE_MAX:
                return self._send(200, cached["body"])
            return self._send(429, json.dumps(
                {"error": "OpenSky rate limit / unavailable"}).encode())

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
