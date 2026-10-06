#!/usr/bin/env python3
"""
Spotify OAuth Cache-Helper.
- Liest Credentials aus <BASE_DIR>/config/config.json
- Cached den Token nach <BASE_DIR>/data/.spotify_cache
Funktioniert aus jedem Working Directory heraus.
"""
import os
import sys
import json
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ------------------------------------------------------------------
# Pfad-Auflösung: BASE_DIR ermitteln (vishva.paths bevorzugt)
# ------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))      # .../vishva/scripts
BASE = os.path.dirname(HERE)                            # .../vishva
if BASE not in sys.path:
    sys.path.insert(0, BASE)

try:
    from vishva.paths import p, cfg_path
    CONFIG_PATH = p("config", "config.json")
except ImportError:
    CONFIG_PATH = os.path.join(BASE, "config", "config.json")

# ------------------------------------------------------------------
# Config laden
# ------------------------------------------------------------------
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

client_id     = config.get("spotify_client_id", "")
client_secret = config.get("spotify_client_secret", "")
redirect_uri  = config.get("spotify_redirect_uri", "http://127.0.0.1:8888/callback")

if not client_id or client_id == "YOUR_CLIENT_ID":
    print(f"❌ spotify_client_id fehlt in {CONFIG_PATH}")
    sys.exit(1)

# Cache-Pfad: config-Key spotify_cache_path, sonst data/.spotify_cache
try:
    CACHE_PATH = cfg_path(config, "spotify_cache_path",
                          os.path.join("data", ".spotify_cache"))
except NameError:  # falls vishva.paths nicht importiert wurde
    CACHE_PATH = os.path.join(BASE, "data", ".spotify_cache")
os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

print(f"📄 Config: {CONFIG_PATH}")
print(f"💾 Cache:  {CACHE_PATH}")

auth_code = None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auth erfolgreich! Du kannst das Fenster schliessen.")
        elif "error" in params:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Fehler: {params['error'][0]}".encode())
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Warte auf Spotify Auth...")

    def log_message(self, format, *args):
        pass  # kein HTTP-Logging


# ------------------------------------------------------------------
# Server starten + Auth-Flow
# ------------------------------------------------------------------
with socketserver.TCPServer(("127.0.0.1", 8888), Handler) as httpd:
    print("🌐 Server läuft auf http://127.0.0.1:8888")

    sp_oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="user-read-playback-state,user-modify-playback-state,"
              "user-read-currently-playing,playlist-read-private,user-library-read",
        cache_path=CACHE_PATH,
        open_browser=False
    )
    auth_url = sp_oauth.get_authorize_url()
    print("\n🔗 Öffne diese URL im Browser:")
    print(f"{auth_url}\n")

    # Warten auf den Callback-Request
    httpd.timeout = 120
    httpd.handle_request()

    if auth_code:
        print("✅ Code erhalten, hole Token...")
        token_info = sp_oauth.get_access_token(auth_code, as_dict=True)
        if token_info:
            print(f"✅ Auth erfolgreich! Gecached in {CACHE_PATH}")
            sp = spotipy.Spotify(auth_manager=sp_oauth)
            print(f"   👤 User: {sp.me()['display_name']}")
        else:
            print("❌ Token konnte nicht geholt werden.")
    else:
        print("❌ Kein Code erhalten.")
