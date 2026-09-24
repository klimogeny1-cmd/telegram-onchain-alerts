"""Minimal Telegram Bot API client - sendMessage only.

This template only posts to a channel; it never reads updates or runs bot commands, so
that's all this file does. Standard library only (urllib), with the same retry/flood
control shape as a Bot API client that does more (getUpdates, callbacks, payments) would
need, trimmed down to one call.
"""
import json
import logging
import socket
import time
import urllib.error
import urllib.request

log = logging.getLogger("telegram")


class TelegramError(Exception):
    def __init__(self, method, code, description, retry_after=None):
        super().__init__("%s failed: %s %s" % (method, code, description))
        self.method = method
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after


class TokenFilter(logging.Filter):
    """Strips the bot token out of every log record produced anywhere in this process,
    so a stray exception or debug line can never leak it into a log file or console."""

    def __init__(self, *secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record):
        if not self.secrets:
            return True
        msg = record.getMessage()
        cleaned = msg
        for secret in self.secrets:
            cleaned = cleaned.replace(secret, "<TOKEN>")
        if cleaned != msg:
            record.msg = cleaned
            record.args = ()
        return True


class BotAPI:
    def __init__(self, token, base="https://api.telegram.org", timeout=20):
        if not token:
            raise ValueError("BOT_TOKEN is empty")
        self._token = token
        self._base = "%s/bot%s/" % (base, token)
        self.timeout = timeout

    def _clean(self, text):
        return str(text).replace(self._token, "<TOKEN>")

    def call(self, method, params=None, retries=2):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        for attempt in range(retries + 1):
            request = urllib.request.Request(
                self._base + method, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    data = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    data = {"ok": False, "error_code": exc.code, "description": "HTTP %s" % exc.code}
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, ValueError) as exc:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise TelegramError(method, "network", self._clean(str(exc)))
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            desc = data.get("description", "")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after and attempt < retries:
                log.warning("%s: flood control, waiting %ss", method, retry_after)
                time.sleep(min(int(retry_after), 60) + 0.5)
                continue
            if code and code >= 500 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise TelegramError(method, code, self._clean(desc), retry_after)
        raise TelegramError(method, "retries", "gave up")

    def send_message(self, chat_id, text, parse_mode="HTML", disable_preview=True):
        params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if disable_preview:
            params["link_preview_options"] = {"is_disabled": True}
        return self.call("sendMessage", params)

    def get_me(self):
        return self.call("getMe", retries=1)
