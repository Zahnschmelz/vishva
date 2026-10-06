"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
import os
from ..paths import p, cfg_path, BASE_DIR
import re
import json
import time
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
try:
    import requests
except ImportError:
    requests = None

def _spotify(self, args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action", "")
    query = args.get("query", "")
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        return {"error": "spotipy not installed. Run: pip install spotipy"}
    client_id = self.config.get("spotify_client_id", "")
    client_secret = self.config.get("spotify_client_secret", "")
    redirect_uri = self.config.get("spotify_redirect_uri", "http://127.0.0.1:8888/callback")
    market = self.config.get("spotify_market", "DE")
    if not client_id or not client_secret:
        return {"error": "Spotify credentials missing."}
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id, client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="user-read-playback-state,user-modify-playback-state",
            cache_path=p("data", ".spotify_cache"), open_browser=False))
        if action == "play":
            if not query:
                return {"error": "query required for play."}
            results = sp.search(q=query, type="track", limit=1, market=market)
            tracks = results.get("tracks", {}).get("items", [])
            if not tracks:
                return {"error": f"No track: '{query}'"}
            track = tracks[0]
            uri = track["uri"]
            track_name = track["name"]
            artists = track.get("artists", [])
            artist_name = artists[0]["name"] if artists else "Unknown"
            artist_id = artists[0]["id"] if artists else None
            import time
            def _get_spotifyd_id():
                devs = sp.devices().get("devices", [])
                for d in devs:
                    if "spotifyd" in d.get("name", "").lower() and d.get("is_active"):
                        return d.get("id")
                for d in devs:
                    if "spotifyd" in d.get("name", "").lower():
                        return d.get("id")
                return None
            pc_id = None
            for attempt in range(5):
                pc_id = _get_spotifyd_id()
                if pc_id:
                    break
                time.sleep(1.0)
            if not pc_id:
                return {"error": "No spotifyd found. Is it running?"}
            try:
                sp.start_playback(device_id=pc_id, uris=[uri])
            except Exception as e:
                err = str(e).lower()
                if "device" in err and ("not found" in err or "not active" in err):
                    time.sleep(1.0)
                    pc_id = _get_spotifyd_id()
                    if not pc_id:
                        return {"error": "spotifyd disappeared during playback start."}
                    sp.start_playback(device_id=pc_id, uris=[uri])
                else:
                    raise
            time.sleep(1.5)
            rec_uris = []
            if artist_id:
                try:
                    top = sp.artist_top_tracks(artist_id, country=market)
                    rec_uris = [
                        t["uri"] for t in top.get("tracks", [])
                        if t["uri"] != uri
                    ][:5]
                except Exception:
                    pass
            if rec_uris:
                added = 0
                for rec_uri in rec_uris:
                    fresh_id = _get_spotifyd_id() or pc_id
                    try:
                        sp.add_to_queue(uri=rec_uri, device_id=fresh_id)
                        added += 1
                        time.sleep(0.6)
                    except Exception as e:
                        err = str(e).lower()
                        if "device" in err and "not found" in err:
                            pc_id = _get_spotifyd_id()
                            if pc_id:
                                try:
                                    sp.add_to_queue(uri=rec_uri, device_id=pc_id)
                                    added += 1
                                    time.sleep(0.6)
                                except Exception:
                                    pass
                        continue
                return {
                    "success": True,
                    "message": f"▶️ {track_name} – {artist_name} (+{added}/{len(rec_uris)} Radio)"}
            else:
                album_uri = track.get("album", {}).get("uri", "")
                if album_uri:
                    time.sleep(0.5)
                    fresh_id = _get_spotifyd_id() or pc_id
                    try:
                        sp.start_playback(
                            device_id=fresh_id,
                            context_uri=album_uri,
                            offset={"uri": uri})
                    except Exception:
                        pass
                    return {
                        "success": True,
                        "message": f"▶️ {track_name} – {artist_name} (Album)"}
                return {
                    "success": True,
                    "message": f"▶️ {track_name} – {artist_name}"}
        elif action == "stop":
            sp.pause_playback()
            return {"success": True, "message": "⏹️ Stopped."}
        else:
            return {"error": "Action: play or stop."}
    except Exception as e:
        return {"error": f"Spotify: {type(e).__name__}: {str(e)}"}
