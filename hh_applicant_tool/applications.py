from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from .utils import random_text


_FILE_RE = re.compile(
    r"""@file:(?P<path>"[^"]+"|'[^']+'|[^\s{}|]+)""",
    flags=re.IGNORECASE,
)


def _expand_file_directives(text: str) -> str:
    """
    Replace @file:/path or @file:~/path (also supports quotes) with file content.
    Allows using @file inside {a|b|c} random blocks.

    Safe fallback: if a file can't be read, leave the directive as-is.
    """
    def repl(m: re.Match) -> str:
        raw = m.group("path")

        # strip optional quotes
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]

        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return m.group(0)

    return _FILE_RE.sub(repl, text)


def render_application_message(template: str, placeholders: Mapping[str, Any]) -> str:
    """
    Render application message with this pipeline:
    1) expand @file: directives
    2) apply random_text {...|...}
    3) apply %(placeholder)s mapping
    """
    template = _expand_file_directives(template)
    return random_text(template) % placeholders


def send_application(api_client, params: dict[str, Any]) -> dict:
    """Send an application using HH negotiations API."""
    return api_client.post("/negotiations", params)
