"""Bounded diagnostics, with no credentials or notification bodies in exports."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import re
import platform
import ssl
import threading
from pathlib import Path

_LOCK = threading.Lock()
_LOGGERS: dict[str, logging.Logger] = {}


def redact(value: object) -> str:
    text = str(value)
    # Handle quoted JSON/log values before whitespace escaping or the unquoted
    # fallback. Passwords can contain spaces, punctuation, and escaped quotes.
    text = re.sub(r'''(?ix)((?:["']?)(?:authorization|password|token|secret|api[_-]?key)(?:["']?)\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
                  r'\1"[redacted]"', text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(password|token|secret|api[_-]?key)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    text = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1?[redacted]", text)
    return text.replace("\r", "\\r").replace("\n", "\\n")[:1500]


def write_log(path: Path, message: object) -> None:
    try:
        key = str(path)
        with _LOCK:
            logger = _LOGGERS.get(key)
            if logger is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                logger = logging.Logger(key, logging.INFO)
                handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=4, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                logger.addHandler(handler)
                _LOGGERS[key] = logger
        logger.info(redact(message))
    except OSError:
        pass


def export_diagnostics(destination: Path, *, version: str, statuses: dict, counts: dict, update_error: str = "") -> None:
    document = {
        "application_version": version, "python": platform.python_version(),
        "openssl": ssl.OPENSSL_VERSION, "windows": platform.platform(),
        "profiles": [{"number": int(index) + 1, "state": str(value[0])} for index, value in statuses.items()],
        "inbox_counts": counts,
        "update_error": "An update error was recorded. Review Health on this device." if update_error else "",
        "privacy": "Credentials, PBX addresses, usernames and message contents are excluded.",
    }
    destination.write_text(json.dumps(document, indent=2), encoding="utf-8")
