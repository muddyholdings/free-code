"""Declarative platform tools: validate a spec, then run it against the Free.ai API.

A spec is plain data (no code), so the same engine runs the built-in set in
``builtin_manifest.py`` and any tool the server manifest adds later. A spec can
only call Free.ai API paths with a templated JSON body or a multipart upload,
and can only save output inside the project. It cannot run local commands or
read files other than the one a user-visible argument names.
"""

from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from free_code import __version__

DEFAULT_API_BASE = "https://api.free.ai"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_TEXT_RESULT = 20000
POLL_SECONDS = 5
POLL_MAX_SECONDS = 600

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PATH_RE = re.compile(r"^/v1/[a-z0-9_\-/]+/?$")
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)(?:\|([^{}]*))?\}")
CONFIRM_LEVELS = ("none", "write", "upload")
SCHEMA_TYPES = ("string", "integer", "number", "boolean", "array", "object")

# Tools that run local code or touch the local tree. A manifest can never
# define or replace these; it can only disable them or add a note.
LOCAL_TOOL_NAMES = frozenset({
    "file_read", "file_write", "apply_patch", "shell_command", "grep_search",
    "git_status", "git_diff", "git_commit", "git_log", "run_tests",
    "list_files", "web_fetch", "translate_file",
})


class SpecError(ValueError):
    """A tool spec failed validation."""


def validate_spec(spec: Any, *, allow_local_names: bool = False) -> Dict[str, Any]:
    """Return the spec if it is safe and well formed, else raise SpecError."""
    if not isinstance(spec, dict):
        raise SpecError("tool spec must be an object")
    name = spec.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError(f"bad tool name: {name!r}")
    if name in LOCAL_TOOL_NAMES and not allow_local_names:
        raise SpecError(f"{name} is a local tool and cannot come from a manifest")
    desc = spec.get("description")
    if not isinstance(desc, str) or not (3 <= len(desc) <= 600):
        raise SpecError(f"{name}: description must be 3 to 600 characters")
    params = spec.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        raise SpecError(f"{name}: parameters must be an object schema")
    props = params.get("properties", {})
    if not isinstance(props, dict) or len(props) > 20:
        raise SpecError(f"{name}: properties must be an object of at most 20 entries")
    for pname, pdef in props.items():
        if not isinstance(pname, str) or not re.match(r"^[a-z_][a-z0-9_]*$", pname):
            raise SpecError(f"{name}: bad parameter name {pname!r}")
        if not isinstance(pdef, dict) or pdef.get("type") not in SCHEMA_TYPES:
            raise SpecError(f"{name}.{pname}: unsupported type")
    required = params.get("required", [])
    if not isinstance(required, list) or any(r not in props for r in required):
        raise SpecError(f"{name}: required lists an unknown parameter")

    http = spec.get("http")
    if not isinstance(http, dict):
        raise SpecError(f"{name}: http block required")
    if http.get("method") not in ("GET", "POST"):
        raise SpecError(f"{name}: method must be GET or POST")
    path = http.get("path")
    if not isinstance(path, str) or not PATH_RE.match(path) or ".." in path:
        raise SpecError(f"{name}: path must be a /v1/ API path")
    timeout = http.get("timeout", 120)
    if not isinstance(timeout, (int, float)) or not (1 <= timeout <= 900):
        raise SpecError(f"{name}: timeout must be 1 to 900 seconds")
    body = http.get("body", {})
    if not isinstance(body, dict):
        raise SpecError(f"{name}: body must be an object")
    _check_template(name, body, props)
    upload = http.get("upload")
    if upload is not None:
        if not isinstance(upload, dict) or upload.get("arg") not in props:
            raise SpecError(f"{name}: upload.arg must name a parameter")
        if not isinstance(upload.get("field", "file"), str):
            raise SpecError(f"{name}: upload.field must be a string")

    result = spec.get("result", {})
    if not isinstance(result, dict):
        raise SpecError(f"{name}: result must be an object")
    for key in ("text", "file_url"):
        vals = result.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise SpecError(f"{name}: result.{key} must be a list of field names")
    save_arg = result.get("save_arg")
    if save_arg is not None and save_arg not in props:
        raise SpecError(f"{name}: result.save_arg must name a parameter")
    confirm = spec.get("confirm", "none")
    if confirm not in CONFIRM_LEVELS:
        raise SpecError(f"{name}: confirm must be one of {CONFIRM_LEVELS}")
    if save_arg and confirm == "none":
        raise SpecError(f"{name}: a tool that writes a file must set confirm")
    if upload is not None and confirm != "upload":
        raise SpecError(f"{name}: a tool that uploads a file must set confirm=upload")
    return spec


