from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]


def _text_from_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def find_openai_api_key() -> str:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value

    file_candidates = [
        os.environ.get("OPENAI_API_KEY_FILE"),
        str(ROOT / "secrets" / "openai_api_key.txt"),
        str(ROOT / "secrets" / "openai.env"),
    ]
    for raw_path in file_candidates:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        text = _text_from_file(path)
        if not text:
            continue
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if "=" in candidate:
                key, value = candidate.split("=", 1)
                if key.strip() == "OPENAI_API_KEY":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
            elif candidate:
                return candidate
    raise RuntimeError("No OpenAI API key found")


def _default_models() -> tuple[str, ...]:
    raw = os.environ.get("OPENAI_MODEL", "").strip()
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return ("gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini")


@dataclass
class OpenAIClient:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_sec: int = 120
    models: tuple[str, ...] = ("gpt-4.1-mini",)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _parse_responses_content(self, payload: dict[str, Any]) -> str:
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: list[str] = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if text:
                    parts.append(str(text))
        text = "\n".join(part.strip() for part in parts if str(part).strip()).strip()
        if text:
            return text
        raise RuntimeError("OpenAI responses output format is not supported")

    def _parse_content(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI response did not include choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            text = "\n".join(parts).strip()
            if text:
                return text
        raise RuntimeError("OpenAI response content format is not supported")

    def _responses_request(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        response = requests.post(
            f"{self.base_url.rstrip('/')}/responses",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout_sec,
        )
        if response.ok:
            return self._parse_responses_content(response.json())
        response.raise_for_status()
        raise RuntimeError("OpenAI responses request failed")

    def _chat_completions_request(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout_sec,
        )
        if response.ok:
            return self._parse_content(response.json())
        response.raise_for_status()
        raise RuntimeError("OpenAI chat completions request failed")

    def ask(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 2600,
    ) -> str:
        last_error: str | None = None
        last_exception: Exception | None = None
        for model in self.models:
            for api_surface in ("responses", "chat_completions"):
                try:
                    if api_surface == "responses":
                        return self._responses_request(
                            model=model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                    return self._chat_completions_request(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    response_obj = getattr(exc, "response", None)
                    if response_obj is not None:
                        try:
                            error_payload = response_obj.json()
                        except Exception:  # noqa: BLE001
                            error_payload = {"error": {"message": str(response_obj.text)[:500]}}
                        last_error = json.dumps(error_payload, ensure_ascii=False)
                        message = str((error_payload.get("error") or {}).get("message") or "").lower()
                        if response_obj.status_code in {400, 404} and (
                            "model" in message or "unsupported" in message or "not found" in message
                        ):
                            if api_surface == "chat_completions":
                                break
                            continue
                    if last_error is None:
                        last_error = f"{type(exc).__name__}: {exc}"
                    if api_surface == "chat_completions":
                        break
        if last_exception is not None:
            raise RuntimeError(f"OpenAI request failed ({last_error})") from last_exception
        raise RuntimeError(f"OpenAI request failed ({last_error})")


def build_openai_client() -> OpenAIClient:
    return OpenAIClient(
        api_key=find_openai_api_key(),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
        timeout_sec=int(os.environ.get("OPENAI_TIMEOUT_SEC", "120")),
        models=_default_models(),
    )
