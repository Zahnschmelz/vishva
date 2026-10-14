"""Auto-extrahiertes Tool-Modul (aus tool_manager.py)."""
from typing import Any, Dict

try:
    import requests
except ImportError:
    requests = None


def _speak(self, args: Dict[str, Any]) -> Dict[str, Any]:
    text = (args.get("speech") or "").strip()
    if not text:
        return {"error": "speech is required."}

    if not getattr(self, "tts_manager", None):
        try:
            from ..tts_manager import TTSManager
            self.tts_manager = TTSManager(self.config)
        except Exception as e:
            return {"error": f"TTS init failed: {type(e).__name__}: {str(e)}"}

    mgr = self.tts_manager
    was_enabled = mgr.enabled
    mgr.enabled = True
    try:
        audio = mgr.synthesize(text)
    finally:
        mgr.enabled = was_enabled

    if not audio:
        return {
            "success": False,
            "error": f"TTS synthesis failed — server unreachable? ({mgr.server_url})"}

    played = mgr._play_audio(audio)
    if played:
        return {"success": True, "message": f"TTS: '{text[:60]}' played."}
    return {
        "success": False,
        "error": "Audio synthesized, but no local player found "
                 "(aplay/paplay/ffplay/mpg123)."}
