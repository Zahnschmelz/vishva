#!/usr/bin/env python3
import os
import tempfile
import requests
from typing import Optional

class TTSManager:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = bool(config.get("tts_enabled", False))
## Properties
    @property
    def server_url(self) -> str:
        return str(
            self.config.get("tts_server_url", "http://127.0.0.1:8091")
            or "http://127.0.0.1:8091").rstrip("/")

    @property
    def model(self) -> str:
        return str(self.config.get("tts_model", "kokoro") or "kokoro")

    @property
    def voice(self) -> str:
        return str(self.config.get("tts_voice", "kerstin") or "kerstin")

    @property
    def speed(self) -> float:
        try:
            return float(self.config.get("tts_speed", 1.0) or 1.0)
        except Exception:
            return 1.0

    @property
    def response_format(self) -> str:
        return str(self.config.get("tts_response_format", "wav") or "wav")

    @property
    def timeout(self) -> int:
        try:
            return int(self.config.get("tts_timeout", 120) or 120)
        except Exception:
            return 120

    @property
    def play_locally(self) -> bool:
        return bool(self.config.get("tts_play", False))

# Kern-Funktionen
    def synthesize(self, text: str) -> Optional[bytes]:
        if not text or not text.strip():
            return None
        if not self.enabled:
            return None
        try:
            response = requests.post(
                f"{self.server_url}/v1/audio/speech",
                json={
                    "model": self.model,
                    "input": text,
                    "voice": self.voice,
                    "speed": self.speed,
                    "response_format": self.response_format,},
                timeout=self.timeout,)
            response.raise_for_status()
            return response.content
        except requests.exceptions.ConnectionError:
            print(f"[TTS] Server nicht erreichbar: {self.server_url}")
            return None
        except requests.exceptions.Timeout:
            print(f"[TTS] Timeout nach {self.timeout}s")
            return None
        except requests.exceptions.HTTPError as e:
            print(f"[TTS] HTTP-Fehler: {e}")
            try:
                error_data = e.response.json()
                print(f"[TTS] Server-Antwort: {error_data}")
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[TTS] Unerwarteter Fehler: {type(e).__name__}: {e}")
            return None

    def synthesize_to_file(self, text: str, output_path: str) -> bool:
        audio_data = self.synthesize(text)
        if not audio_data:
            return False
        try:
            with open(output_path, "wb") as f:
                f.write(audio_data)
            return True
        except Exception as e:
            print(f"[TTS] Datei-Schreibfehler: {e}")
            return False

    def speak(self, text: str) -> bool:
        if not self.enabled:
            return False
        audio_data = self.synthesize(text)
        if not audio_data:
            return False
        if self.play_locally:
            return self._play_audio(audio_data)
        return True

    def _play_audio(self, audio_data: bytes) -> bool:
        import shutil
        import subprocess
        suffix = f".{self.response_format}"
        tmp_path = tempfile.mktemp(suffix=suffix)
        try:
            with open(tmp_path, "wb") as f:
                f.write(audio_data)
            players = []
            if self.response_format == "wav":
                players = [
                    ["aplay", "-q", tmp_path],
                    ["paplay", tmp_path],
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],]
            elif self.response_format == "mp3":
                players = [
                    ["mpg123", "-q", tmp_path],
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],]
            else:
                players = [
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],]
            for cmd in players:
                if shutil.which(cmd[0]):
                    try:
                        subprocess.run(
                            cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=60,)
                        return True
                    except Exception as e:
                        print(f"[TTS] Player-Fehler mit {cmd[0]}: {e}")
                        continue
            print("[TTS] Kein geeigneter Audio-Player gefunden.")
            return False
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def toggle(self, enabled: bool):
        self.enabled = enabled
        self.config["tts_enabled"] = enabled
