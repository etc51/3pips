from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]

TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _text_from_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _looks_like_base64_auth(value: str) -> bool:
    compact = value.strip()
    return len(compact) >= 40 and all(ch.isalnum() or ch in "+/=_-" for ch in compact)


def find_gigachat_auth_key() -> str:
    direct_env_names = [
        "GIGACHAT_AUTH_KEY",
        "GIGACHAT_CREDENTIALS",
        "GIGACHAT_TOKEN",
    ]
    for name in direct_env_names:
        value = os.environ.get(name)
        if value and _looks_like_base64_auth(value):
            return value.strip()

    file_candidates = [
        os.environ.get("GIGACHAT_AUTH_FILE"),
        str(ROOT / "secrets" / "gigachat_auth.txt"),
        str(ROOT / "secrets" / "gigachat.env"),
    ]
    for raw_path in file_candidates:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        text = _text_from_file(path)
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if "=" in candidate:
                key, value = candidate.split("=", 1)
                if key.strip() in {"GIGACHAT_AUTH_KEY", "GIGACHAT_CREDENTIALS", "GIGACHAT_TOKEN"}:
                    value = value.strip().strip("'").strip('"')
                    if _looks_like_base64_auth(value):
                        return value
            elif _looks_like_base64_auth(candidate):
                return candidate

    client_id = os.environ.get("GIGACHAT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GIGACHAT_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        pair = f"{client_id}:{client_secret}".encode("utf-8")
        return base64.b64encode(pair).decode("ascii")

    raise RuntimeError("No GigaChat auth key found")


@dataclass
class GigaChatClient:
    auth_key: str
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat"
    verify_ssl: bool = False
    timeout_sec: int = 60

    def fetch_access_token(self) -> str:
        headers = {
            "Authorization": f"Basic {self.auth_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        response = requests.post(
            TOKEN_URL,
            headers=headers,
            data={"scope": self.scope},
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        payload = response.json()
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("GigaChat token response did not include access_token")
        return access_token

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        access_token = self.fetch_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = requests.post(
            CHAT_URL,
            headers=headers,
            json=payload,
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def ask(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        payload = self.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("GigaChat response did not include choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            return "\n".join(parts).strip()
        raise RuntimeError("GigaChat response content format is not supported")


def build_gigachat_client() -> GigaChatClient:
    return GigaChatClient(
        auth_key=find_gigachat_auth_key(),
        scope=os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        model=os.environ.get("GIGACHAT_MODEL", "GigaChat"),
        verify_ssl=_bool_env("GIGACHAT_VERIFY_SSL", False),
        timeout_sec=int(os.environ.get("GIGACHAT_TIMEOUT_SEC", "60")),
    )
