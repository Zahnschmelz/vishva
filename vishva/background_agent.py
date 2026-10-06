#!/usr/bin/env python3
import json
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

try:
    import requests
except ImportError:
    requests = None

class MetaModelClient:
    def __init__(self, config: dict):
        self.config = config or {}
    @property
    def url(self) -> str:
        return str(self.config.get("meta_model_url", "") or "").strip()
    @property
    def model(self) -> str:
        return str(self.config.get("meta_model_name", "meta") or "meta")
    @property
    def api_key(self) -> str:
        return str(
            self.config.get("meta_api_key", "")
            or self.config.get("api_key", "")
            or "")
    @property
    def timeout(self) -> int:
        try:
            return int(self.config.get("meta_model_timeout", 60) or 60)
        except Exception:
            return 60
    @property
    def fallback_enabled(self) -> bool:
        return bool(self.config.get("meta_fallback_enabled", True))
    @property
    def main_url(self) -> str:
        return str(self.config.get("base_url", "") or "").strip()
    @property
    def main_model(self) -> str:
        return str(self.config.get("model", "") or "")
    @property
    def main_api_key(self) -> str:
        return str(self.config.get("api_key", "") or "")
    @property
    def main_timeout(self) -> int:
        try:
            raw = int(self.config.get("api_timeout", 300) or 300)
        except Exception:
            raw = 300
        return min(raw, 120)
    @property
    def available(self) -> bool:
        if requests is None:
            return False
        if self.url:
            return True
        return bool(self.fallback_enabled and self.main_url and self.main_model)

    def chat(self, prompt: str, system: str = "",
             max_tokens: int = 50, temperature: float = 0.1) -> Optional[str]:
        if requests is None:
            return None
        # Config-Override für Reasoning-Modelle, die mehr Tokens brauchen
        try:
            cfg_max = int(self.config.get("meta_model_max_tokens", 0) or 0)
            if cfg_max > max_tokens:
                max_tokens = cfg_max
        except (TypeError, ValueError):
            pass
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        if self.url:
            extra = None
            if bool(self.config.get("meta_send_thinking_kwargs", False)):
                extra = {"chat_template_kwargs": {"enable_thinking": False}}
            content = self._request(
                base_url=self.url,
                model=self.model,
                api_key=self.api_key,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=self.timeout,
                extra=extra,
                label="meta")
            if content is not None:
                return content
        if self.fallback_enabled and self.main_url and self.main_model:
            if self.url:
                print("[BackgroundAgent] Meta-Modell fehlgeschlagen → Fallback auf Hauptmodell")
            return self._request(
                base_url=self.main_url,
                model=self.main_model,
                api_key=self.main_api_key,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=self.main_timeout,
                extra=None,
                label="main")
        return None

    def _request(self, base_url: str, model: str, api_key: str,
                 messages: List[Dict[str, Any]], max_tokens: int,
                 temperature: float, timeout: int,
                 extra: Optional[Dict[str, Any]] = None,
                 label: str = "") -> Optional[str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,}
        if bool(self.config.get("meta_send_thinking_kwargs", False)):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if extra:
            payload.update(extra)
        try:
            resp = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            content = (msg.get("content", "") or "").strip()
            # Reasoning-Modelle (z.B. LFM) liefern die Antwort in reasoning_content
            if not content:
                content = (msg.get("reasoning_content", "")
                           or msg.get("reasoning", "")
                           or msg.get("thinking", "") or "").strip()
            return content if content else None
        except Exception as e:
            print(f"[BackgroundAgent] {label or 'model'} request error: {e}")
            return None
