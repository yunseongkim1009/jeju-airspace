#!/usr/bin/env python3
"""
Jeju Airspace — local server with OpenSky OAuth2.

Why this exists
---------------
OpenSky's REST API does NOT send permissive CORS headers, so a browser
page can't call it directly. It also moved to OAuth2 (client-credentials)
for authenticated access, which raises your rate limit well above the
flaky anonymous tier. This tiny zero-dependency server solves both:

  * It serves index.html (and the rest of the folder) as static files.
  * It exposes  GET /api/states?<bbox>  which:
        1. fetches an OAuth2 access token (cached until it expires),
        2. calls OpenSky with the Bearer token,
        3. returns the JSON to the browser, same-origin (no CORS pain).

Your client secret stays on this machine — it's never sent to the page.

Setup
-----
1. Create an API client at https://opensky-network.org/ :
      Account  ->  API Client  ->  create  ->  copy client_id + secret
2. Run with your credentials (either env vars or edit the constants below):

      OPENSKY_CLIENT_ID=xxxx OPENSKY_CLIENT_SECRET=yyyy python3 server.py

   Then open  http://localhost:5390/

Running WITHOUT credentials still works — the server falls back to
anonymous OpenSky (same rate limits as the public proxy, but no CORS
issues). The dashboard also has its own proxy + demo fallbacks.
"""

import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ config
PORT = int(os.environ.get("PORT", "5390"))

CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "")       # or paste here
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")  # or paste here

TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
STATES_URL = "https://opensky-network.org/api/states/all"

# Default bounding box (used only if the request omits one)
DEFAULT_BBOX = "lamin=33.0&lamax=33.7&lomin=126.0&lomax=127.0"

# ------------------------------------------------------------ token cache
_token = {"value": None, "exp": 0.0}


def get_token():
    """Return a cached OAuth2 access token, refreshing shortly before it
    expires. Returns None when no credentials are configured (anonymous)."""
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
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 1800))
    print("[auth] obtained OpenSky token (expires in "
          f"{int(data.get('expires_in', 1800))}s)")
    return _token["value"]


# --------------------------------------------------------------- handler
class Handler(SimpleHTTPRequestHandler):
    # Silence the default noisy logging; keep our own concise lines
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/states":
            return self._states()
        return super().do_GET()

    def _states(self):
        # Forward the bounding box the client asked for (or the default)
        query = self.path.split("?", 1)[1] if "?" in self.path else DEFAULT_BBOX
        url = STATES_URL + "?" + query
        headers = {"User-Agent": "jeju-airspace/1.0"}

        try:
            token = get_token()
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read() or b"{}")
            # Tag the payload with how we fetched it (our own extra key;
            # the client ignores unknown keys). Handy for the UI badge.
            data["_auth"] = bool(token)
            payload = json.dumps(data).encode()
            self._send_json(200, payload)
            n = len((data or {}).get("states") or [])
            print(f"[states] 200 · {n} aircraft · "
                  f"{'auth' if token else 'anon'}")
        except urllib.error.HTTPError as e:
            # 429 = rate limited; surface the code so the client can react
            msg = "rate limit" if e.code == 429 else f"OpenSky HTTP {e.code}"
            self._send_json(e.code if e.code in (429,) else 502,
                            json.dumps({"error": msg}).encode())
            print(f"[states] error {e.code}: {msg}")
        except Exception as e:  # network, timeout, token failure, etc.
            self._send_json(502, json.dumps({"error": str(e)}).encode())
            print(f"[states] error: {e}")

    def _send_json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    mode = "AUTHENTICATED (OAuth2)" if (CLIENT_ID and CLIENT_SECRET) else "anonymous"
    print("=" * 54)
    print(f"  Jeju Airspace server  ·  {mode}")
    print(f"  Open  http://localhost:{PORT}/")
    if mode == "anonymous":
        print("  (set OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET for")
        print("   authenticated access + higher rate limits)")
    print("=" * 54)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
