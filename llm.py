# -*- coding: utf-8 -*-
"""
Сменный OpenAI-совместимый клиент.
Конфиг только из env: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")


def llm_configured() -> bool:
    return bool(LLM_API_KEY and LLM_BASE_URL)


def chat_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    json_mode: bool = True,
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Возвращает:
      {ok, content, raw, usage, error, latency_ms}
    """
    if not llm_configured():
        return {
            "ok": False,
            "error": "LLM не настроен: задайте LLM_API_KEY (или DEEPSEEK_API_KEY) в Variables",
            "content": None,
            "raw": None,
            "usage": None,
        }

    url = f"{LLM_BASE_URL}/chat/completions"
    payload: dict[str, Any] = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # non-thinking для flash: если провайдер поддерживает
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        latency = int((time.time() - t0) * 1000)
        body_text = r.text
        try:
            body = r.json()
        except Exception:
            return {
                "ok": False,
                "error": f"HTTP {r.status_code}: non-json body",
                "content": None,
                "raw": body_text[:2000],
                "usage": None,
                "latency_ms": latency,
            }

        if r.status_code == 402:
            return {
                "ok": False,
                "error": "402 Insufficient Balance — пополните баланс провайдера",
                "content": None,
                "raw": body,
                "usage": None,
                "latency_ms": latency,
            }
        if r.status_code == 429:
            return {
                "ok": False,
                "error": "429 rate/concurrency limit",
                "content": None,
                "raw": body,
                "usage": None,
                "latency_ms": latency,
            }
        if r.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {r.status_code}: {body.get('error') or body}",
                "content": None,
                "raw": body,
                "usage": None,
                "latency_ms": latency,
            }

        choices = body.get("choices") or []
        content = None
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
        usage = body.get("usage")
        return {
            "ok": True,
            "content": content,
            "raw": body,
            "usage": usage,
            "latency_ms": latency,
            "model": payload["model"],
        }
    except requests.Timeout:
        return {
            "ok": False,
            "error": f"timeout after {timeout}s",
            "content": None,
            "raw": None,
            "usage": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "content": None,
            "raw": None,
            "usage": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }


def parse_json_content(content: str | None) -> dict[str, Any]:
    if not content:
        return {"ok": False, "error": "empty content", "data": None}
    text = content.strip()
    # иногда обёртка ```json
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return {"ok": True, "data": json.loads(text), "error": None}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"json parse: {e}", "data": None, "raw": text[:2000]}
