"""Build the ExtendScript (JSX) that Photoshop runs."""

from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("jsx") / "ps_remover.jsx"
CONFIG_PLACEHOLDER = "/*PSR_CONFIG*/null"

_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def build_script(config: dict) -> str:
    """Return the Photoshop script with ``config`` embedded.

    The result is pure ASCII, so it runs the same whatever encoding Photoshop
    assumes for script files, and non-ASCII paths (e.g. Korean folder names)
    survive intact.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(CONFIG_PLACEHOLDER) != 1:
        raise RuntimeError(f"{TEMPLATE_PATH} must contain {CONFIG_PLACEHOLDER} exactly once")
    # A JSON document is a valid ECMAScript 3 object literal.
    literal = json.dumps(config, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return to_ascii_js(template.replace(CONFIG_PLACEHOLDER, literal))


def to_ascii_js(source: str) -> str:
    """Replace every non-ASCII character with a ``\\uXXXX`` escape.

    The escapes are valid in string literals and meaningless in comments,
    which is where the template's Korean text lives.
    """
    return _NON_ASCII.sub(_escape, source)


def _escape(match: "re.Match[str]") -> str:
    encoded = match.group().encode("utf-16-be")
    return "".join(f"\\u{int.from_bytes(encoded[i:i + 2], 'big'):04x}" for i in range(0, len(encoded), 2))
