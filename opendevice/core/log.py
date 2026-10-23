from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from . import paths

ROOT = "opendevice"
ENV = "OPENDEVICE_LOG"
ENV_LEGACY = "OPENDDEVICE_LOG"

SECRET_TOKENS = ("private", "secret", "password", "passwd", "token", "certificate",
                 "cert", "escr", "irk", "public_key", "host_key")
MAX_STRING = 120
MAX_BYTES = 0

_configured = False


def get(name: str = "") -> logging.Logger:
    'Get a logger (``get("services.afc")`` → ``opendevice.services.afc``).'
    if not name or name == ROOT:
        return logging.getLogger(ROOT)
    if name.startswith(f"{ROOT}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT}.{name}")


def _is_secret(key: Any) -> bool:
    lowered = str(key).lower()
    return any(token in lowered for token in SECRET_TOKENS)


def redact_mapping(mapping: Any) -> dict[str, Any]:
    'Redact a dict: sensitive keys become ``<hidden>``, other values are handled recursively.'
    return {str(key): ("<hidden>" if _is_secret(key) else redact(value))
            for key, value in mapping.items()}


def redact(value: Any, depth: int = 0) -> Any:
    'Redact + truncate for logging (does not mutate the original).'
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {key: ("<hidden>" if _is_secret(key) else redact(item, depth + 1))
                for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact(item, depth + 1) for item in value)
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))}B>"
    if isinstance(value, str) and len(value) > MAX_STRING:
        return f"{value[:MAX_STRING]}…({len(value)})"
    return value


class SafeFormatter(logging.Formatter):
    'Runs :func:`redact` before formatting.'

    def format(self, record: logging.LogRecord) -> str:
        record = logging.makeLogRecord(record.__dict__)
        if isinstance(record.args, dict):
            record.args = redact_mapping(record.args)
        elif record.args:
            record.args = tuple(redact(value) for value in record.args)
        return super().format(record)


def configure(verbose: int = 0, path: Optional[str] = None,
              stream: bool = True, force: bool = False) -> logging.Logger:
    'Turn logging on.'
    global _configured
    logger = get()
    if _configured and not force:
        return logger

    level = logging.DEBUG if verbose >= 2 else (logging.INFO if verbose else logging.WARNING)
    logger.setLevel(level)
    logger.handlers = [handler for handler in logger.handlers
                       if not isinstance(handler, logging.NullHandler)]
    formatter = SafeFormatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")

    if stream:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)

    if path is not None:
        target = paths.ensure(paths.logs_dir()) / "opendevice.log" if not path else None
        log_path = target if target is not None else _as_path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.debug('logging to %s', log_path)

    _configured = True
    return logger


def _as_path(value: str) -> "Any":
    from pathlib import Path

    return Path(value).expanduser()


def level_from_env() -> int:
    'Map ``OPENDEVICE_LOG=debug`` to a verbosity level (returns 0 when unset).'
    value = (os.environ.get(ENV) or os.environ.get(ENV_LEGACY) or "").strip().lower()
    if value in ("debug", "2", "vv"):
        return 2
    if value in ("info", "1", "v"):
        return 1
    return 0


def reset() -> None:
    'Turn logging off (mainly for tests).'
    global _configured
    logger = get()
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(logging.NOTSET)
    _configured = False


get().addHandler(logging.NullHandler())