def _check_template(name: str, node: Any, props: Dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise SpecError(f"{name}: body keys must be strings")
            _check_template(name, v, props)
    elif isinstance(node, list):
        for v in node:
            _check_template(name, v, props)
    elif isinstance(node, str):
        for m in PLACEHOLDER_RE.finditer(node):
            if m.group(1) not in props:
                raise SpecError(f"{name}: body uses unknown parameter {m.group(1)}")
    elif not isinstance(node, (int, float, bool)) and node is not None:
        raise SpecError(f"{name}: unsupported value in body")


def _render(node: Any, args: Dict[str, Any], props: Optional[Dict[str, Any]] = None) -> Tuple[Any, bool]:
    """Render a body template. Returns (value, keep); keep False drops the key."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            val, keep = _render(v, args, props)
            if keep:
                out[k] = val
        return out, True
    if isinstance(node, list):
        return [v for v, keep in (_render(x, args, props) for x in node) if keep], True
    if isinstance(node, str):
        whole = PLACEHOLDER_RE.fullmatch(node)
        if whole:
            name, default = whole.group(1), whole.group(2)
            if name in args and args[name] not in (None, ""):
                return args[name], True
            if default is not None:
                return _coerce(default, (props or {}).get(name, {}).get("type")), True
            return None, False

        def sub(m: "re.Match[str]") -> str:
            val = args.get(m.group(1))
            if val in (None, ""):
                return m.group(2) or ""
            return str(val)

        return PLACEHOLDER_RE.sub(sub, node), True
    return node, True


def _coerce(value: str, kind: Optional[str]) -> Any:
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            return value.lower() in ("1", "true", "yes")
    except ValueError:
        pass
    return value


def render_body(spec: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    props = spec.get("parameters", {}).get("properties", {})
    body, _ = _render(spec.get("http", {}).get("body", {}), args, props)
    return body


def missing_required(spec: Dict[str, Any], args: Dict[str, Any]) -> List[str]:
    req = spec.get("parameters", {}).get("required", [])
    return [r for r in req if args.get(r) in (None, "")]


def _pick(data: Any, keys: List[str]) -> Optional[Any]:
    """First non-empty value for any key, looking one level into nested dicts."""
    if not isinstance(data, dict):
        return None
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    for v in data.values():
        if isinstance(v, dict):
            hit = _pick(v, keys)
            if hit is not None:
                return hit
    return None


def _resolve_inside(project_root: Optional[str], rel: str) -> Path:
    root = Path(project_root or ".").resolve()
    target = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if root != target and root not in target.parents:
        raise SpecError(f"refusing to write outside the project: {rel}")
    return target


def _headers(token: Optional[str]) -> Dict[str, str]:
    h = {"User-Agent": f"free-code/{__version__}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _http_error(status: int, text: str) -> str:
    low = (text or "")[:300]
    if status in (401, 402, 403, 429):
        what = ("The free anonymous limit for today is used up."
                if status == 429 else "This needs a Free.ai key or more tokens.")
        return (f"Error: API {status}: {low}\n{what} Get a free key (no card) at "
                "https://free.ai/account/#api-keys then run: free-code login")
    return f"Error: API {status}: {low}"


def execute_spec(
    spec: Dict[str, Any],
    args: Dict[str, Any],
    *,
    token: Optional[str] = None,
    project_root: Optional[str] = None,
    api_base: str = DEFAULT_API_BASE,
    client: Optional[httpx.Client] = None,
) -> str:
    """Run a validated spec. Returns text for the model; errors start with 'Error:'."""
    miss = missing_required(spec, args)
    if miss:
        return f"Error: missing required argument(s): {', '.join(miss)}"
    http = spec["http"]
    result_cfg = spec.get("result", {})
    save_arg = result_cfg.get("save_arg")
    out_path: Optional[Path] = None
    if save_arg and args.get(save_arg):
        try:
            out_path = _resolve_inside(project_root, str(args[save_arg]))
        except SpecError as e:
            return f"Error: {e}"

    url = api_base.rstrip("/") + http["path"]
    timeout = float(http.get("timeout", 120))
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        upload = http.get("upload")
        if upload:
            src = _resolve_read(project_root, str(args.get(upload["arg"], "")))
            if isinstance(src, str):
                return src
            if src.stat().st_size > MAX_UPLOAD_BYTES:
                return f"Error: file too large to upload ({src.stat().st_size} bytes, cap {MAX_UPLOAD_BYTES})"
            extra = render_body(spec, args)
            mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            with open(src, "rb") as fh:
                resp = client.post(
                    url,
                    headers=_headers(token),
                    files={upload.get("field", "file"): (src.name, fh, mime)},
                    data={k: str(v) for k, v in extra.items()},
                    timeout=timeout,
                )
        elif http["method"] == "GET":
            resp = client.get(url, headers=_headers(token), params=render_body(spec, args), timeout=timeout)
        else:
            resp = client.post(url, headers=_headers(token), json=render_body(spec, args), timeout=timeout)

        if resp.status_code >= 400:
            return _http_error(resp.status_code, resp.text)
        try:
            data = resp.json()
        except ValueError:
            return f"Error: API returned a non-JSON response ({resp.status_code})"
        if isinstance(data, dict) and data.get("error") and not _pick(data, result_cfg.get("file_url", []) + result_cfg.get("text", [])):
            return f"Error: {data.get('error')}"

        job_id = data.get("job_id") if isinstance(data, dict) else None
        if job_id and not _pick(data, result_cfg.get("file_url", []) + result_cfg.get("text", [])):
            polled = _poll_job(client, api_base, str(job_id), token, timeout, data.get("poll_url"))
            if isinstance(polled, str):
                return polled
            data = polled

        return _finish(client, spec, data, out_path, project_root, token)
    except httpx.TimeoutException:
        return "Error: request to Free.ai timed out"
    except httpx.HTTPError as e:
        return f"Error: could not reach Free.ai ({type(e).__name__})"
    finally:
        if own:
            client.close()


def _resolve_read(project_root: Optional[str], rel: str) -> "Path | str":
    if not rel:
        return "Error: no file path given"
    p = Path(rel)
    if not p.is_absolute():
        p = Path(project_root or ".") / rel
    p = p.resolve()
    if not p.is_file():
        return f"Error: file not found: {rel}"
    return p


WEB_BASE = "https://free.ai"
JOB_URL_RE = re.compile(r"^/api/v1/jobs/[0-9a-f\-]{36}/$")


def _poll_job(client: httpx.Client, api_base: str, job_id: str, token: Optional[str], timeout: float,
              poll_url: Optional[str] = None) -> "Dict[str, Any] | str":
    """Wait for a queued GPU job. Free.ai answers a busy GPU with {queued, job_id, poll_url}
    (poll the web tier's /api/v1/jobs/<id>/) and other endpoints with a bare job_id
    (poll the API's /v1/status/<id>/)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{4,80}", job_id):
        return "Error: unexpected job id from API"
    if isinstance(poll_url, str) and JOB_URL_RE.match(poll_url):
        url = WEB_BASE + poll_url
        queue_job = True
    else:
        url = f"{api_base.rstrip('/')}/v1/status/{job_id}/"
        queue_job = False
    deadline = time.time() + POLL_MAX_SECONDS
    last = ""
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            r = client.get(url, headers=_headers(token), timeout=30)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            continue
        if queue_job:
            data = data.get("job") if isinstance(data, dict) and isinstance(data.get("job"), dict) else data
        status = str(data.get("status", "")).lower()
        last = status
        if status in ("completed", "complete", "done", "success", "succeeded"):
            out = dict(data)
            link = out.get("output_url")
            if isinstance(link, str) and link.startswith("/"):
                out["output_url"] = WEB_BASE + link
            return out
        if status in ("failed", "error", "cancelled", "rejected"):
            return f"Error: job {job_id} {status}: {str(data.get('error_message') or data.get('error', ''))[:200]}"
    return (f"Error: job {job_id} did not finish within {POLL_MAX_SECONDS}s (last status {last or 'unknown'}). "
            "It keeps running on Free.ai; the result will appear under your jobs at https://free.ai/account/")


def _finish(client: httpx.Client, spec: Dict[str, Any], data: Any, out_path: Optional[Path], project_root: Optional[str], token: Optional[str]) -> str:
    result_cfg = spec.get("result", {})
    parts: List[str] = []
    text = None
    for key in result_cfg.get("text", []):
        val = data.get(key) if isinstance(data, dict) else None
        if val in (None, "", [], {}):
            continue
        text = True
        parts.append(_fmt(key, val))
    file_url = _pick(data, result_cfg.get("file_url", []))
    if file_url and isinstance(file_url, str):
        if out_path is not None:
            err = _download(client, file_url, out_path, token)
            if err:
                return err
            rel = _rel(out_path, project_root)
            parts.append(f"Saved to {rel}")
        else:
            parts.append(f"Output URL: {file_url}")
    elif result_cfg.get("file_url") and not text:
        return "Error: API response had no output file"
    if not parts:
        parts.append(_compact(data))
    out = "\n".join(parts)
    if len(out) > MAX_TEXT_RESULT:
        out = out[:MAX_TEXT_RESULT] + "\n... (truncated)"
    return out


def _fmt(key: str, val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val and all(isinstance(v, dict) for v in val):
        lines = []
        for i, v in enumerate(val[:10], 1):
            title = str(v.get("title") or v.get("name") or "").strip()
            link = str(v.get("url") or v.get("link") or "").strip()
            lines.append(f"[{v.get('index', i)}] {title} {link}".strip())
        return f"{key}:\n" + "\n".join(lines)
    return _compact(val)


def _compact(data: Any) -> str:
    import json
    try:
        return json.dumps(data, ensure_ascii=False)[:MAX_TEXT_RESULT]
    except (TypeError, ValueError):
        return str(data)[:MAX_TEXT_RESULT]


def _rel(path: Path, project_root: Optional[str]) -> str:
    try:
        return str(path.relative_to(Path(project_root or ".").resolve()))
    except ValueError:
        return str(path)


def _download(client: httpx.Client, url: str, dest: Path, token: Optional[str]) -> Optional[str]:
    if not url.startswith("https://"):
        return "Error: refusing to download a non-HTTPS output URL"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with client.stream("GET", url, headers=_headers(None), timeout=300, follow_redirects=True) as r:
            if r.status_code >= 400:
                return f"Error: download failed with HTTP {r.status_code}"
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return "Error: output file is larger than the 200 MB cap"
                    fh.write(chunk)
            tmp.replace(dest)
        return None
    except (httpx.HTTPError, OSError) as e:
        return f"Error: could not save output ({type(e).__name__})"
