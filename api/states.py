"""
Vercel serverless function  ->  GET /api/states

Gives the DEPLOYED site real aircraft data. The browser can't call these
APIs directly (no CORS), so this function fetches server-side and returns
JSON same-origin, in OpenSky's { time, states:[[...]] } shape.

Sources, in order:
  1. OpenSky (authenticated) — only when OPENSKY_CLIENT_ID / _SECRET are
     set as Vercel env vars. Best data (includes origin country).
  2. adsb.fi — a free, keyless community ADS-B feed. This is what lets the
     hosted site show REAL traffic with no credentials at all. Its rows
     are converted into OpenSky's array format so the client needs no
     changes. (Anonymous OpenSky is skipped here because OpenSky throttles
     shared datacenter IPs like Vercel's.)

A shared in-memory cache means every visitor rides one upstream request
per ~8 s, and the last good data is served if a source momentarily fails.
Stdlib only — no requirements.txt.
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
OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSBFI_URL = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}"
DEFAULT_BBOX = "lamin=33.0&lamax=33.7&lomin=126.0&lomax=127.0"

FRESH_TTL = 8      # serve cache without re-fetching
STALE_MAX = 300    # on failure, serve stale cache up to this age

CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")

FT_TO_M = 0.3048
KT_TO_MS = 0.514444
FTMIN_TO_MS = 0.00508

_token = {"value": None, "exp": 0.0}
_cache = {}   # query -> { body, ts }


def _bbox(query):
    p = urllib.parse.parse_qs(query)
    g = lambda k, d: float(p.get(k, [d])[0])
    return g("lamin", "33.0"), g("lamax", "33.7"), g("lomin", "126.0"), g("lomax", "127.0")


# ---- source 1: OpenSky (authenticated only) -----------------------------
def _get_token():
    if not (CLIENT_ID and CLIENT_SECRET):
        return None
    if _token["value"] and time.time() < _token["exp"] - 30:
        return _token["value"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.load(r)
    _token["value"] = d["access_token"]
    _token["exp"] = time.time() + int(d.get("expires_in", 1800))
    return _token["value"]


def fetch_opensky(query, token):
    req = urllib.request.Request(
        OPENSKY_URL + "?" + query,
        headers={"Authorization": "Bearer " + token,
                 "User-Agent": "jeju-airspace/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read()
    if b"too many requests" in raw.lower():
        raise RuntimeError("rate")
    data = json.loads(raw or b"{}")
    data["_auth"] = True
    return data


# ---- source 2: adsb.fi (keyless) → OpenSky shape ------------------------
def fetch_adsbfi(query):
    lamin, lamax, lomin, lomax = _bbox(query)
    clat, clon = (lamin + lamax) / 2, (lomin + lomax) / 2
    url = ADSBFI_URL.format(lat=round(clat, 4), lon=round(clon, 4), nm=60)
    req = urllib.request.Request(
        url, headers={"User-Agent": "jeju-airspace/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read() or b"{}")

    states = []
    for a in (d.get("aircraft") or []):
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue
        if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
            continue  # keep only aircraft inside the requested box
        alt = a.get("alt_baro")
        on_ground = (alt == "ground")
        alt_m = 0.0 if on_ground else (alt * FT_TO_M if isinstance(alt, (int, float)) else None)
        geom = a.get("alt_geom")
        geom_m = geom * FT_TO_M if isinstance(geom, (int, float)) else None
        gs = a.get("gs")
        vel = gs * KT_TO_MS if isinstance(gs, (int, float)) else None
        vr = a.get("baro_rate", a.get("geom_rate"))
        vr_ms = vr * FTMIN_TO_MS if isinstance(vr, (int, float)) else None

        s = [None] * 17
        s[0] = (a.get("hex") or "").strip()          # icao24
        s[1] = (a.get("flight") or "").strip()        # callsign
        s[2] = a.get("flag") or ""                    # origin country (n/a)
        s[5] = lon
        s[6] = lat
        s[7] = alt_m                                  # baro altitude (m)
        s[8] = on_ground
        s[9] = vel                                    # velocity (m/s)
        s[10] = a.get("track")                        # true track
        s[11] = vr_ms                                 # vertical rate (m/s)
        s[13] = geom_m                                # geo altitude (m)
        states.append(s)

    return {"time": int(d.get("now") or time.time()),
            "states": states, "_auth": False, "_src": "adsb.fi"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query or DEFAULT_BBOX
        now = time.time()
        cached = _cache.get(query)

        if cached and now - cached["ts"] < FRESH_TTL:
            return self._send(200, cached["body"])

        data = None
        # 1) authenticated OpenSky (only if credentials are configured)
        try:
            token = _get_token()
            if token:
                data = fetch_opensky(query, token)
        except Exception:
            data = None
        # 2) keyless adsb.fi fallback
        if data is None:
            try:
                data = fetch_adsbfi(query)
            except Exception:
                data = None

        if data is not None:
            body = json.dumps(data).encode()
            _cache[query] = {"body": body, "ts": now}
            return self._send(200, body)

        # 3) all sources down — serve recent real data if we have it
        if cached and now - cached["ts"] < STALE_MAX:
            return self._send(200, cached["body"])
        return self._send(429, json.dumps(
            {"error": "flight data temporarily unavailable"}).encode())

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
