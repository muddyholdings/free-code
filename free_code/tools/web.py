"""web_fetch: read a web page as plain text."""

from __future__ import annotations

import html
import re
from typing import Optional

import httpx

from free_code import __version__

MAX_BYTES = 2 * 1024 * 1024
_SCRIPT = re.compile(r"(?is)<(script|style|noscript|svg)\b.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    raw = _SCRIPT.sub(" ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\b[^>]*>", "\n", raw)
    text = html.unescape(_TAG.sub(" ", raw))
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def web_fetch(url: str, max_chars: int = 12000, _project_root: Optional[str] = None,
              _client: Optional[httpx.Client] = None) -> str:
    """Fetch a URL (http or https) and return its text content."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    max_chars = max(500, min(int(max_chars or 12000), 50000))
    own = _client is None
    client = _client or httpx.Client(timeout=20, follow_redirects=True, max_redirects=5)
    try:
        with client.stream("GET", url, headers={"User-Agent": f"free-code/{__version__}",
                                                "Accept": "text/html,text/plain,application/json,*/*;q=0.5"}) as r:
            if r.status_code >= 400:
                return f"Error: HTTP {r.status_code} from {url}"
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    break
            ctype = r.headers.get("content-type", "")
    except httpx.TimeoutException:
        return "Error: request timed out"
    except httpx.HTTPError as e:
        return f"Error: could not fetch ({type(e).__name__})"
    finally:
        if own:
            client.close()
    text = buf.decode("utf-8", errors="replace")
    if "html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        text = html_to_text(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated, {len(text)} characters total)"
    return text or "(empty page)"
