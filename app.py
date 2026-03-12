from __future__ import annotations

from flask import (
    Flask,
    render_template,
    abort,
    request,
    redirect,
    url_for,
    session,
    flash,
    Response,
    send_file,
    send_from_directory,
)
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache, wraps
from textwrap import dedent
from contextlib import contextmanager
from itertools import zip_longest
from pathlib import Path
from uuid import uuid4
import json
import hashlib
import mimetypes
from werkzeug.utils import secure_filename
import ast
import os
import re
import importlib
import warnings
import importlib.util
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urljoin, quote, urlencode
from typing import List, Tuple
import threading
import time
import sqlite3

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None

markdown = None
if importlib.util.find_spec("markdown") is not None:
    markdown = importlib.import_module("markdown")

Image = None
ImageOps = None
features = None
if importlib.util.find_spec("PIL") is not None:
    Image = importlib.import_module("PIL.Image")
    ImageOps = importlib.import_module("PIL.ImageOps")
    features = importlib.import_module("PIL.features")

brotli = None
for _brotli_module in ("brotli", "brotlicffi"):
    if importlib.util.find_spec(_brotli_module) is not None:
        brotli = importlib.import_module(_brotli_module)
        break

from markupsafe import Markup, escape
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "3kvf8P9s3NzjtSDsYcuG")


def _compute_asset_version() -> str:
    explicit = os.environ.get("ASSET_VERSION")
    if explicit:
        return explicit
    static_root = BASE_DIR / "static"
    latest_mtime = 0.0
    try:
        for path in static_root.rglob("*"):
            if not path.is_file():
                continue
            latest_mtime = max(latest_mtime, path.stat().st_mtime)
    except FileNotFoundError:
        latest_mtime = 0.0
    if latest_mtime:
        return str(int(latest_mtime))
    return str(int(datetime.utcnow().timestamp()))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        app.logger.warning(
            "Invalid value for %s=%s – falling back to %s", name, value, default
        )
        return default


def _build_decapi_url(endpoint: str, slug: str) -> str:
    normalized_slug = quote(slug.strip().lower())
    return f"{DECAPI_BASE_URL.rstrip('/')}/{endpoint}/{normalized_slug}"


def _add_live_api_headers(response: Response) -> Response:
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, OPTIONS")
    return response


_DECAPI_CACHE_LOCK = threading.Lock()
_DECAPI_CACHE_TTL_SECONDS = _env_int("DECAPI_CACHE_TTL", 45)
_DECAPI_CACHE: dict[str, tuple[float, int, str]] = {}
_LIVE_STATUS_LOCK = threading.Lock()
_LIVE_STATUS_THREAD_STARTED = False


def _get_cached_decapi_response(endpoint: str, slug: str) -> Response | None:
    if _DECAPI_CACHE_TTL_SECONDS <= 0:
        return None

    cache_key = f"{endpoint}:{slug.strip().lower()}"
    with _DECAPI_CACHE_LOCK:
        entry = _DECAPI_CACHE.get(cache_key)

    if not entry:
        return None

    expires_at, status_code, body = entry
    if expires_at < time.time():
        with _DECAPI_CACHE_LOCK:
            _DECAPI_CACHE.pop(cache_key, None)
        return None

    return Response(body, status=status_code, mimetype="text/plain")


def _set_cached_decapi_response(endpoint: str, slug: str, status_code: int, body: str) -> None:
    if _DECAPI_CACHE_TTL_SECONDS <= 0:
        return

    cache_key = f"{endpoint}:{slug.strip().lower()}"
    expires_at = time.time() + _DECAPI_CACHE_TTL_SECONDS
    with _DECAPI_CACHE_LOCK:
        _DECAPI_CACHE[cache_key] = (expires_at, status_code, body)


def _proxy_decapi_request(endpoint: str, slug: str) -> Response:
    if not slug or not slug.strip():
        return Response("Missing channel name", status=400, mimetype="text/plain")

    cached_response = _get_cached_decapi_response(endpoint, slug)
    if cached_response is not None:
        return _add_live_api_headers(cached_response)

    url = _build_decapi_url(endpoint, slug)
    request_obj = urlrequest.Request(url, headers={"Accept": "text/plain"})

    try:
        with urlrequest.urlopen(request_obj, timeout=6) as upstream:
            body = upstream.read().decode("utf-8", errors="replace")
            response = Response(body, status=upstream.status, mimetype="text/plain")
            _set_cached_decapi_response(endpoint, slug, upstream.status, body)
            return _add_live_api_headers(response)
    except urlerror.HTTPError as exc:  # pragma: no cover - relies on external service
        message = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status_code = exc.code or 502
        response = Response(
            message or "Upstream error", status=status_code, mimetype="text/plain"
        )
        _set_cached_decapi_response(endpoint, slug, status_code, message)
        return _add_live_api_headers(response)
    except urlerror.URLError as exc:  # pragma: no cover - relies on external service
        app.logger.warning("Decapi request failed for %s/%s: %s", endpoint, slug, exc)
        response = Response("Upstream unavailable", status=502, mimetype="text/plain")
        _set_cached_decapi_response(endpoint, slug, 502, "Upstream unavailable")
        return _add_live_api_headers(response)


def _normalize_decapi_status_text(raw_text: str | None) -> dict[str, str]:
    if not raw_text or not isinstance(raw_text, str):
        return {"state": "error", "message": "Status derzeit nicht verfügbar."}

    text = raw_text.strip()
    if not text:
        return {"state": "error", "message": "Status derzeit nicht verfügbar."}

    normalized = text.lower()

    if (
        "could not find" in normalized
        or "invalid channel" in normalized
        or "no user with the name" in normalized
    ):
        return {"state": "error", "message": "Kanal nicht gefunden."}

    if "too many requests" in normalized:
        return {"state": "error", "message": "Status derzeit nicht verfügbar."}

    if "is offline" in normalized:
        return {"state": "offline", "message": "Offline"}

    if text == "Stream has not started":
        return {"state": "offline", "message": "Offline"}

    return {"state": "live", "message": "Live", "detail": text}


def _is_offline_title(title: str | None) -> bool:
    if not title:
        return False
    return "aktuell offline" in title.strip().lower()


def _fetch_decapi_text(endpoint: str, slug: str, timeout: int) -> str | None:
    url = _build_decapi_url(endpoint, slug)
    request_obj = urlrequest.Request(url, headers={"Accept": "text/plain"})
    try:
        with urlrequest.urlopen(request_obj, timeout=timeout) as upstream:
            return upstream.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:  # pragma: no cover - relies on external service
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        if body:
            return body
        return None
    except urlerror.URLError as exc:  # pragma: no cover - relies on external service
        app.logger.warning("Decapi fetch failed for %s/%s: %s", endpoint, slug, exc)
        return None


def _build_live_status_snapshot() -> dict[str, object]:
    _, member_index = get_talent_data()
    streamers: dict[str, dict[str, object]] = {}
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for slug, member in member_index.items():
        status_text = _fetch_decapi_text("uptime", slug, LIVE_STATUS_REQUEST_TIMEOUT)
        status = _normalize_decapi_status_text(status_text)
        title = ""
        game = ""
        if status.get("state") == "live":
            title = _fetch_decapi_text("title", slug, LIVE_STATUS_REQUEST_TIMEOUT) or ""
            if _is_offline_title(title):
                status = {"state": "offline", "message": "Offline"}
                title = ""
            if status.get("state") == "live":
                game = _fetch_decapi_text("game", slug, LIVE_STATUS_REQUEST_TIMEOUT) or ""
        streamers[slug] = {
            "state": status.get("state", "error"),
            "message": status.get("message", "Status derzeit nicht verfügbar."),
            "detail": status.get("detail", ""),
            "title": title.strip(),
            "game": game.strip(),
            "checked_at": now,
            "name": member.get("name") or slug,
        }
    return {"updated_at": now, "streamers": streamers}


def _write_live_status_snapshot(snapshot: dict[str, object]) -> None:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = LIVE_STATUS_FILE.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(LIVE_STATUS_FILE)


def _refresh_live_status_snapshot() -> None:
    snapshot = _build_live_status_snapshot()
    with _LIVE_STATUS_LOCK:
        _write_live_status_snapshot(snapshot)


def _load_live_status_snapshot() -> dict[str, object]:
    if not LIVE_STATUS_FILE.exists():
        return {}
    try:
        with LIVE_STATUS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _get_live_status_map() -> dict[str, dict[str, object]]:
    data = _load_live_status_snapshot()
    streamers = data.get("streamers") if isinstance(data, dict) else None
    if not isinstance(streamers, dict):
        return {}
    return streamers


def _build_twitch_embed_url(slug: str, host: str) -> str:
    parent_host = (host or "localhost").split(":", 1)[0]
    params = {
        "channel": slug,
        "parent": parent_host,
        "muted": "true",
    }
    return "https://player.twitch.tv/?" + urlencode(params)


def _live_status_worker() -> None:
    interval = max(10, LIVE_STATUS_POLL_INTERVAL_SECONDS)
    while True:
        try:
            _refresh_live_status_snapshot()
        except Exception:  # pragma: no cover - background worker resilience
            app.logger.exception("Failed to refresh live status snapshot")
        time.sleep(interval)


def _start_live_status_worker() -> None:
    global _LIVE_STATUS_THREAD_STARTED
    if LIVE_STATUS_POLL_INTERVAL_SECONDS <= 0:
        return
    if not LIVE_STATUS_FILE.exists():
        _refresh_live_status_snapshot()
    if _LIVE_STATUS_THREAD_STARTED:
        return
    with _LIVE_STATUS_LOCK:
        if _LIVE_STATUS_THREAD_STARTED:
            return
        worker = threading.Thread(
            target=_live_status_worker, name="live-status-worker", daemon=True
        )
        worker.start()
        _LIVE_STATUS_THREAD_STARTED = True


NETIM_MAIL_DEFAULTS = {
    "MAIL_SERVER": "mail1.netim.hosting",
    "MAIL_PORT": 465,
    "MAIL_USE_TLS": False,
    "MAIL_USE_SSL": True,
    "MAIL_USERNAME": "project@astralia.de",
    "MAIL_PASSWORD": "your_mail_password",
    "MAIL_DEFAULT_SENDER": "Astralia <noreply@astralia.de>",
}


app.config.setdefault(
    "MAIL_SERVER", os.environ.get("MAIL_SERVER", NETIM_MAIL_DEFAULTS["MAIL_SERVER"])
)
app.config.setdefault(
    "MAIL_PORT", _env_int("MAIL_PORT", NETIM_MAIL_DEFAULTS["MAIL_PORT"])
)
app.config.setdefault(
    "MAIL_USE_TLS", _env_bool("MAIL_USE_TLS", NETIM_MAIL_DEFAULTS["MAIL_USE_TLS"])
)
app.config.setdefault(
    "MAIL_USE_SSL", _env_bool("MAIL_USE_SSL", NETIM_MAIL_DEFAULTS["MAIL_USE_SSL"])
)
app.config.setdefault(
    "MAIL_USERNAME",
    os.environ.get("MAIL_USERNAME", NETIM_MAIL_DEFAULTS["MAIL_USERNAME"]),
)
app.config.setdefault(
    "MAIL_PASSWORD",
    os.environ.get("MAIL_PASSWORD", NETIM_MAIL_DEFAULTS["MAIL_PASSWORD"]),
)
app.config.setdefault(
    "MAIL_DEFAULT_SENDER",
    os.environ.get(
        "MAIL_DEFAULT_SENDER", NETIM_MAIL_DEFAULTS["MAIL_DEFAULT_SENDER"]
    ),
)

app.config.setdefault("BROTLI_QUALITY", 6)
app.config.setdefault("BROTLI_MIN_SIZE", 1400)

mail = Mail(app)
CONTACT_RECIPIENT = os.environ.get("CONTACT_RECIPIENT", "contact@astralia.de")
DECAPI_BASE_URL = "https://decapi.me/twitch"


@app.template_filter("nl2br")
def nl2br(value):
    """Convert line breaks in plain text to <br> tags for HTML rendering."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # splitlines() keeps compatibility with different newline styles
    escaped = escape(value)
    return Markup("<br>".join(escaped.splitlines()))


@app.template_filter("break_name")
def break_name(value):
    """Allow manual name line breaks using the backslash character."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    manual_breaks = value.replace("\\", "\n")
    return nl2br(manual_breaks)


BERLIN_TZ = ZoneInfo("Europe/Berlin")


def parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BERLIN_TZ)
    return parsed.astimezone(BERLIN_TZ)


@app.template_filter("german_datetime")
def german_datetime(value):
    dt = value if isinstance(value, datetime) else parse_iso_date(value)
    if dt is None:
        return ""
    return dt.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")



def _apply_basic_inline_markdown(text: str) -> str:
    """Render a very small subset of Markdown inline formatting.

    The implementation intentionally stays conservative: it only supports
    emphasis, strong emphasis and inline code while ensuring all other text is
    HTML-escaped. Any unmatched markers fall back to their literal characters so
    authors do not lose content if they forget a closing delimiter.
    """

    result: List[str] = []
    stack: List[Tuple[str, int]] = []
    i = 0
    length = len(text)

    def push(token: str, opening: str) -> None:
        stack.append((token, len(result)))
        result.append(opening)

    def pop(token: str, closing: str) -> bool:
        if stack and stack[-1][0] == token:
            stack.pop()
            result.append(closing)
            return True
        return False

    while i < length:
        char = text[i]

        if char == "\\" and i + 1 < length:
            result.append(escape(text[i + 1]))
            i += 2
            continue

        if char == "[":
            label_end = text.find("]", i + 1)
            if label_end != -1 and label_end + 1 < length and text[label_end + 1] == "(":
                href_end = text.find(")", label_end + 2)
                if href_end != -1:
                    label = escape(text[i + 1 : label_end])
                    href = escape(text[label_end + 2 : href_end].strip())
                    result.append(f"<a href=\"{href}\">{label}</a>")
                    i = href_end + 1
                    continue

        if text.startswith("**", i):
            if pop("**", "</strong>"):
                i += 2
                continue
            push("**", "<strong>")
            i += 2
            continue

        if text.startswith("__", i):
            if pop("__", "</strong>"):
                i += 2
                continue
            push("__", "<strong>")
            i += 2
            continue

        if char in {"*", "_"}:
            if pop(char, "</em>"):
                i += 1
                continue
            push(char, "<em>")
            i += 1
            continue

        if char == "`":
            if pop("`", "</code>"):
                i += 1
                continue
            push("`", "<code>")
            i += 1
            continue

        result.append(escape(char))
        i += 1

    while stack:
        token, index = stack.pop()
        result[index] = escape(token)

    return "".join(result)


def _render_markdown_basic(text: str) -> str:
    lines = text.splitlines()
    html_parts = []
    paragraph_buffer = []
    in_list = False

    def flush_paragraph():
        nonlocal paragraph_buffer
        if paragraph_buffer:
            # Preserve intentional line breaks inside a paragraph while still escaping
            # the raw text for safe HTML output.
            escaped_lines = [
                _apply_basic_inline_markdown(segment) for segment in paragraph_buffer if segment
            ]
            paragraph = "<br>".join(escaped_lines).strip()
            if paragraph:
                html_parts.append(f"<p>{paragraph}</p>")
        paragraph_buffer = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            flush_paragraph()
            continue

        if stripped.startswith("#"):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            flush_paragraph()
            level = len(stripped) - len(stripped.lstrip("#"))
            level = max(1, min(6, level))
            heading_text = stripped[level:].strip()
            heading_html = _apply_basic_inline_markdown(heading_text)
            html_parts.append(f"<h{level}>{heading_html}</h{level}>")
            continue

        if stripped.startswith("- "):
            flush_paragraph()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            item_text = stripped[2:].strip()
            item_html = _apply_basic_inline_markdown(item_text)
            html_parts.append(f"<li>{item_html}</li>")
            continue

        if in_list:
            html_parts.append("</ul>")
            in_list = False

        paragraph_buffer.append(stripped)

    if in_list:
        html_parts.append("</ul>")
    flush_paragraph()
    return "\n".join(part for part in html_parts if part)


def render_markdown(text) -> Markup:
    if text is None:
        return Markup("")
    source = str(text)
    if not source.strip():
        return Markup("")
    normalized = source.replace("\r\n", "\n")
    if markdown is not None:
        html = markdown.markdown(
            normalized,
            extensions=["extra", "sane_lists", "nl2br"],
            output_format="html5",
        )
        return Markup(html)
    return Markup(_render_markdown_basic(normalized))


@app.template_filter("markdown")
def markdown_filter(value):
    return render_markdown(value)


@app.template_filter("format_species")
def format_species(value):
    """Format species labels so long values wrap at natural separators."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)

    normalized = value.strip()
    normalized = re.sub(r"\s*/\s*", " / ", normalized)
    normalized = re.sub(r"\s*(?:[,;|+]|&|\band\b)\s*", " / ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    parts = [part for part in (segment.strip() for segment in normalized.split(" / ")) if part]
    if not parts:
        return ""

    markup_parts = []
    for part in parts:
        escaped_part = escape(part)
        escaped_part = escaped_part.replace("-", Markup("-&#8203;"))
        markup_parts.append(escaped_part)

    separator = Markup(" /<wbr> ")
    return separator.join(markup_parts)


@app.template_global()
def asset_url(filename: str) -> str:
    normalized = str(filename or "").lstrip("/")
    if normalized.startswith("static/"):
        normalized = normalized[len("static/") :]
    if not normalized:
        return ""
    version = app.config.get("ASSET_VERSION")
    if version:
        return url_for("static", filename=normalized, v=version)
    return url_for("static", filename=normalized)


def _safe_positive_int(value):
    try:
        number = int(str(value).strip())
        return number if number > 0 else None
    except Exception:
        return None


def _append_vary(existing: str | None, value: str) -> str:
    if not existing:
        return value

    entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
    if value in entries:
        return ", ".join(entries)
    entries.append(value)
    return ", ".join(entries)


def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


def _has_image_feature(feature_name: str) -> bool:
    if features is None:
        return False

    normalized = feature_name.lower()
    if SUPPORTED_PIL_FEATURES is not None and normalized not in SUPPORTED_PIL_FEATURES:
        return False

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Unknown feature .*",
                category=UserWarning,
            )
            return bool(features.check(normalized))
    except Exception:
        return False


@contextmanager
def _safe_image_open(path: Path):
    if Image is None:
        raise RuntimeError("Pillow is not available")

    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
        img = Image.open(path)

    try:
        yield img
    finally:
        img.close()


def _preferred_image_format(requested_format: str | None, accept_header: str) -> str | None:
    if Image is None:
        return None

    normalized_request = (requested_format or "").strip().lower()
    if normalized_request in {"webp", "avif", "jpeg", "jpg", "png"}:
        preferred = [normalized_request]
    else:
        preferred = []
        accept = (accept_header or "").lower()
        if "image/avif" in accept:
            preferred.append("avif")
        if "image/webp" in accept:
            preferred.append("webp")

    preferred.append(None)

    for candidate in preferred:
        if candidate == "avif" and _has_image_feature("avif"):
            return "avif"
        if candidate == "webp" and _has_image_feature("webp"):
            return "webp"
        if candidate in {"jpeg", "jpg"}:
            return "jpeg"
        if candidate == "png":
            return "png"

    return None


def _ensure_static_path(path: str) -> Path | None:
    normalized = str(path or "").strip().lstrip("/")
    if not normalized:
        return None

    candidate = (BASE_DIR / "static" / normalized).resolve()
    try:
        candidate.relative_to(BASE_DIR / "static")
    except ValueError:
        return None

    if candidate.is_file():
        return candidate
    return None


def _optimized_media_path(source: Path, width: int | None, height: int | None, fmt: str | None, quality: int) -> Path:
    digest = hashlib.sha1(
        f"{source}:{source.stat().st_mtime_ns}:{width}:{height}:{fmt}:{quality}".encode()
    ).hexdigest()
    suffix = fmt or source.suffix.lstrip(".") or "img"
    target_dir = OPTIMIZED_MEDIA_DIR / digest[:2]
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{digest}.{suffix}"


def _build_optimized_image(source: Path, destination: Path, width: int | None, height: int | None, fmt: str, quality: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _safe_image_open(source) as img:
        img = ImageOps.exif_transpose(img)
        original_mode = img.mode
        target_width = width or img.width
        target_height = height or img.height
        target_size = (min(target_width, img.width), min(target_height, img.height))

        if target_size[0] < img.width or target_size[1] < img.height:
            img.thumbnail(target_size, Image.LANCZOS)

        has_alpha = "A" in img.getbands() or "transparency" in img.info

        if fmt in {"jpeg", "jpg"}:
            mask = None
            if "A" in img.getbands():
                mask = img.getchannel("A")
            elif "transparency" in img.info:
                img = img.convert("RGBA")
                mask = img.getchannel("A")

            if mask:
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=mask)
                img = background
            elif img.mode not in {"RGB", "L"}:
                img = img.convert("RGB")

        elif fmt in {"webp", "avif", "png"}:
            if img.mode == "P":
                target_mode = "RGBA" if (fmt == "avif" and "A" in original_mode) or has_alpha else "RGB"
                img = img.convert(target_mode)
            elif has_alpha and img.mode not in {"RGBA", "LA"}:
                img = img.convert("RGBA")

        save_kwargs = {"optimize": True}
        if fmt in {"jpeg", "jpg", "webp", "avif"}:
            save_kwargs["quality"] = quality

        img.save(destination, format=fmt.upper(), **save_kwargs)


def _add_cache_headers(response: Response) -> Response:
    response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    return response


@app.template_global()
def optimized_asset_url(filename: str, *, width: int | None = None, height: int | None = None) -> str:
    normalized = str(filename or "").lstrip("/")
    if normalized.startswith("static/"):
        normalized = normalized[len("static/") :]
    if not normalized:
        return ""

    params = {}
    if width:
        params["w"] = width
    if height:
        params["h"] = height

    version = app.config.get("ASSET_VERSION")
    if version:
        params["v"] = version

    return url_for("serve_media_asset", path=normalized, **params)


def _absolute_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        scheme = request.scheme or "https"
        return f"{scheme}:{path}"
    if path.startswith("/static/"):
        return urljoin(request.url_root, path.lstrip("/"))
    return urljoin(request.url_root, path.lstrip("/"))


def _normalize_description(text: str, fallback: str = "") -> str:
    value = (text or fallback or "").strip()
    return re.sub(r"\s+", " ", value)


def _coerce_iso_datetime(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.isoformat()
            except ValueError:
                continue
    return ""


def _normalize_multiline_text(value) -> str:
    text = str(value or "")
    if "\n" in text:
        # Remove a leading blank line so dedent can strip the true common
        # indentation instead of bailing out because the first line is empty.
        text_lines = text.splitlines()
        if text_lines and not text_lines[0].strip():
            text_lines = text_lines[1:]
        text = "\n".join(text_lines)
        text = dedent(text)
    return text.strip()


def _sitemap_lastmod(value):
    iso_value = _coerce_iso_datetime(value)
    if not iso_value:
        return ""
    if "T" in iso_value:
        date_part = iso_value.split("T", 1)[0]
        if date_part:
            return date_part
    return iso_value


def build_seo_metadata(
    *,
    title=None,
    description=None,
    image=None,
    canonical=None,
    og_type="website",
    robots=None,
    published=None,
    modified=None,
    section=None,
    structured_data=None,
):
    settings = get_settings()
    site_name = settings.get("site_name") or DEFAULT_SITE_SETTINGS["site_name"]
    site_subtitle = settings.get("site_subtitle") or DEFAULT_SITE_SETTINGS["site_subtitle"]
    meta_settings = settings.get("meta", {})
    default_description = meta_settings.get("default_description") or settings.get("site_tagline")
    description_value = _normalize_description(description, default_description)
    base_title = site_name if not site_subtitle else f"{site_name} — {site_subtitle}"
    page_title = (title or "").strip()
    if page_title:
        full_title = f"{page_title} — {site_name}"
    else:
        full_title = base_title
        page_title = site_name

    canonical_url = canonical or request.base_url
    canonical_url = _absolute_url(canonical_url)

    image_source = image or meta_settings.get("default_image")
    if image_source:
        if image_source.startswith(("http://", "https://", "//")):
            image_url = _absolute_url(image_source)
        elif image_source.startswith("/static/"):
            image_url = _absolute_url(image_source)
        else:
            normalized_image = image_source.lstrip("/")
            image_url = _absolute_url(asset_url(normalized_image))
    else:
        image_url = ""

    published_iso = _coerce_iso_datetime(published)
    modified_iso = _coerce_iso_datetime(modified)

    meta = {
        "title": full_title,
        "page_title": page_title,
        "description": description_value,
        "image": image_url,
        "canonical": canonical_url,
        "og_type": og_type or "website",
        "twitter_card": "summary_large_image",
        "robots": robots,
        "site_name": site_name,
    }
    if published_iso:
        meta["published_time"] = published_iso
    if modified_iso:
        meta["modified_time"] = modified_iso
    if section:
        meta["section"] = section

    if structured_data:
        if isinstance(structured_data, (list, tuple)):
            meta["structured_data"] = [item for item in structured_data if item]
        else:
            meta["structured_data"] = [structured_data]

    return meta


def build_article_schema(
    *,
    headline,
    description,
    image,
    canonical,
    date_published,
    date_modified,
    publisher,
):
    schema = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "headline": headline,
        "description": description,
    }
    images = [image] if image else []
    if images:
        schema["image"] = images
    schema["datePublished"] = date_published
    if date_modified:
        schema["dateModified"] = date_modified
    if publisher:
        schema["publisher"] = publisher
    return schema


def build_product_schema(
    *,
    name,
    description,
    image,
    canonical,
    price,
    currency="EUR",
    availability="https://schema.org/InStock",
):
    schema = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "description": description,
        "url": canonical,
    }
    if image:
        schema["image"] = [image]
    offer = {
        "@type": "Offer",
        "url": canonical,
        "availability": availability,
    }
    if price is not None:
        try:
            offer["price"] = f"{float(price):.2f}"
            offer["priceCurrency"] = currency
        except (TypeError, ValueError):
            offer["price"] = price
    schema["offers"] = offer
    return schema


def _compute_meta_defaults(settings):
    site_name = settings.get("site_name") or DEFAULT_SITE_SETTINGS["site_name"]
    site_tagline = settings.get("site_tagline") or DEFAULT_SITE_SETTINGS["site_tagline"]
    meta_value = settings.get("meta")
    meta_settings = meta_value if isinstance(meta_value, dict) else {}
    default_description = meta_settings.get("default_description") or site_tagline
    default_image_value = meta_settings.get("default_image") or "images/logo.png"
    if default_image_value.startswith(("http://", "https://", "//")):
        default_social_image = _absolute_url(default_image_value)
    elif default_image_value.startswith("/static/"):
        default_social_image = _absolute_url(default_image_value)
    else:
        default_social_image = _absolute_url(asset_url(default_image_value.lstrip("/")))

    org_value = meta_settings.get("organization") if isinstance(meta_settings, dict) else {}
    organization_meta = org_value if isinstance(org_value, dict) else {}
    org_type = organization_meta.get("type") or "Organization"
    org_name = organization_meta.get("name") or site_name
    org_url = request.url_root.rstrip("/") if request.url_root else request.host_url.rstrip("/")
    org_logo_value = organization_meta.get("logo") or default_image_value
    if org_logo_value.startswith(("http://", "https://", "//")):
        org_logo_url = _absolute_url(org_logo_value)
    elif org_logo_value.startswith("/static/"):
        org_logo_url = _absolute_url(org_logo_value)
    else:
        org_logo_url = _absolute_url(asset_url(org_logo_value.lstrip("/")))
    organization_schema = {
        "@context": "https://schema.org",
        "@type": org_type,
        "name": org_name,
        "url": org_url,
    }
    if org_logo_url:
        organization_schema["logo"] = {"@type": "ImageObject", "url": org_logo_url}
    if default_description:
        organization_schema["description"] = default_description
    same_as_value = organization_meta.get("same_as") if isinstance(organization_meta, dict) else []
    if isinstance(same_as_value, (list, tuple)):
        same_as = [link for link in same_as_value if link]
        if same_as:
            organization_schema["sameAs"] = same_as
    publisher_schema = {
        "@type": organization_schema.get("@type", "Organization"),
        "name": organization_schema.get("name"),
    }
    if "logo" in organization_schema:
        publisher_schema["logo"] = organization_schema["logo"]
    if "url" in organization_schema:
        publisher_schema["url"] = organization_schema["url"]
    return {
        "description": default_description,
        "image": default_social_image,
        "organization": organization_schema,
        "image_source": default_image_value,
        "publisher": publisher_schema,
    }


# --- Simple content loaders for demo data (replace with DB later) ---
BASE_DIR = Path(__file__).parent
CONTENT_DIR = BASE_DIR / "content"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
OPTIMIZED_MEDIA_DIR = BASE_DIR / "static" / "optimized"
PRIVACY_FILE = "privacy.md"
SETTINGS_FILE = "settings.yaml"
LIVE_STATUS_FILE = CONTENT_DIR / "live_status.json"
LIVE_STATUS_POLL_INTERVAL_SECONDS = _env_int("LIVE_STATUS_POLL_INTERVAL", 60)
LIVE_STATUS_REQUEST_TIMEOUT = _env_int("LIVE_STATUS_REQUEST_TIMEOUT", 6)

DEFAULT_IMAGE_MAX_WIDTH = _env_int("IMAGE_MAX_WIDTH", 1920)
DEFAULT_IMAGE_MAX_HEIGHT = _env_int("IMAGE_MAX_HEIGHT", 1080)
DEFAULT_IMAGE_QUALITY = _env_int("IMAGE_QUALITY", 82)
DEFAULT_MAX_IMAGE_PIXELS = _env_int("IMAGE_MAX_PIXELS", 200_000_000)
DEFAULT_BROTLI_QUALITY = _env_int("BROTLI_QUALITY", 5)
DEFAULT_BROTLI_MIN_SIZE = _env_int("BROTLI_MIN_SIZE", 512)

COMPRESSIBLE_MIMETYPES = {
    "text/plain",
    "text/html",
    "text/css",
    "text/xml",
    "text/javascript",
    "application/javascript",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/manifest+json",
    "image/svg+xml",
}

SUPPORTED_PIL_FEATURES = None
if features is not None:
    try:
        supported_info = features.get_supported()
        if isinstance(supported_info, dict):
            SUPPORTED_PIL_FEATURES = {name.lower() for name in supported_info.keys()}
    except Exception:  # pragma: no cover - optional pillow introspection
        SUPPORTED_PIL_FEATURES = None

if Image is not None:
    try:
        Image.MAX_IMAGE_PIXELS = None if DEFAULT_MAX_IMAGE_PIXELS <= 0 else DEFAULT_MAX_IMAGE_PIXELS
    except Exception:  # pragma: no cover - pillow may not expose the attribute
        pass

ASSET_VERSION = _compute_asset_version()
app.config["ASSET_VERSION"] = ASSET_VERSION

ADMIN_USERNAME = os.environ.get("LUMY_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("LUMY_ADMIN_PASS", "5WyDfT3TJ2kqa9aZmF22")
MAINTENANCE_USERNAME = os.environ.get("LUMY_MAINT_USER", "visitor")
MAINTENANCE_PASSWORD = os.environ.get("LUMY_MAINT_PASS", "SSlz9ZWdPDYDJBHvFBJ8")
ARTWORKS_USERNAME = os.environ.get("LUMY_ARTWORKS_USER", "gallery")
ARTWORKS_PASSWORD = os.environ.get("LUMY_ARTWORKS_PASS", "ovxEneAkzWZZzIgPb2BE")
USER_DB_PATH = BASE_DIR / "data" / "users.db"


def _db_row_to_user(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _ensure_user_db() -> None:
    USER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _get_user_by_username(username: str):
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    _ensure_user_db()
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin, created_at, updated_at FROM users WHERE lower(username)=?",
            (normalized,),
        ).fetchone()
    return _db_row_to_user(row)


def _list_users():
    _ensure_user_db()
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at, updated_at FROM users ORDER BY lower(username)"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _create_user(username: str, password: str, is_admin: bool = False):
    normalized = _slugify(username)
    if not normalized:
        return False, "Bitte einen gültigen Benutzernamen angeben."
    if len(password or "") < 8:
        return False, "Passwort muss mindestens 8 Zeichen lang sein."
    if _get_user_by_username(normalized):
        return False, "Benutzername existiert bereits."
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    password_hash = generate_password_hash(password)
    _ensure_user_db()
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (normalized, password_hash, 1 if is_admin else 0, now, now),
        )
        conn.commit()
    return True, "Benutzer wurde erstellt."


def _update_user_password(username: str, new_password: str):
    user = _get_user_by_username(username)
    if not user:
        return False, "Benutzer nicht gefunden."
    if len(new_password or "") < 8:
        return False, "Passwort muss mindestens 8 Zeichen lang sein."
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    password_hash = generate_password_hash(new_password)
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (password_hash, now, user["id"]),
        )
        conn.commit()
    return True, "Passwort wurde aktualisiert."


def _delete_user(username: str):
    user = _get_user_by_username(username)
    if not user:
        return False, "Benutzer nicht gefunden."
    with sqlite3.connect(USER_DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user["id"],))
        conn.commit()
    return True, "Benutzer wurde gelöscht."

SHOP_EFFECTS = {
    "": {"label": "", "class": ""},
    "stars": {"label": "✨ Highlight", "class": "shop-effect-stars"},
    "hot": {"label": "🔥 Hot", "class": "shop-effect-hot"},
    "discounted": {"label": "💸 Rabatt", "class": "shop-effect-discount"},
    "limited": {"label": "⏳ Limitiert", "class": "shop-effect-limited"},
    "digital": {"label": "💾 Digital", "class": "shop-effect-digital"},
}

SERVICE_BADGES = {
    "discounted": {"label": "Rabatt", "class": "service-badge--discounted"},
    "limited": {"label": "Wenige Plätze", "class": "service-badge--limited"},
}

CALENDAR_ICONS = {
    "heart": {"label": "Herz", "file": "icons/calendar-heart.svg"},
    "star": {"label": "Stern", "file": "icons/calendar-star.svg"},
    "cake": {"label": "Torte", "file": "icons/calendar-cake.svg"},
}

DEFAULT_CALENDAR_ICON = "heart"

CALENDAR_RECURRENCE_OPTIONS = {
    "none": "Keine Wiederholung",
    "weekly": "Wöchentlich",
    "monthly": "Monatlich",
    "yearly": "Jährlich",
}

DEFAULT_SITE_SETTINGS = {
    "site_name": "Astralia.Live",
    "site_tagline": "Voices from the silence of the cosmos, carried by the light of forgotten stars",
    "site_subtitle": "German VTuber Hub",
    "maintenance_mode": False,
    "shop_enabled": True,
    "artworks_panel_enabled": True,
    "talents_no_teams": False,
    "meta": {
        "default_description": "Astralia ist der deutsche VTuber Hub für Events, Projekte und Talente im Streaming-Universum.",
        "default_image": "images/logo.png",
        "organization": {
            "type": "Organization",
            "logo": "images/logo.png",
        },
    },
    "footer": {
        "about_heading": "About",
        "about_text": "Voices from the silence of the cosmos, carried by the light of forgotten stars",
        "social_heading": "Socials",
        "socials": [
            {"label": "Twitch", "url": "https://twitch.tv/"},
            {"label": "YouTube", "url": "https://youtube.com/"},
            {"label": "X (Twitter)", "url": "https://x.com/"},
            {"label": "Bluesky", "url": "https://bsky.app/"},
        ],
        "legal_heading": "Rechtliches",
        "legal_links": [
            {"label": "Impressum", "url": "/impressum"},
            {"label": "Datenschutzerklärung", "url": "/datenschutz"},
        ],
        "fine_print": "© {site_name} {year} • Made with stardust.",
    },
    "starfield": {
        "density_divisor": 9000,
        "min_count": 90,
        "speed_min": 0.03,
        "speed_max": 0.12,
        "fast_fraction": 0.2,
        "fast_multiplier": 2.5,
    },
}

DEFAULT_HOMEPAGE_SECTIONS = [
    {
        "id": "live",
        "heading": "Jetzt Live",
        "cta_label": "",
        "cta_url": "",
        "enabled": True,
        "order": 0,
    },
    {
        "id": "projects",
        "heading": "Aktuelle Projekte",
        "cta_label": "Alle Projekte →",
        "cta_url": "/projects",
        "enabled": True,
        "order": 1,
    },
    {
        "id": "news",
        "heading": "Neuigkeiten",
        "cta_label": "Alle News →",
        "cta_url": "/news",
        "enabled": True,
        "order": 2,
    },
    {
        "id": "calendar",
        "heading": "Kalender",
        "cta_label": "",
        "cta_url": "",
        "enabled": True,
        "order": 3,
    },
    {
        "id": "partners",
        "heading": "Partner",
        "cta_label": "Alle Partner →",
        "cta_url": "/partners",
        "enabled": True,
        "order": 4,
    },
]

DEFAULT_HOMEPAGE_SETTINGS = {
    "hero": {
        "logo": "images/banner-logo.gif",
        "subtitle": DEFAULT_SITE_SETTINGS["site_tagline"],
        "kicker": DEFAULT_SITE_SETTINGS["site_subtitle"],
        "background_image": "",
        "primary_button": {
            "label": "Talente ansehen",
            "url": "/talents",
        },
        "secondary_button": {
            "label": "Projekte entdecken",
            "url": "/projects",
        },
        "tertiary_button": {
            "label": "Zum Discord",
            "url": "https://dc.astralia.de",
        },
    },
    "sections": DEFAULT_HOMEPAGE_SECTIONS,
    "calendar": {"events": []},
}


def _normalize_date_string(value):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _normalize_time_string(value):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    if not text:
        return ""
    normalized = text.replace(" Uhr", "").strip()
    for fmt in ("%H:%M", "%H.%M", "%H:%M:%S", "%H:%M:%S.%f"):
        try:
            return datetime.strptime(normalized, fmt).time().strftime("%H:%M")
        except ValueError:
            continue
    if re.fullmatch(r"\d{1,2}:\d{2}", normalized):
        hours, minutes = normalized.split(":", 1)
        try:
            parsed = datetime.strptime(f"{int(hours):02d}:{int(minutes):02d}", "%H:%M")
            return parsed.time().strftime("%H:%M")
        except ValueError:
            return ""
    return ""


def _normalize_recurrence(value):
    if not value:
        return ""
    if isinstance(value, str):
        normalized = value.strip().lower()
    elif isinstance(value, dict):
        normalized = str(value.get("type", "")).strip().lower()
    else:
        normalized = str(value).strip().lower()
    if normalized in {"", "none", "kein", "false", "nein"}:
        return ""
    if normalized in {"weekly", "woechentlich", "wöchentlich"}:
        return "weekly"
    if normalized in {"monthly", "monatlich"}:
        return "monthly"
    if normalized in {"yearly", "jaehrlich", "jährlich", "annually"}:
        return "yearly"
    return ""


def _format_time_display(value):
    normalized = _normalize_time_string(value)
    if not normalized:
        return ""
    try:
        return datetime.strptime(normalized, "%H:%M").strftime("%H:%M Uhr")
    except ValueError:
        return normalized


def _parse_scalar(value: str):
    if value.startswith(("\"", "'")) and value.endswith(value[0]):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return ast.literal_eval(value)
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None"} or value == "~":
        return None
    try:
        if value.startswith(("0x", "0o", "0b")):
            return int(value, 0)
        if "." in value and value.replace(".", "", 1).isdigit():
            return float(value)
        if value.isdigit():
            return int(value)
    except ValueError:
        pass
    return value


def _parse_block(lines, start, indent):
    sequence = None
    mapping = None
    i = start
    total = len(lines)
    while i < total:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < indent:
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            if mapping is not None:
                raise ValueError("List entry encountered after mapping entries at same level")
            if sequence is None:
                sequence = []
            content = stripped[2:].strip()
            i += 1
            if not content:
                item, i = _parse_block(lines, i, current_indent + 2)
                sequence.append(item)
                continue
            if ":" in content:
                key, value = content.split(":", 1)
                key = key.strip()
                value = value.strip()
                item = {}
                if value:
                    item[key] = _parse_scalar(value)
                else:
                    sub, i = _parse_block(lines, i, current_indent + 2)
                    item[key] = sub
                    sequence.append(item)
                    continue
                sub, new_i = _parse_block(lines, i, current_indent + 2)
                if isinstance(sub, dict):
                    item.update(sub)
                elif sub is None or (isinstance(sub, list) and not sub):
                    pass
                else:
                    raise ValueError("Unsupported nested structure inside list item")
                i = new_i
                sequence.append(item)
            else:
                sequence.append(_parse_scalar(content))
            continue
        else:
            if sequence is not None:
                raise ValueError("Mapping entry encountered after list entries at same level")
            if mapping is None:
                mapping = {}
            if ":" not in stripped:
                raise ValueError(f"Cannot parse line: {stripped}")
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            i += 1
            if value:
                mapping[key] = _parse_scalar(value)
            else:
                sub, i = _parse_block(lines, i, current_indent + 2)
                mapping[key] = sub
            continue
    if sequence is not None:
        return sequence, i
    if mapping is not None:
        return mapping, i
    return None, i


def _simple_yaml(text: str):
    lines = text.splitlines()
    parsed, _ = _parse_block(lines, 0, 0)
    return parsed


def load_yaml(name):
    f = CONTENT_DIR / name
    if not f.exists():
        return []
    text = f.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text) or []
    return _simple_yaml(text) or []


def load_markdown(name: str) -> str:
    f = CONTENT_DIR / name
    if not f.exists():
        return ""
    return f.read_text(encoding="utf-8")


def _parse_price_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace("€", "").replace(" ", "")
        # allow dots for thousands separator and comma for decimals
        cleaned = cleaned.replace(".", "").replace(",", ".")
        match = re.findall(r"-?[0-9]+(?:\.[0-9]+)?", cleaned)
        if not match:
            return None
        try:
            return float(match[0])
        except ValueError:
            return None
    return None


def _format_price_value(value):
    if value is None:
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    integer_part, fractional_part = divmod(round(amount * 100), 100)
    euros = f"{int(integer_part):,}".replace(",", ".")
    cents = f"{int(fractional_part):02d}"
    return f"€{euros},{cents}"


def _parse_int(value, minimum=None):
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None:
        result = max(minimum, result)
    return result


def _clean_price_input(raw, fallback=None):
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return None
    parsed = _parse_price_value(text)
    if parsed is None:
        return fallback
    return round(parsed, 2)


def _compact_dict(data):
    compacted = {}
    for key, value in data.items():
        if value in (None, "", []):
            continue
        compacted[key] = value
    return compacted


def _format_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    special_chars = ":-{}[],#&*!|>\'\"%@`"
    if not text:
        return "''"
    if text.strip() != text or any(ch in text for ch in special_chars) or "\n" in text:
        return repr(text)
    return text


def _dump_yaml(data, indent=0):
    lines = []
    spacer = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{spacer}{key}:")
                lines.extend(_dump_yaml(value, indent + 2))
            elif isinstance(value, (dict, list)):
                if isinstance(value, list):
                    lines.append(f"{spacer}{key}: []")
                else:
                    lines.append(f"{spacer}{key}: {{}}")
            else:
                lines.append(f"{spacer}{key}: {_format_scalar(value)}")
    elif isinstance(data, list):
        if not data:
            lines.append(f"{spacer}[]")
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"{spacer}-")
                lines.extend(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{spacer}- {_format_scalar(item)}")
    else:
        lines.append(f"{spacer}{_format_scalar(data)}")
    return lines


def save_yaml(name, data):
    f = CONTENT_DIR / name
    if yaml is not None:
        dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    else:
        dumped = "\n".join(_dump_yaml(data)) + "\n"
    f.write_text(dumped, encoding="utf-8")


def save_markdown(name: str, text: str) -> None:
    f = CONTENT_DIR / name
    normalized = "" if text is None else str(text)
    normalized = normalized.replace("\r\n", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized = f"{normalized}\n"
    f.write_text(normalized, encoding="utf-8")


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _parse_float_setting(value, default, *, min_value=None, max_value=None):
    try:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError
            cleaned = cleaned.replace(",", ".")
            number = float(cleaned)
        else:
            number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if min_value is not None:
        number = max(min_value, number)
    if max_value is not None:
        number = min(max_value, number)
    return number


def _parse_int_setting(value, default, *, min_value=None, max_value=None):
    number = _parse_float_setting(
        value, default, min_value=min_value, max_value=max_value
    )
    return int(round(number))


@lru_cache(maxsize=1)
def get_settings():
    raw = load_yaml(SETTINGS_FILE) or {}
    if not isinstance(raw, dict):
        raw = {}
    merged = deepcopy(DEFAULT_SITE_SETTINGS)

    def _merge(target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                _merge(target[key], value)
            else:
                target[key] = value

    _merge(merged, raw)

    footer = merged.get("footer", {})
    about_text = footer.get("about_text")
    if isinstance(about_text, str):
        footer["about_text"] = about_text.strip()
    else:
        footer["about_text"] = ""
    socials = []
    for entry in footer.get("socials", []):
        if not isinstance(entry, dict):
            continue
        label = (entry.get("label") or "").strip()
        url_value = (entry.get("url") or "").strip()
        if label and url_value:
            socials.append({"label": label, "url": url_value})
    footer["socials"] = socials

    legal_links = []
    for entry in footer.get("legal_links", []):
        if not isinstance(entry, dict):
            continue
        label = (entry.get("label") or "").strip()
        url_value = (entry.get("url") or "").strip()
        if label and url_value:
            legal_links.append({"label": label, "url": url_value})
    footer["legal_links"] = legal_links

    merged["footer"] = footer

    star_defaults = DEFAULT_SITE_SETTINGS.get("starfield", {})
    star_raw = merged.get("starfield")
    if not isinstance(star_raw, dict):
        star_raw = {}
    density_default = star_defaults.get("density_divisor", 9000)
    min_count_default = star_defaults.get("min_count", 90)
    speed_min_default = star_defaults.get("speed_min", 0.03)
    speed_max_default = star_defaults.get("speed_max", 0.12)
    fast_fraction_default = star_defaults.get("fast_fraction", 0.2)
    fast_multiplier_default = star_defaults.get("fast_multiplier", 2.5)

    starfield = {
        "density_divisor": _parse_float_setting(
            star_raw.get("density_divisor"),
            density_default,
            min_value=500,
            max_value=50000,
        ),
        "min_count": _parse_int_setting(
            star_raw.get("min_count"),
            min_count_default,
            min_value=10,
            max_value=5000,
        ),
        "speed_min": _parse_float_setting(
            star_raw.get("speed_min"),
            speed_min_default,
            min_value=0.001,
            max_value=5,
        ),
        "speed_max": _parse_float_setting(
            star_raw.get("speed_max"),
            speed_max_default,
            min_value=0.001,
            max_value=10,
        ),
        "fast_fraction": _parse_float_setting(
            star_raw.get("fast_fraction"),
            fast_fraction_default,
            min_value=0,
            max_value=1,
        ),
        "fast_multiplier": _parse_float_setting(
            star_raw.get("fast_multiplier"),
            fast_multiplier_default,
            min_value=1,
            max_value=10,
        ),
    }
    if starfield["speed_max"] < starfield["speed_min"]:
        starfield["speed_max"] = starfield["speed_min"]
    merged["starfield"] = starfield

    meta_settings = merged.get("meta", {})
    if not isinstance(meta_settings, dict):
        meta_settings = {}
    default_meta = DEFAULT_SITE_SETTINGS.get("meta", {})
    description = meta_settings.get("default_description") or default_meta.get("default_description", "")
    meta_settings["default_description"] = description.strip()
    default_image = meta_settings.get("default_image") or default_meta.get("default_image")
    if isinstance(default_image, str):
        meta_settings["default_image"] = default_image.strip()
    organization_defaults = default_meta.get("organization", {})
    organization_meta = meta_settings.get("organization")
    if not isinstance(organization_meta, dict):
        organization_meta = {}
    merged_org = dict(organization_defaults)
    merged_org.update({k: v for k, v in organization_meta.items() if v})
    meta_settings["organization"] = merged_org
    merged["meta"] = meta_settings
    return merged


def save_settings(data):
    save_yaml(SETTINGS_FILE, data)
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_homepage_settings():
    raw = load_yaml("homepage.yaml") or {}
    data = deepcopy(DEFAULT_HOMEPAGE_SETTINGS)
    if not isinstance(raw, dict):
        raw = {}

    hero_defaults = data.get("hero", {})
    hero_raw = raw.get("hero")
    if isinstance(hero_raw, dict):
        for key, value in hero_raw.items():
            if key in {"primary_button", "secondary_button", "tertiary_button"}:
                button_defaults = hero_defaults.get(key, {})
                if isinstance(value, dict):
                    button_defaults.update({
                        "label": (value.get("label") or "").strip(),
                        "url": (value.get("url") or "").strip(),
                    })
                hero_defaults[key] = button_defaults
            else:
                hero_defaults[key] = (value or "").strip() if isinstance(value, str) else value
    hero_defaults.setdefault("logo", DEFAULT_HOMEPAGE_SETTINGS["hero"].get("logo", ""))
    hero_defaults.setdefault(
        "background_image", DEFAULT_HOMEPAGE_SETTINGS["hero"].get("background_image", "")
    )
    logo_value = hero_defaults.get("logo")
    hero_defaults["logo"] = logo_value.strip() if isinstance(logo_value, str) else ""
    background_value = hero_defaults.get("background_image")
    hero_defaults["background_image"] = (
        background_value.strip() if isinstance(background_value, str) else ""
    )
    data["hero"] = hero_defaults

    sections_raw = raw.get("sections")
    merged_sections = []
    if isinstance(sections_raw, list):
        for entry in sections_raw:
            if not isinstance(entry, dict):
                continue
            section_id = entry.get("id") or ""
            default_section = None
            for default in DEFAULT_HOMEPAGE_SECTIONS:
                if default["id"] == section_id:
                    default_section = deepcopy(default)
                    break
            if default_section is None:
                default_section = {
                    "id": section_id or f"section-{len(merged_sections) + 1}",
                    "heading": entry.get("heading", ""),
                    "cta_label": entry.get("cta_label", ""),
                    "cta_url": entry.get("cta_url", ""),
                    "enabled": entry.get("enabled", True),
                    "order": entry.get("order", len(merged_sections) + 1),
                }
            else:
                default_section.update(entry)
            try:
                default_section["order"] = int(default_section.get("order", 0))
            except (TypeError, ValueError):
                default_section["order"] = len(merged_sections) + 1
            default_section["enabled"] = bool(default_section.get("enabled", True))
            merged_sections.append(default_section)
    if not merged_sections:
        merged_sections = [deepcopy(item) for item in DEFAULT_HOMEPAGE_SECTIONS]
    else:
        existing_ids = {section.get("id") for section in merged_sections}
        for default in DEFAULT_HOMEPAGE_SECTIONS:
            if default["id"] not in existing_ids:
                merged_sections.append(deepcopy(default))

    merged_sections.sort(key=lambda item: (item.get("order", 0), item.get("heading", "")))
    data["sections"] = merged_sections

    calendar_defaults = deepcopy(DEFAULT_HOMEPAGE_SETTINGS.get("calendar", {"events": []}))
    calendar_raw = raw.get("calendar")
    events = []
    if isinstance(calendar_raw, dict):
        raw_events = calendar_raw.get("events")
        if isinstance(raw_events, list):
            for index, entry in enumerate(raw_events):
                if not isinstance(entry, dict):
                    continue
                iso_date = _normalize_date_string(entry.get("date") or entry.get("day"))
                if not iso_date:
                    continue
                icon_key = str(entry.get("icon") or DEFAULT_CALENDAR_ICON).strip().lower()
                if icon_key not in CALENDAR_ICONS:
                    icon_key = DEFAULT_CALENDAR_ICON
                entry_id = entry.get("id")
                if isinstance(entry_id, str):
                    entry_id = entry_id.strip() or f"event-{index + 1}"
                elif not entry_id:
                    entry_id = f"event-{index + 1}"
                label_source = entry.get("label") or entry.get("title") or ""
                label_text = label_source.strip() if isinstance(label_source, str) else str(label_source).strip()
                url_source = entry.get("url") or entry.get("link") or ""
                url_text = url_source.strip() if isinstance(url_source, str) else str(url_source).strip()
                time_value = _normalize_time_string(entry.get("time"))
                recurrence_value = _normalize_recurrence(entry.get("recurrence"))
                event_payload = {
                    "id": entry_id,
                    "date": iso_date,
                    "label": label_text,
                    "icon": icon_key,
                    "url": url_text,
                }
                if time_value:
                    event_payload["time"] = time_value
                if recurrence_value:
                    event_payload["recurrence"] = recurrence_value
                events.append(event_payload)
    events.sort(key=lambda item: (item.get("date", ""), item.get("time") or ""))
    calendar_defaults["events"] = events
    data["calendar"] = calendar_defaults
    return data


def save_homepage_settings(data):
    save_yaml("homepage.yaml", data)
    get_homepage_settings.cache_clear()


def get_shop_items():
    raw_items = load_yaml("shop.yaml") or []
    if not isinstance(raw_items, list):
        raw_items = []
    items = []
    seen_slugs = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title", "Unbenannt")
        slug = entry.get("slug") or _slugify(title)
        if not slug:
            slug = f"item-{uuid4().hex[:8]}"
        base_slug = slug
        suffix = 2
        while slug in seen_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        seen_slugs.add(slug)
        price_value = _parse_price_value(entry.get("price"))
        price_display = _format_price_value(price_value) if price_value is not None else (entry.get("price") or "")
        original_value = _parse_price_value(entry.get("original_price"))
        original_display = (
            _format_price_value(original_value)
            if original_value is not None
            else (entry.get("original_price") or "")
        )
        discount_percent = ""
        if (
            price_value is not None
            and original_value is not None
            and original_value > 0
            and price_value < original_value
        ):
            reduction = max(0, min(99, round(100 - (price_value / original_value) * 100)))
            if reduction:
                discount_percent = f"-{int(reduction)}%"
        max_per_order = entry.get("max_per_order")
        try:
            max_per_order = int(max_per_order)
        except (TypeError, ValueError):
            max_per_order = 0
        stock_raw = entry.get("stock")
        try:
            stock_value = int(stock_raw)
        except (TypeError, ValueError):
            stock_value = None
        if isinstance(stock_value, int):
            normalized_stock = stock_value if stock_value >= 0 else 0
        else:
            normalized_stock = None
        effect = entry.get("effect", "") or ""
        effect_info = SHOP_EFFECTS.get(effect, SHOP_EFFECTS[""])
        options_data = []
        raw_options = entry.get("options") or []
        for idx, option in enumerate(raw_options):
            if isinstance(option, dict):
                label = option.get("label") or option.get("name") or ""
                note = option.get("note") or option.get("description") or ""
                opt_price_value = _parse_price_value(option.get("price"))
            else:
                label = str(option).strip()
                note = ""
                opt_price_value = None
            if not label:
                continue
            options_data.append(
                {
                    "id": f"{slug}-opt-{idx+1}",
                    "label": label,
                    "note": note,
                    "price_value": opt_price_value,
                    "price_display": _format_price_value(opt_price_value)
                    if opt_price_value is not None
                    else (option.get("price") if isinstance(option, dict) else ""),
                }
            )
        items.append(
            {
                "slug": slug,
                "title": title,
                "streamer": entry.get("streamer", ""),
                "description": entry.get("description", ""),
                "est_arrival": entry.get("est_arrival", ""),
                "price": price_display,
                "price_value": price_value,
                "original_price": original_display,
                "original_price_value": original_value,
                "discount_label": discount_percent,
                "discount_percent": discount_percent,
                "image": entry.get("image", ""),
                "badge": entry.get("badge", ""),
                "purchase_url": entry.get("purchase_url") or entry.get("url", ""),
                "options": options_data,
                "max_per_order": max_per_order if max_per_order > 0 else 0,
                "stock": normalized_stock,
                "sold_out": normalized_stock == 0 if normalized_stock is not None else False,
                "effect": effect if effect in SHOP_EFFECTS else "",
                "effect_label": effect_info.get("label", ""),
                "effect_class": effect_info.get("class", ""),
            }
        )
    return items


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("is_admin") or session.get("artworks_manager"):
            return view(*args, **kwargs)

        next_target = request.full_path.rstrip("?") if request else None
        login_endpoint = "admin_login"
        if request.args.get("tab") == "artworks":
            login_endpoint = "gallery_login"
        return redirect(url_for(login_endpoint, next=next_target or request.path))

    return wrapped


def _save_upload(file_storage, prefix):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if not filename:
        return None
    ext = Path(filename).suffix
    unique_name = f"{prefix}-{uuid4().hex}{ext}" if ext else f"{prefix}-{uuid4().hex}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / unique_name
    file_storage.save(destination)
    return f"uploads/{unique_name}"


def _resolve_media_url(path):
    if not path:
        return ""

    text = str(path).strip()
    if not text:
        return ""

    if text.startswith(("http://", "https://", "//", "data:", "mailto:")):
        return text

    if text.startswith("/"):
        return text

    normalized = text.lstrip("/")
    if normalized.startswith("static/"):
        normalized = normalized[len("static/") :]

    max_width = DEFAULT_IMAGE_MAX_WIDTH if DEFAULT_IMAGE_MAX_WIDTH > 0 else None
    max_height = DEFAULT_IMAGE_MAX_HEIGHT if DEFAULT_IMAGE_MAX_HEIGHT > 0 else None

    return optimized_asset_url(normalized, width=max_width, height=max_height)


@app.route("/api/live/uptime/<slug>")
def live_uptime(slug):
    return _proxy_decapi_request("uptime", slug)


@app.route("/api/live/title/<slug>")
def live_title(slug):
    return _proxy_decapi_request("title", slug)


@app.route("/api/live/game/<slug>")
def live_game(slug):
    return _proxy_decapi_request("game", slug)


@app.route("/api/live/status")
def live_status_snapshot():
    data = _load_live_status_snapshot()
    payload = json.dumps(data or {}, ensure_ascii=False)
    response = Response(payload, mimetype="application/json")
    return _add_live_api_headers(response)


@app.route("/media/<path:path>")
def serve_media_asset(path):
    source_path = _ensure_static_path(path)
    if not source_path:
        abort(404)

    requested_width = _safe_positive_int(request.args.get("w") or request.args.get("width"))
    requested_height = _safe_positive_int(request.args.get("h") or request.args.get("height"))
    quality = _safe_positive_int(request.args.get("q") or request.args.get("quality"))

    width = requested_width or (DEFAULT_IMAGE_MAX_WIDTH if DEFAULT_IMAGE_MAX_WIDTH > 0 else None)
    height = requested_height or (DEFAULT_IMAGE_MAX_HEIGHT if DEFAULT_IMAGE_MAX_HEIGHT > 0 else None)
    quality = quality or DEFAULT_IMAGE_QUALITY

    if Image is None or not _is_image_file(source_path):
        response = send_from_directory(BASE_DIR / "static", source_path.relative_to(BASE_DIR / "static"))
        return _add_cache_headers(response)

    try:
        with _safe_image_open(source_path) as probe_img:
            has_alpha = "A" in probe_img.getbands() or "transparency" in probe_img.info
    except Exception as exc:  # pragma: no cover - pillow may be unavailable or fail to read
        app.logger.warning("Failed to inspect %s (%s), serving original", source_path, exc)
        response = send_from_directory(BASE_DIR / "static", source_path.relative_to(BASE_DIR / "static"))
        return _add_cache_headers(response)

    requested_format = request.args.get("format")
    target_format = _preferred_image_format(requested_format, request.headers.get("Accept", ""))
    if not target_format:
        target_format = source_path.suffix.lstrip(".") or "png"

    if has_alpha and target_format in {"jpeg", "jpg"}:
        target_format = "png"

    optimized_path = _optimized_media_path(source_path, width, height, target_format, quality)

    if not optimized_path.exists():
        try:
            _build_optimized_image(source_path, optimized_path, width, height, target_format, quality)
        except Exception as exc:  # pragma: no cover - pillow may be unavailable
            app.logger.warning("Failed to optimize %s (%s), serving original", source_path, exc)
            response = send_from_directory(BASE_DIR / "static", source_path.relative_to(BASE_DIR / "static"))
            return _add_cache_headers(response)

    mime_type = mimetypes.types_map.get(f".{target_format.lower()}", f"image/{target_format.lower()}")
    response = send_file(optimized_path, mimetype=mime_type)
    return _add_cache_headers(response)


def _clean_content_block(block):
    if not isinstance(block, dict):
        return None

    block_type = str(block.get("type", "text")).strip().lower()
    if block_type not in {"text", "image", "gallery", "split"}:
        return None

    if block_type == "text":
        heading = str(block.get("heading", "")).strip()
        body = _normalize_multiline_text(block.get("body", ""))
        if not (heading or body):
            return None
        return {"type": "text", "heading": heading, "body": body}

    if block_type == "image":
        image = str(block.get("image", "")).strip()
        if not image:
            return None
        heading = str(block.get("heading", "")).strip()
        alt_text = str(block.get("alt", "")).strip()
        caption = str(block.get("caption", "")).strip()
        return {
            "type": "image",
            "heading": heading,
            "image": image,
            "alt": alt_text,
            "caption": caption,
        }

    if block_type == "gallery":
        heading = str(block.get("heading", "")).strip()
        images = []
        for media in block.get("images", []) or []:
            if not isinstance(media, dict):
                continue
            image = str(media.get("image", "")).strip()
            if not image:
                continue
            images.append(
                {
                    "image": image,
                    "alt": str(media.get("alt", "")).strip(),
                    "caption": str(media.get("caption", "")).strip(),
                }
            )
        if not images:
            return None
        return {"type": "gallery", "heading": heading, "images": images}

    # split block
    layout_raw = str(block.get("layout", "text-left")).strip().lower()
    allowed_layouts = {"text-left", "text-right", "image-top", "text-top"}
    layout = layout_raw if layout_raw in allowed_layouts else "text-left"
    heading = str(block.get("heading", "")).strip()
    text_heading = str(block.get("text_heading", "")).strip()
    text_body = _normalize_multiline_text(block.get("text_body", ""))
    image = str(block.get("image", "")).strip()
    image_heading = str(block.get("image_heading", "")).strip()
    image_alt = str(block.get("image_alt", "")).strip()
    image_caption = str(block.get("image_caption", "")).strip()
    if not (text_body or image):
        return None
    return {
        "type": "split",
        "layout": layout,
        "heading": heading,
        "text_heading": text_heading,
        "text_body": text_body,
        "image": image,
        "image_heading": image_heading,
        "image_alt": image_alt,
        "image_caption": image_caption,
    }


def _sanitize_blocks(blocks, fallback_text=""):
    sanitized = []
    if isinstance(blocks, list):
        for raw in blocks:
            cleaned = _clean_content_block(raw)
            if cleaned:
                sanitized.append(cleaned)
    if not sanitized:
        fallback = _normalize_multiline_text(fallback_text)
        if fallback:
            sanitized.append({"type": "text", "heading": "", "body": fallback})
    return sanitized


def _apply_block_uploads_to_blocks(blocks, upload_prefix, files):
    if not upload_prefix or not files or not isinstance(blocks, list):
        return

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue

        block_type = str(block.get("type", "")).strip().lower()
        base_prefix = f"{upload_prefix}-{index}"

        if block_type == "image":
            field_name = f"{base_prefix}-image-upload"
            upload = files.get(field_name)
            saved_path = _save_upload(upload, f"{base_prefix}-image") if upload else None
            if saved_path:
                block["image"] = saved_path
        elif block_type == "gallery":
            images = block.get("images")
            if not isinstance(images, list):
                continue
            for media_index, media in enumerate(images):
                if not isinstance(media, dict):
                    continue
                field_name = f"{base_prefix}-gallery-{media_index}-upload"
                upload = files.get(field_name)
                saved_path = (
                    _save_upload(upload, f"{base_prefix}-gallery-{media_index}")
                    if upload
                    else None
                )
                if saved_path:
                    media["image"] = saved_path
        elif block_type == "split":
            field_name = f"{base_prefix}-split-image-upload"
            upload = files.get(field_name)
            saved_path = _save_upload(upload, f"{base_prefix}-image") if upload else None
            if saved_path:
                block["image"] = saved_path


def _prepare_blocks_for_display(blocks):
    prepared = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        item = dict(block)
        if block_type == "image":
            item["image_url"] = _resolve_media_url(block.get("image"))
        elif block_type == "gallery":
            images = []
            for media in block.get("images", []) or []:
                if not isinstance(media, dict):
                    continue
                images.append(
                    {
                        "image": media.get("image", ""),
                        "alt": media.get("alt", ""),
                        "caption": media.get("caption", ""),
                        "image_url": _resolve_media_url(media.get("image")),
                    }
                )
            item["images"] = images
        elif block_type == "split":
            item["image_url"] = _resolve_media_url(block.get("image"))
        prepared.append(item)
    return prepared


def _coerce_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _prepare_admin_entries(entries, fallback_key, prefix):
    prepared = []
    used_slugs = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        summary_source = entry.get("summary") or entry.get(fallback_key) or ""
        entry["summary"] = _coerce_text(summary_source)
        entry["blocks"] = _sanitize_blocks(entry.get("blocks"), entry["summary"])
        slug_source = entry.get("slug") or entry.get("title") or entry.get("name") or f"{prefix}-{index + 1}"
        entry["slug"] = _ensure_unique_slug(slug_source, used_slugs, f"{prefix}-{index + 1}")
        prepared.append(entry)
    return prepared


def _parse_blocks_payload(raw_value, fallback_text="", upload_prefix=None, files=None):
    if raw_value:
        try:
            data = json.loads(raw_value)
        except (TypeError, ValueError):
            data = []
    else:
        data = []

    if not isinstance(data, list):
        data = []

    _apply_block_uploads_to_blocks(data, upload_prefix, files)
    return _sanitize_blocks(data, fallback_text)


def get_news_entries():
    raw = load_yaml("news.yaml") or []
    if not isinstance(raw, list):
        raw = []
    prepared = []
    used_slugs = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        title = _coerce_text(item.get("title"))
        date_value = _coerce_text(item.get("date"))
        image_value = _coerce_text(item.get("image"))
        summary = _coerce_text(item.get("summary") or item.get("body"))
        blocks = _sanitize_blocks(item.get("blocks"), summary)
        slug_source = item.get("slug") or title or f"news-{index + 1}"
        slug = _ensure_unique_slug(slug_source, used_slugs, f"news-{index + 1}")
        prepared.append(
            {
                "slug": slug,
                "title": title,
                "date": date_value,
                "image": image_value,
                "image_url": _resolve_media_url(image_value),
                "summary": summary,
                "blocks": _prepare_blocks_for_display(blocks),
            }
        )
    return prepared


def get_project_entries():
    raw = load_yaml("projects.yaml") or []
    if not isinstance(raw, list):
        raw = []
    prepared = []
    used_slugs = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        title = _coerce_text(item.get("title"))
        image_value = _coerce_text(item.get("image"))
        summary = _coerce_text(item.get("summary") or item.get("blurb"))
        url_value = _coerce_text(item.get("url"))
        blocks = _sanitize_blocks(item.get("blocks"), summary)
        slug_source = item.get("slug") or title or f"project-{index + 1}"
        slug = _ensure_unique_slug(slug_source, used_slugs, f"project-{index + 1}")
        tags = []
        for tag in item.get("tags", []) or []:
            tag_text = _coerce_text(tag)
            if tag_text:
                tags.append(tag_text)
        prepared.append(
            {
                "slug": slug,
                "title": title,
                "image": image_value,
                "image_url": _resolve_media_url(image_value),
                "summary": summary,
                "url": url_value,
                "tags": tags,
                "blocks": _prepare_blocks_for_display(blocks),
            }
        )
    return prepared


def get_resource_entries():
    raw = load_yaml("resources.yaml") or []
    if not isinstance(raw, list):
        raw = []
    prepared = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _coerce_text(item.get("title"))
        description = _coerce_text(item.get("description"))
        image_value = _coerce_text(item.get("image"))
        image_alt = _coerce_text(item.get("image_alt"))
        file_value = _coerce_text(item.get("file"))
        file_label = _coerce_text(item.get("file_label")) or "Download"
        prepared.append(
            {
                "title": title,
                "description": description,
                "image": image_value,
                "image_url": _resolve_media_url(image_value),
                "image_alt": image_alt,
                "file": file_value,
                "file_url": _resolve_media_url(file_value),
                "file_label": file_label,
            }
        )
    return prepared


def _prepare_partner_entries(entries):
    prepared = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        partner = dict(item)
        partner["logo_url"] = _resolve_media_url(partner.get("logo"))
        prepared.append(partner)
    return prepared


def _prepare_homepage_for_display(homepage_data):
    data = deepcopy(homepage_data)
    hero = data.setdefault("hero", {})
    logo_value = hero.get("logo") or DEFAULT_HOMEPAGE_SETTINGS["hero"].get("logo", "")
    hero["logo_url"] = _resolve_media_url(logo_value)
    background_value = hero.get("background_image") or ""
    hero["background_url"] = _resolve_media_url(background_value) if background_value else ""
    subtitle_value = hero.get("subtitle") or ""
    hero["subtitle_html"] = render_markdown(subtitle_value) if subtitle_value else Markup("")
    calendar = data.setdefault("calendar", {"events": []})
    events = []
    for event in calendar.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        iso_date = _normalize_date_string(event.get("date"))
        if not iso_date:
            continue
        icon_key = str(event.get("icon") or DEFAULT_CALENDAR_ICON).strip().lower()
        if icon_key not in CALENDAR_ICONS:
            icon_key = DEFAULT_CALENDAR_ICON
        icon_meta = CALENDAR_ICONS[icon_key]
        time_value = _normalize_time_string(event.get("time"))
        time_display = _format_time_display(time_value)
        recurrence_value = _normalize_recurrence(event.get("recurrence")) or "none"
        events.append(
            {
                "id": event.get("id", ""),
                "date": iso_date,
                "label": (event.get("label") or "").strip() if isinstance(event.get("label"), str) else str(event.get("label") or "").strip(),
                "icon": icon_key,
                "icon_label": icon_meta.get("label", ""),
                "icon_url": _resolve_media_url(icon_meta.get("file")),
                "url": (event.get("url") or "").strip() if isinstance(event.get("url"), str) else str(event.get("url") or "").strip(),
                "time": time_value,
                "time_display": time_display,
                "recurrence": recurrence_value,
            }
        )
    events.sort(key=lambda item: (item.get("date", ""), item.get("time") or ""))
    calendar["events"] = events
    return data


def _parse_collection(value):
    if not value:
        return []
    normalized = value.replace("\r", "\n")
    items = []
    for line in normalized.splitlines():
        segments = line.split(",") if "," in line else [line]
        for seg in segments:
            entry = seg.strip()
            if entry:
                items.append(entry)
    return items


def _parse_socials(value):
    if not value:
        return []
    links = []
    for line in value.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        label, url = parts[0], parts[1]
        icon = parts[2] if len(parts) > 2 and parts[2] else "link.svg"
        links.append({"label": label, "url": url, "icon": icon})
    return links


def _parse_gallery_lines(value):
    if not value:
        return []
    items = []
    for line in value.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        image = parts[0] if parts else ""
        alt_text = parts[1] if len(parts) > 1 else ""
        if image:
            items.append({"image": image, "alt": alt_text})
    return items


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def _parse_price_lines(value):
    if not value:
        return []
    rows = []
    for line in value.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        label = parts[0] if parts else ""
        price = parts[1] if len(parts) > 1 else ""
        note = parts[2] if len(parts) > 2 else ""
        if label or price or note:
            rows.append({"label": label, "price": price, "note": note})
    return rows


def _ensure_unique_slug(raw_value, used, fallback):
    base = _slugify(raw_value) or _slugify(fallback) or fallback or "service"
    if not base:
        base = "service"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def get_services():
    raw = load_yaml("services.yaml") or []
    if not isinstance(raw, list):
        raw = []
    services = []
    used_slugs = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        fallback = f"service-{index + 1}"
        slug_source = entry.get("slug") or name or fallback
        slug = _ensure_unique_slug(slug_source, used_slugs, fallback)

        status = entry.get("status", "open")
        if status not in {"open", "closed"}:
            status = "open"
        is_closed = status == "closed"

        badges = []
        raw_badges = entry.get("badges", []) or []
        for badge_key in raw_badges:
            key = str(badge_key).strip().lower()
            if key in SERVICE_BADGES:
                badge_info = dict(SERVICE_BADGES[key])
                badge_info["key"] = key
                badges.append(badge_info)

        contacts = []
        for contact in entry.get("contacts", []) or []:
            if not isinstance(contact, dict):
                continue
            label = contact.get("label", "").strip()
            url_value = contact.get("url", "").strip()
            icon = contact.get("icon", "").strip()
            if not (label and url_value):
                continue
            contacts.append({"label": label, "url": url_value, "icon": icon})

        image_source = (entry.get("image") or "").strip()
        image_url = _resolve_media_url(image_source)

        gallery = []
        for media in entry.get("gallery", []) or []:
            if not isinstance(media, dict):
                continue
            media_source = (media.get("image") or "").strip()
            resolved_media = _resolve_media_url(media_source)
            alt_text = (media.get("alt") or "").strip()
            if not (media_source or resolved_media):
                continue
            gallery.append(
                {
                    "image": resolved_media or media_source,
                    "image_source": media_source,
                    "image_url": resolved_media,
                    "alt": alt_text,
                }
            )

        prices = []
        for row in entry.get("prices", []) or []:
            if not isinstance(row, dict):
                continue
            label = row.get("label", "").strip()
            price_raw = row.get("price")
            note = row.get("note", "").strip()
            if not (label or price_raw or note):
                continue
            price_display = ""
            parsed_price = _parse_price_value(price_raw)
            if parsed_price is not None:
                price_display = _format_price_value(parsed_price)
            elif isinstance(price_raw, str):
                price_display = price_raw.strip()
            prices.append(
                {
                    "label": label,
                    "price": price_raw,
                    "price_display": price_display,
                    "note": note,
                }
            )

        services.append(
            {
                "name": name,
                "slug": slug,
                "description": entry.get("description", ""),
                "offered_by": entry.get("offered_by", ""),
                "image": image_url or image_source,
                "image_source": image_source,
                "image_url": image_url,
                "image_alt": entry.get("image_alt", ""),
                "status": status,
                "is_closed": is_closed,
                "contacts": contacts,
                "badges": badges,
                "gallery": gallery,
                "prices": prices,
            }
        )
    return services


def _parse_positive_int(value):
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = int(text)
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def get_artworks():
    raw = load_yaml("artworks.yaml") or []
    intro_text = ""
    entries_source = []
    if isinstance(raw, dict):
        intro_text = raw.get("intro", "")
        entries_source = raw.get("talents") or raw.get("entries") or []
    elif isinstance(raw, list):
        entries_source = raw
    else:
        entries_source = []

    talents = []
    used_slugs = set()
    for index, entry in enumerate(entries_source):
        if not isinstance(entry, dict):
            continue

        fallback = f"artwork-{index + 1}"
        slug_source = entry.get("slug") or entry.get("name") or fallback
        slug = _ensure_unique_slug(slug_source, used_slugs, fallback)
        name = entry.get("name") or slug
        tagline = entry.get("tagline") or ""
        description = entry.get("description") or ""
        interval = _parse_positive_int(entry.get("interval"))

        gallery_items = []
        for media in entry.get("gallery", []) or []:
            if not isinstance(media, dict):
                continue
            image_url = _resolve_media_url(media.get("image"))
            if not image_url:
                continue
            gallery_items.append(
                {
                    "image": image_url,
                    "alt": media.get("alt") or "",
                    "caption": media.get("caption") or "",
                    "credits": media.get("credits") or "",
                    "credits_url": media.get("credits_url") or "",
                    "watermark": _to_bool(media.get("watermark")),
                }
            )

        if not gallery_items:
            continue

        talents.append(
            {
                "slug": slug,
                "name": name,
                "tagline": tagline,
                "description": description,
                "gallery": gallery_items,
                "interval": interval,
            }
        )

    return {"intro": intro_text, "talents": talents}


@app.context_processor
def inject_globals():
    settings = get_settings()
    site_name = settings.get("site_name") or DEFAULT_SITE_SETTINGS["site_name"]
    site_tagline = settings.get("site_tagline") or DEFAULT_SITE_SETTINGS["site_tagline"]
    site_subtitle = settings.get("site_subtitle") or DEFAULT_SITE_SETTINGS["site_subtitle"]
    meta_defaults = _compute_meta_defaults(settings)
    default_meta_description = meta_defaults["description"]
    default_social_image = meta_defaults["image"]
    organization_schema = meta_defaults["organization"]
    footer_settings = settings.get("footer", {})
    footer_about_text = footer_settings.get("about_text")
    if footer_about_text is None:
        footer_about_text = DEFAULT_SITE_SETTINGS["footer"].get("about_text", "")
    current_year = datetime.utcnow().year
    nav_items = [
        ("Home", "index"),
        ("Über uns", "about"),
        ("Talente", "talents"),
        ("Projekte", "projects"),
        ("Services", "services"),
        ("Partner", "partners"),
        ("News", "news"),
    ]
    if settings.get("shop_enabled", True):
        nav_items.append(("Shop", "shop"))
    artworks_enabled = settings.get("artworks_panel_enabled", True)
    gallery_login_available = (
        artworks_enabled or session.get("is_admin") or session.get("artworks_manager")
    )
    if artworks_enabled:
        nav_items.append(("Galerie", "artworks"))
    nav_items.append(("Kontakt", "contact"))
    starfield_settings = settings.get("starfield")
    if not isinstance(starfield_settings, dict):
        starfield_settings = DEFAULT_SITE_SETTINGS.get("starfield", {})
    return {
        "SITE_NAME": site_name,
        "SITE_TAGLINE": site_tagline,
        "SITE_SUBTITLE": site_subtitle,
        "DEFAULT_META_DESCRIPTION": default_meta_description,
        "DEFAULT_SOCIAL_IMAGE": default_social_image,
        "ORGANIZATION_SCHEMA": organization_schema,
        "NAV_ITEMS": nav_items,
        "IS_ADMIN": session.get("is_admin"),
        "IS_ARTWORKS_MANAGER": session.get("artworks_manager"),
        "AUTH_USERNAME": session.get("auth_username"),
        "AUTH_SLUG": session.get("auth_slug"),
        "AUTH_USER_ADMIN": session.get("auth_user_admin"),
        "MAINTENANCE_MODE": settings.get("maintenance_mode", False),
        "SHOW_GALLERY_LOGIN": gallery_login_available,
        "FOOTER": {
            "about_heading": footer_settings.get("about_heading") or "About",
            "about_text": footer_about_text,
            "social_heading": footer_settings.get("social_heading") or "Socials",
            "socials": footer_settings.get("socials", []),
            "legal_heading": footer_settings.get("legal_heading") or "Rechtliches",
            "legal_links": footer_settings.get("legal_links", []),
            "fine_print": (footer_settings.get("fine_print") or "© {site_name} {year} • Made with stardust.").format(
                site_name=site_name,
                year=current_year,
            ),
        },
        "STARFIELD": starfield_settings,
    }


@app.before_request
def enforce_maintenance_mode():
    _start_live_status_worker()
    settings = get_settings()
    if not settings.get("maintenance_mode"):
        return

    if session.get("is_admin") or session.get("maintenance_access"):
        return

    endpoint = request.endpoint or ""
    if endpoint.startswith("static"):
        return

    allowed = {
        "maintenance_login",
        "maintenance_logout",
        "admin_login",
        "gallery_login",
        "legacy_artworks_login",
    }
    if endpoint in allowed or endpoint.startswith("admin_"):
        return

    return redirect(url_for("maintenance_login", next=request.path))


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
    )
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Permissions-Policy", "interest-cohort=()")
    if "Cache-Control" not in response.headers:
        if response.mimetype in {"text/html", "application/xhtml+xml"}:
            response.headers.setdefault("Cache-Control", "public, max-age=300")
        elif response.mimetype and (
            "javascript" in response.mimetype or response.mimetype in {"text/css"}
        ):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    return response


@app.after_request
def apply_brotli_compression(response):
    if brotli is None:
        return response

    if request.method == "HEAD" or response.direct_passthrough:
        return response

    if "br" not in (request.accept_encodings or {}):
        return response

    if response.status_code in {204, 304}:
        return response

    if "Content-Encoding" in response.headers:
        return response

    mimetype = (response.mimetype or "").split(";")[0]
    if mimetype and not (
        mimetype in COMPRESSIBLE_MIMETYPES or mimetype.startswith("text/")
    ):
        return response

    raw_data = response.get_data()
    if raw_data is None or len(raw_data) < app.config.get("BROTLI_MIN_SIZE", 0):
        return response

    try:
        compressed = brotli.compress(
            raw_data, quality=app.config.get("BROTLI_QUALITY", DEFAULT_BROTLI_QUALITY)
        )
    except Exception:
        return response

    if len(compressed) >= len(raw_data):
        return response

    response.set_data(compressed)
    response.headers["Content-Encoding"] = "br"
    response.headers["Content-Length"] = str(len(compressed))
    response.headers["Vary"] = _append_vary(response.headers.get("Vary"), "Accept-Encoding")
    return response



def _extract_talent_members(raw_data):
    """Return a flat list of members from old/new talents.yaml structures."""

    if isinstance(raw_data, dict):
        if isinstance(raw_data.get("members"), list):
            return [item for item in raw_data.get("members", []) if isinstance(item, dict)]
        source = raw_data.get("teams", [])
    else:
        source = raw_data

    members = []
    if isinstance(source, list):
        for item in source:
            if isinstance(item, dict) and isinstance(item.get("members"), list):
                members.extend(member for member in item.get("members", []) if isinstance(member, dict))
            elif isinstance(item, dict):
                members.append(item)
    return members


@lru_cache(maxsize=1)
def get_talent_data():
    raw = load_yaml("talents.yaml")
    members = _extract_talent_members(raw)
    normalized_members = []
    member_index = {}

    for member in members:
        member_copy = deepcopy(member)
        slug = (member_copy.get("slug") or "").strip()
        if not slug:
            continue

        default_asset = f"images/talents/members/{slug}.svg"
        profile_image = member_copy.get("profile_image") or member_copy.get("avatar") or default_asset
        member_copy["profile_image"] = profile_image or default_asset

        fullbody_image = member_copy.get("fullbody_image") or member_copy.get("fullbody")
        if not fullbody_image:
            fullbody_image = member_copy["profile_image"]
        member_copy["fullbody_image"] = fullbody_image

        member_copy.setdefault("colors", ["#f3c92d", "#57d6ff"])
        member_copy.pop("avatar", None)
        member_copy.pop("fullbody", None)

        entry = deepcopy(member_copy)
        entry["team"] = {
            "id": "astralia",
            "name": "Astralia",
            "slogan": "",
            "logo": "images/logo.png",
            "colors": ["#f3c92d", "#57d6ff"],
        }
        normalized_members.append(member_copy)
        member_index[slug] = entry

    pseudo_team = {
        "id": "astralia",
        "name": "Astralia",
        "slogan": "",
        "logo": "images/logo.png",
        "colors": ["#f3c92d", "#57d6ff"],
        "members": normalized_members,
    }
    return [pseudo_team], member_index


def _user_can_edit_slug(slug: str) -> bool:
    if session.get("is_admin"):
        return True
    auth_slug = (session.get("auth_slug") or "").strip().casefold()
    target_slug = (slug or "").strip().casefold()
    return auth_slug and auth_slug == target_slug


def _team_auth_required() -> bool:
    return session.get("auth_source") == "db" and bool(session.get("auth_slug"))


def _upsert_talent_profile(slug: str, payload: dict[str, object]) -> bool:
    data = load_yaml("talents.yaml") or {}
    if not isinstance(data, dict):
        data = {}

    teams = data.get("teams")
    if not isinstance(teams, list):
        teams = []
        data["teams"] = teams

    if not teams:
        teams.append(
            {
                "id": "astralia",
                "name": "Astralia",
                "slogan": "",
                "logo": "images/logo.png",
                "colors": ["#f3c92d", "#57d6ff"],
                "members": [],
            }
        )

    target_slug = (slug or "").strip()
    updated = False
    for team in teams:
        if not isinstance(team, dict):
            continue
        members = team.get("members")
        if not isinstance(members, list):
            members = []
            team["members"] = members
        for member in members:
            if not isinstance(member, dict):
                continue
            if (member.get("slug") or "").strip() != target_slug:
                continue
            member.update(payload)
            updated = True
            break
        if updated:
            break

    if not updated:
        first_team = teams[0]
        members = first_team.get("members")
        if not isinstance(members, list):
            members = []
            first_team["members"] = members
        new_profile = {
            "slug": target_slug,
            "name": payload.get("name") or target_slug.replace("-", " ").title(),
            "birthday": "",
            "species": "",
            "height": "",
            "profile_image": f"images/talents/members/{target_slug}.svg",
            "fullbody_image": f"images/talents/members/{target_slug}.svg",
            "favorites": [],
            "specialties": "",
            "motto": "",
            "socials": [],
            "introduction": "",
        }
        new_profile.update(payload)
        members.append(new_profile)

    save_yaml("talents.yaml", data)
    get_talent_data.cache_clear()
    return True


@app.route("/user/login", methods=["GET", "POST"])
@app.route("/admin/team-login", methods=["GET", "POST"], endpoint="team_login")
@app.route("/verwaltung/team-login", methods=["GET", "POST"], endpoint="team_login_verwaltung")
def user_login():
    if session.get("auth_username"):
        own_slug = session.get("auth_slug")
        if own_slug:
            return redirect(url_for("team_profile_verwaltung"))
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        user = _get_user_by_username(username)
        if user and check_password_hash(user.get("password_hash") or "", password):
            session["auth_source"] = "db"
            session["auth_username"] = user.get("username")
            session["auth_slug"] = user.get("username")
            session["auth_user_admin"] = bool(user.get("is_admin"))
            flash("Erfolgreich angemeldet.", "success")
            next_url = request.args.get("next") or url_for("team_profile_verwaltung")
            return redirect(next_url)
        flash("Ungültige Zugangsdaten.", "error")

    return render_template(
        "admin/login.html",
        login_title="Talent Login",
        login_intro="Melde dich mit deinem Talent-Konto an, um dein Profil zu bearbeiten.",
        login_button_label="Einloggen",
    )


@app.route("/user/logout")
@app.route("/admin/team-logout", endpoint="team_logout")
@app.route("/verwaltung/team-logout", endpoint="team_logout_verwaltung")
def user_logout():
    for key in ("auth_source", "auth_username", "auth_slug", "auth_user_admin"):
        session.pop(key, None)
    return redirect(url_for("index"))


@app.route("/account/password", methods=["GET", "POST"])
@app.route("/admin/team-password", methods=["GET", "POST"], endpoint="team_password")
@app.route("/verwaltung/team-password", methods=["GET", "POST"], endpoint="team_password_verwaltung")
def account_password():
    if session.get("auth_source") != "db" or not session.get("auth_username"):
        return redirect(url_for("team_login_verwaltung", next=request.path))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        user = _get_user_by_username(session.get("auth_username"))
        if not user or not check_password_hash(user.get("password_hash") or "", current_password):
            flash("Aktuelles Passwort ist ungültig.", "error")
        elif new_password != confirm_password:
            flash("Die neuen Passwörter stimmen nicht überein.", "error")
        else:
            ok, message = _update_user_password(user.get("username"), new_password)
            flash(message, "success" if ok else "error")
            if ok:
                return redirect(url_for("team_profile_verwaltung"))

    return render_template("account_password.html")


def _profile_images_from_request(slug: str, fallback_talent: dict[str, object] | None = None) -> dict[str, str]:
    fallback_talent = fallback_talent or {}
    profile_image = (request.form.get("profile_image") or "").strip()
    fullbody_image = (request.form.get("fullbody_image") or "").strip()

    profile_upload = request.files.get("profile_upload")
    if profile_upload and profile_upload.filename:
        uploaded = _save_upload(profile_upload, f"{slug}-profile")
        if uploaded:
            profile_image = uploaded

    fullbody_upload = request.files.get("fullbody_upload")
    if fullbody_upload and fullbody_upload.filename:
        uploaded = _save_upload(fullbody_upload, f"{slug}-fullbody")
        if uploaded:
            fullbody_image = uploaded

    if not profile_image:
        profile_image = (fallback_talent.get("profile_image") or "").strip() if isinstance(fallback_talent.get("profile_image"), str) else ""
    if not fullbody_image:
        fullbody_image = (fallback_talent.get("fullbody_image") or "").strip() if isinstance(fallback_talent.get("fullbody_image"), str) else ""

    return {
        "profile_image": profile_image,
        "fullbody_image": fullbody_image,
    }


@app.route("/admin/team-profile", methods=["GET", "POST"], endpoint="team_profile")
@app.route("/verwaltung/team-profile", methods=["GET", "POST"], endpoint="team_profile_verwaltung")
def team_profile():
    if not _team_auth_required():
        return redirect(url_for("team_login_verwaltung", next=request.path))

    slug = (session.get("auth_slug") or "").strip()
    _, member_index = get_talent_data()
    talent = member_index.get(slug)
    if not talent:
        talent = {
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "birthday": "",
            "species": "",
            "height": "",
            "specialties": "",
            "motto": "",
            "introduction": "",
            "favorites": [],
            "socials": [],
        }

    if request.method == "POST":
        image_payload = _profile_images_from_request(slug, talent)
        payload = {
            "name": (request.form.get("name") or "").strip() or talent.get("name", ""),
            "birthday": (request.form.get("birthday") or "").strip(),
            "species": (request.form.get("species") or "").strip(),
            "height": (request.form.get("height") or "").strip(),
            "specialties": (request.form.get("specialties") or "").strip(),
            "motto": (request.form.get("motto") or "").strip(),
            "introduction": (request.form.get("introduction") or "").strip(),
            "favorites": _parse_collection(request.form.get("favorites", "")),
            "socials": _parse_socials(request.form.get("socials", "")),
            "profile_image": image_payload.get("profile_image", ""),
            "fullbody_image": image_payload.get("fullbody_image", ""),
        }
        if _upsert_talent_profile(slug, payload):
            flash("Profil gespeichert.", "success")
            return redirect(url_for("team_profile_verwaltung"))
        flash("Profil konnte nicht gespeichert werden.", "error")

    return render_template("talent_edit.html", talent=talent, managed_in_admin=True)


@app.route("/talents/<slug>/edit", methods=["GET", "POST"], strict_slashes=False)
def talent_edit(slug):
    _, member_index = get_talent_data()
    talent = member_index.get(slug)
    if not talent:
        abort(404)
    if not _user_can_edit_slug(slug):
        return redirect(url_for("team_login_verwaltung", next=request.path))

    if request.method == "POST":
        image_payload = _profile_images_from_request(slug, talent)
        payload = {
            "name": (request.form.get("name") or "").strip() or talent.get("name", ""),
            "birthday": (request.form.get("birthday") or "").strip(),
            "species": (request.form.get("species") or "").strip(),
            "height": (request.form.get("height") or "").strip(),
            "specialties": (request.form.get("specialties") or "").strip(),
            "motto": (request.form.get("motto") or "").strip(),
            "introduction": (request.form.get("introduction") or "").strip(),
            "favorites": _parse_collection(request.form.get("favorites", "")),
            "socials": _parse_socials(request.form.get("socials", "")),
            "profile_image": image_payload.get("profile_image", ""),
            "fullbody_image": image_payload.get("fullbody_image", ""),
        }
        if _upsert_talent_profile(slug, payload):
            flash("Profil gespeichert.", "success")
            return redirect(url_for("talent_detail", slug=slug))
        flash("Profil konnte nicht gespeichert werden.", "error")

    return render_template("talent_edit.html", talent=talent)


@app.route("/admin/login", methods=["GET", "POST"])
@app.route("/verwaltung/login", methods=["GET", "POST"], endpoint="admin_login_verwaltung")
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            session["auth_source"] = "env"
            session["auth_username"] = ADMIN_USERNAME
            flash("Erfolgreich angemeldet.", "success")
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)

        user = _get_user_by_username(username)
        if user and check_password_hash(user.get("password_hash") or "", password) and user.get("is_admin"):
            session["is_admin"] = True
            session["auth_source"] = "db"
            session["auth_username"] = user.get("username")
            session["auth_slug"] = user.get("username")
            session["auth_user_admin"] = True
            flash("Erfolgreich angemeldet.", "success")
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)

        error = "Ungültige Zugangsdaten."
        flash(error, "error")
    return render_template("admin/login.html")


@app.route("/gallery-login", methods=["GET", "POST"])
@app.route(
    "/admin/artworks-login",
    methods=["GET", "POST"],
    endpoint="legacy_artworks_login",
)
def gallery_login():
    settings = get_settings()
    artworks_enabled = settings.get("artworks_panel_enabled", True)

    if not artworks_enabled and not session.get("is_admin"):
        abort(404)

    if session.get("is_admin") or session.get("artworks_manager"):
        next_url = request.args.get("next") or url_for("admin_dashboard", tab="artworks")
        return redirect(next_url)

    next_url = request.args.get("next")
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")

        def _match(target_user, target_password):
            if not target_user or not target_password:
                return False
            return username.casefold() == target_user.casefold() and password == target_password

        if _match(ARTWORKS_USERNAME, ARTWORKS_PASSWORD):
            session["artworks_manager"] = True
            flash("Willkommen im Galerie-Bereich.", "success")
            target = next_url or url_for("admin_dashboard", tab="artworks")
            return redirect(target)

        if _match(ADMIN_USERNAME, ADMIN_PASSWORD):
            session["is_admin"] = True
            flash("Erfolgreich angemeldet.", "success")
            target = next_url or url_for("admin_dashboard", tab="artworks")
            return redirect(target)

        flash("Ungültige Zugangsdaten.", "error")

    return render_template(
        "admin/login.html",
        login_title="Galerie-Login",
        login_intro="Melde dich mit dem Talent-Zugang an, um die Galerien zu verwalten.",
        login_button_label="Einloggen",
    )


@app.route("/admin/logout")
def admin_logout():
    was_admin = session.pop("is_admin", None)
    was_artworks_manager = session.pop("artworks_manager", None)
    for key in ("auth_source", "auth_username", "auth_slug", "auth_user_admin"):
        session.pop(key, None)
    flash("Abgemeldet.", "info")
    if was_artworks_manager and not was_admin:
        return redirect(url_for("gallery_login"))
    return redirect(url_for("index"))


@app.route("/maintenance-login", methods=["GET", "POST"])
def maintenance_login():
    settings = get_settings()
    if not settings.get("maintenance_mode") and not session.get("maintenance_access") and not session.get("is_admin"):
        flash("Die Seite ist derzeit live – kein Wartungslogin erforderlich.", "info")
        return redirect(url_for("index"))

    if session.get("maintenance_access") or session.get("is_admin"):
        next_url = request.args.get("next") or url_for("index")
        return redirect(next_url)

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == MAINTENANCE_USERNAME and password == MAINTENANCE_PASSWORD:
            session["maintenance_access"] = True
            flash("Willkommen zurück.", "success")
            next_url = request.form.get("next") or request.args.get("next") or url_for("index")
            return redirect(next_url)
        error = "Ungültige Zugangsdaten."
        flash(error, "error")

    return render_template("maintenance_login.html", error=error)


@app.route("/maintenance-logout")
def maintenance_logout():
    session.pop("maintenance_access", None)
    flash("Abgemeldet.", "info")
    settings = get_settings()
    if settings.get("maintenance_mode"):
        redirect_target = request.args.get("next") or url_for("maintenance_login")
    else:
        redirect_target = request.args.get("next") or url_for("index")
    return redirect(redirect_target)


@app.route("/admin", methods=["GET", "POST"])
@app.route("/verwaltung", methods=["GET", "POST"], endpoint="admin_dashboard_verwaltung")
@admin_required
def admin_dashboard():
    requested_tab = request.args.get("tab", "home")
    is_admin = session.get("is_admin")
    is_artworks_manager = session.get("artworks_manager")
    settings_data = get_settings()
    artworks_enabled = settings_data.get("artworks_panel_enabled", True)
    privacy_markdown = load_markdown(PRIVACY_FILE)
    users_data = _list_users() if is_admin else []

    if not is_admin and requested_tab == "users":
        return redirect(url_for("admin_dashboard", tab="home"))

    if is_artworks_manager and not is_admin:
        if not artworks_enabled and request.method == "GET":
            flash(
                "Die Galerie ist derzeit deaktiviert. Änderungen hier bleiben privat, bis sie wieder aktiviert wird.",
                "info",
            )
        if requested_tab != "artworks":
            return redirect(url_for("admin_dashboard", tab="artworks"))

    tab = requested_tab
    news_raw = load_yaml("news.yaml") or []
    if not isinstance(news_raw, list):
        news_raw = []
    news_data = _prepare_admin_entries(news_raw, "body", "news")

    projects_raw = load_yaml("projects.yaml") or []
    if not isinstance(projects_raw, list):
        projects_raw = []
    projects_data = _prepare_admin_entries(projects_raw, "blurb", "project")
    partners_data = load_yaml("partners.yaml") or []
    resources_source = load_yaml("resources.yaml") or []
    services_source = load_yaml("services.yaml") or []
    if not isinstance(partners_data, list):
        partners_data = []
    if not isinstance(resources_source, list):
        resources_source = []
    if not isinstance(services_source, list):
        services_source = []
    resources_data = []
    for entry in resources_source:
        if not isinstance(entry, dict):
            continue
        resource_entry = dict(entry)
        image_value = (resource_entry.get("image") or "").strip()
        file_value = (resource_entry.get("file") or "").strip()
        resource_entry["image"] = image_value
        resource_entry["file"] = file_value
        resource_entry["image_url"] = _resolve_media_url(image_value)
        resource_entry["file_url"] = _resolve_media_url(file_value)
        resources_data.append(resource_entry)
    services_data = []
    for entry in services_source:
        if not isinstance(entry, dict):
            continue
        image_value = (entry.get("image") or "").strip()
        service_entry = dict(entry)
        service_entry["image"] = image_value
        service_entry["image_url"] = _resolve_media_url(image_value)
        gallery_items = []
        gallery_source = entry.get("gallery")
        if isinstance(gallery_source, list):
            for media in gallery_source:
                if not isinstance(media, dict):
                    continue
                media_image = (media.get("image") or "").strip()
                gallery_items.append(
                    {
                        "image": media_image,
                        "image_url": _resolve_media_url(media_image),
                        "alt": (media.get("alt") or "").strip(),
                    }
                )
        service_entry["gallery"] = gallery_items
        services_data.append(service_entry)
    about_data = load_yaml("about.yaml") or {}
    if not isinstance(about_data, dict):
        about_data = {}
    talents_raw = load_yaml("talents.yaml") or {}
    talents_data = _extract_talent_members(talents_raw)
    shop_items = get_shop_items()
    homepage_data = get_homepage_settings()
    homepage_display = _prepare_homepage_for_display(homepage_data)

    artworks_source = load_yaml("artworks.yaml") or {}
    if not isinstance(artworks_source, dict):
        artworks_source = {}
    intro_raw = artworks_source.get("intro")
    intro_text = intro_raw.strip() if isinstance(intro_raw, str) else ""
    talents_source = artworks_source.get("talents")
    if not isinstance(talents_source, list):
        talents_source = []
    artworks_admin = {"intro": intro_text, "talents": []}
    for entry in talents_source:
        if not isinstance(entry, dict):
            continue
        slug = (entry.get("slug") or "").strip()
        name = (entry.get("name") or "").strip()
        tagline = (entry.get("tagline") or "").strip()
        description_value = entry.get("description")
        description = description_value.strip() if isinstance(description_value, str) else ""
        interval_value = entry.get("interval")
        try:
            interval = int(interval_value)
        except (TypeError, ValueError):
            interval = None
        gallery_items = []
        gallery_source = entry.get("gallery")
        if isinstance(gallery_source, list):
            for media in gallery_source:
                if not isinstance(media, dict):
                    continue
                image_value = (media.get("image") or "").strip()
                gallery_items.append(
                    {
                        "image": image_value,
                        "image_url": _resolve_media_url(image_value),
                        "alt": (media.get("alt") or "").strip(),
                        "caption": (media.get("caption") or "").strip(),
                        "credits": (media.get("credits") or "").strip(),
                        "credits_url": (media.get("credits_url") or "").strip(),
                        "watermark": bool(media.get("watermark")),
                    }
                )
        artworks_admin["talents"].append(
            {
                "slug": slug,
                "name": name,
                "tagline": tagline,
                "description": description,
                "interval": interval if isinstance(interval, int) and interval > 0 else "",
                "gallery": gallery_items,
            }
        )

    if request.method == "POST":
        form_name = request.form.get("form-name") or tab
        if is_artworks_manager and not is_admin and form_name != "artworks":
            flash("Du hast keine Berechtigung für diesen Bereich.", "error")
            return redirect(url_for("admin_dashboard", tab="artworks"))
        if form_name == "homepage":
            hero_subtitle = request.form.get("home-hero-subtitle", "").strip()
            hero_kicker = request.form.get("home-hero-kicker", "").strip()
            hero_logo = request.form.get("home-hero-logo", "").strip()
            hero_background = request.form.get("home-hero-background", "").strip()
            primary_label = request.form.get("home-hero-primary-label", "").strip()
            primary_url = request.form.get("home-hero-primary-url", "").strip()
            secondary_label = request.form.get("home-hero-secondary-label", "").strip()
            secondary_url = request.form.get("home-hero-secondary-url", "").strip()
            tertiary_label = request.form.get("home-hero-tertiary-label", "").strip()
            tertiary_url = request.form.get("home-hero-tertiary-url", "").strip()

            logo_upload = request.files.get("home-hero-logo-upload")
            saved_logo = _save_upload(logo_upload, "home-hero-logo") if logo_upload else None
            if saved_logo:
                hero_logo = saved_logo

            background_upload = request.files.get("home-hero-background-upload")
            saved_background = (
                _save_upload(background_upload, "home-hero-background") if background_upload else None
            )
            if saved_background:
                hero_background = saved_background

            sections_payload = []
            section_count = len(homepage_data.get("sections", []))
            for idx in range(section_count):
                section_id = request.form.get(f"home-section-{idx}-id", "").strip()
                heading = request.form.get(f"home-section-{idx}-heading", "").strip()
                cta_label = request.form.get(f"home-section-{idx}-cta-label", "").strip()
                cta_url = request.form.get(f"home-section-{idx}-cta-url", "").strip()
                enabled = request.form.get(f"home-section-{idx}-enabled") == "on"
                order_raw = request.form.get(f"home-section-{idx}-order", "").strip()
                try:
                    order_value = int(order_raw)
                except (TypeError, ValueError):
                    order_value = idx + 1
                sections_payload.append(
                    {
                        "id": section_id or f"section-{idx + 1}",
                        "heading": heading,
                        "cta_label": cta_label,
                        "cta_url": cta_url,
                        "enabled": enabled,
                        "order": order_value,
                    }
                )

            sections_payload.sort(key=lambda item: item.get("order", 0))

            calendar_events_payload = []
            existing_events = homepage_data.get("calendar", {}).get("events", [])
            for idx in range(len(existing_events)):
                if request.form.get(f"home-calendar-{idx}-delete") == "on":
                    continue
                event_id = request.form.get(f"home-calendar-{idx}-id", "").strip()
                date_value = request.form.get(f"home-calendar-{idx}-date", "").strip()
                label_value = request.form.get(f"home-calendar-{idx}-label", "").strip()
                icon_value = request.form.get(f"home-calendar-{idx}-icon", "").strip().lower()
                url_value = request.form.get(f"home-calendar-{idx}-url", "").strip()
                time_value = _normalize_time_string(request.form.get(f"home-calendar-{idx}-time"))
                recurrence_value = _normalize_recurrence(
                    request.form.get(f"home-calendar-{idx}-recurrence")
                )
                iso_date = _normalize_date_string(date_value)
                if not iso_date:
                    continue
                if icon_value not in CALENDAR_ICONS:
                    icon_value = DEFAULT_CALENDAR_ICON
                event_id = event_id or f"event-{len(calendar_events_payload) + 1}"
                event_payload = {
                    "id": event_id,
                    "date": iso_date,
                    "label": label_value,
                    "icon": icon_value,
                    "url": url_value,
                }
                if time_value:
                    event_payload["time"] = time_value
                if recurrence_value:
                    event_payload["recurrence"] = recurrence_value
                calendar_events_payload.append(event_payload)

            new_date = request.form.get("home-calendar-new-date", "").strip()
            new_label = request.form.get("home-calendar-new-label", "").strip()
            new_icon = request.form.get("home-calendar-new-icon", DEFAULT_CALENDAR_ICON).strip().lower()
            new_url = request.form.get("home-calendar-new-url", "").strip()
            new_time = _normalize_time_string(request.form.get("home-calendar-new-time"))
            new_recurrence = _normalize_recurrence(
                request.form.get("home-calendar-new-recurrence")
            )
            new_iso_date = _normalize_date_string(new_date)
            if new_iso_date:
                if new_icon not in CALENDAR_ICONS:
                    new_icon = DEFAULT_CALENDAR_ICON
                event_payload = {
                    "id": f"event-{len(calendar_events_payload) + 1}",
                    "date": new_iso_date,
                    "label": new_label,
                    "icon": new_icon,
                    "url": new_url,
                }
                if new_time:
                    event_payload["time"] = new_time
                if new_recurrence:
                    event_payload["recurrence"] = new_recurrence
                calendar_events_payload.append(event_payload)

            calendar_events_payload.sort(
                key=lambda item: (item.get("date", ""), item.get("time") or "")
            )

            homepage_payload = {
                "hero": {
                    "logo": hero_logo,
                    "subtitle": hero_subtitle,
                    "kicker": hero_kicker,
                    "background_image": hero_background,
                    "primary_button": {
                        "label": primary_label,
                        "url": primary_url,
                    },
                    "secondary_button": {
                        "label": secondary_label,
                        "url": secondary_url,
                    },
                    "tertiary_button": {
                        "label": tertiary_label,
                        "url": tertiary_url,
                    },
                },
                "sections": sections_payload,
                "calendar": {"events": calendar_events_payload},
            }
            save_homepage_settings(homepage_payload)
            flash("Startseite aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="home"))

        if form_name == "news":
            entries = []
            used_slugs = set()
            for idx in range(len(news_data)):
                if request.form.get(f"news-{idx}-delete") == "on":
                    continue

                title = request.form.get(f"news-{idx}-title", "") or ""
                date = request.form.get(f"news-{idx}-date", "") or ""
                image = request.form.get(f"news-{idx}-image", "") or ""
                summary_value = request.form.get(f"news-{idx}-summary")
                if summary_value is None:
                    summary_value = request.form.get(f"news-{idx}-body", "")
                summary = (summary_value or "").strip()
                slug_input = (request.form.get(f"news-{idx}-slug") or "").strip()

                upload = request.files.get(f"news-{idx}-image-upload")
                saved_path = _save_upload(upload, f"news-{idx}") if upload else None
                if saved_path:
                    image = saved_path

                blocks_raw = (request.form.get(f"news-{idx}-blocks") or "").strip()
                blocks = _parse_blocks_payload(
                    blocks_raw,
                    summary,
                    f"news-{idx}-block",
                    request.files,
                )

                if any([title.strip(), date.strip(), image.strip(), summary, blocks]):
                    slug = _ensure_unique_slug(slug_input or title or f"news-{idx + 1}", used_slugs, f"news-{idx + 1}")
                    entry = {
                        "slug": slug,
                        "title": title.strip(),
                        "date": date.strip(),
                        "image": image.strip(),
                        "summary": summary,
                        "body": summary,
                    }
                    if blocks:
                        entry["blocks"] = blocks
                    entries.append(entry)

            new_title = (request.form.get("news-new-title") or "").strip()
            new_date = (request.form.get("news-new-date") or "").strip()
            new_image = (request.form.get("news-new-image") or "").strip()
            new_summary_val = request.form.get("news-new-summary")
            if new_summary_val is None:
                new_summary_val = request.form.get("news-new-body", "")
            new_summary = (new_summary_val or "").strip()
            new_slug_input = (request.form.get("news-new-slug") or "").strip()
            new_upload = request.files.get("news-new-image-upload")
            new_saved = _save_upload(new_upload, "news-new") if new_upload else None
            if new_saved:
                new_image = new_saved
            new_blocks_raw = (request.form.get("news-new-blocks") or "").strip()
            new_blocks = _parse_blocks_payload(
                new_blocks_raw,
                new_summary,
                "news-new-block",
                request.files,
            )

            if any([new_title, new_date, new_image, new_summary, new_blocks]):
                slug = _ensure_unique_slug(new_slug_input or new_title or f"news-{len(entries) + 1}", used_slugs, f"news-{len(entries) + 1}")
                new_entry = {
                    "slug": slug,
                    "title": new_title,
                    "date": new_date,
                    "image": new_image,
                    "summary": new_summary,
                    "body": new_summary,
                }
                if new_blocks:
                    new_entry["blocks"] = new_blocks
                entries.append(new_entry)

            save_yaml("news.yaml", entries)
            flash("News aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="news"))

        if form_name == "projects":
            entries = []
            used_slugs = set()
            for idx in range(len(projects_data)):
                if request.form.get(f"projects-{idx}-delete") == "on":
                    continue

                title = request.form.get(f"projects-{idx}-title", "") or ""
                summary_value = request.form.get(f"projects-{idx}-summary")
                if summary_value is None:
                    summary_value = request.form.get(f"projects-{idx}-blurb", "")
                summary = (summary_value or "").strip()
                image = request.form.get(f"projects-{idx}-image", "") or ""
                url_value = request.form.get(f"projects-{idx}-url", "") or ""
                tags_text = request.form.get(f"projects-{idx}-tags", "")
                slug_input = (request.form.get(f"projects-{idx}-slug") or "").strip()

                upload = request.files.get(f"projects-{idx}-image-upload")
                saved_path = _save_upload(upload, f"project-{idx}") if upload else None
                if saved_path:
                    image = saved_path

                blocks_raw = (request.form.get(f"projects-{idx}-blocks") or "").strip()
                blocks = _parse_blocks_payload(
                    blocks_raw,
                    summary,
                    f"projects-{idx}-block",
                    request.files,
                )
                tags_list = _parse_collection(tags_text)

                if any([title.strip(), summary, image.strip(), url_value.strip(), tags_list, blocks]):
                    slug = _ensure_unique_slug(slug_input or title or f"project-{idx + 1}", used_slugs, f"project-{idx + 1}")
                    entry = {
                        "slug": slug,
                        "title": title.strip(),
                        "summary": summary,
                        "blurb": summary,
                        "image": image.strip(),
                        "url": url_value.strip(),
                        "tags": tags_list,
                    }
                    if blocks:
                        entry["blocks"] = blocks
                    entries.append(entry)

            new_title = (request.form.get("projects-new-title") or "").strip()
            new_summary_val = request.form.get("projects-new-summary")
            if new_summary_val is None:
                new_summary_val = request.form.get("projects-new-blurb", "")
            new_summary = (new_summary_val or "").strip()
            new_image = (request.form.get("projects-new-image") or "").strip()
            new_url = (request.form.get("projects-new-url") or "").strip()
            new_tags = request.form.get("projects-new-tags", "")
            new_slug_input = (request.form.get("projects-new-slug") or "").strip()
            new_upload = request.files.get("projects-new-image-upload")
            new_saved = _save_upload(new_upload, "project-new") if new_upload else None
            if new_saved:
                new_image = new_saved
            new_blocks_raw = (request.form.get("projects-new-blocks") or "").strip()
            new_blocks = _parse_blocks_payload(
                new_blocks_raw,
                new_summary,
                "projects-new-block",
                request.files,
            )
            new_tags_list = _parse_collection(new_tags)

            if any([new_title, new_summary, new_image, new_url, new_tags_list, new_blocks]):
                slug = _ensure_unique_slug(new_slug_input or new_title or f"project-{len(entries) + 1}", used_slugs, f"project-{len(entries) + 1}")
                new_entry = {
                    "slug": slug,
                    "title": new_title,
                    "summary": new_summary,
                    "blurb": new_summary,
                    "image": new_image,
                    "url": new_url,
                    "tags": new_tags_list,
                }
                if new_blocks:
                    new_entry["blocks"] = new_blocks
                entries.append(new_entry)

            save_yaml("projects.yaml", entries)
            flash("Projekte aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="projects"))

        if form_name == "partners":
            entries = []
            for idx in range(len(partners_data)):
                if request.form.get(f"partners-{idx}-delete") == "on":
                    continue

                name = request.form.get(f"partners-{idx}-name", "").strip()
                url_value = request.form.get(f"partners-{idx}-url", "").strip()
                logo = request.form.get(f"partners-{idx}-logo", "").strip()

                upload = request.files.get(f"partners-{idx}-logo-upload")
                saved_path = _save_upload(upload, f"partner-{idx}") if upload else None
                if saved_path:
                    logo = saved_path

                if any([name, url_value, logo]):
                    entries.append(
                        {
                            "name": name,
                            "url": url_value,
                            "logo": logo,
                        }
                    )

            new_name = request.form.get("partners-new-name", "").strip()
            new_url = request.form.get("partners-new-url", "").strip()
            new_logo = request.form.get("partners-new-logo", "").strip()
            new_upload = request.files.get("partners-new-logo-upload")
            new_saved = _save_upload(new_upload, "partner-new") if new_upload else None
            if new_saved:
                new_logo = new_saved

            if any([new_name, new_url, new_logo]):
                entries.append({"name": new_name, "url": new_url, "logo": new_logo})

            save_yaml("partners.yaml", entries)
            flash("Partner aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="partners"))

        if form_name == "resources":
            entries = []
            for idx in range(len(resources_data)):
                if request.form.get(f"resources-{idx}-delete") == "on":
                    continue

                title = request.form.get(f"resources-{idx}-title", "").strip()
                description = request.form.get(f"resources-{idx}-description", "").strip()
                image = request.form.get(f"resources-{idx}-image", "").strip()
                image_alt = request.form.get(f"resources-{idx}-image-alt", "").strip()
                file_value = request.form.get(f"resources-{idx}-file", "").strip()
                file_label = request.form.get(f"resources-{idx}-file-label", "").strip()

                image_upload = request.files.get(f"resources-{idx}-image-upload")
                if image_upload and image_upload.filename:
                    saved_image = _save_upload(image_upload, f"resource-{idx}-image")
                    if saved_image:
                        image = saved_image

                file_upload = request.files.get(f"resources-{idx}-file-upload")
                if file_upload and file_upload.filename:
                    saved_file = _save_upload(file_upload, f"resource-{idx}-file")
                    if saved_file:
                        file_value = saved_file

                if any([title, description, image, file_value, image_alt, file_label]):
                    entry = {"title": title}
                    if description:
                        entry["description"] = description
                    if image:
                        entry["image"] = image
                    if image_alt:
                        entry["image_alt"] = image_alt
                    if file_value:
                        entry["file"] = file_value
                    if file_label:
                        entry["file_label"] = file_label
                    entries.append(entry)

            new_title = request.form.get("resources-new-title", "").strip()
            new_description = request.form.get("resources-new-description", "").strip()
            new_image = request.form.get("resources-new-image", "").strip()
            new_image_alt = request.form.get("resources-new-image-alt", "").strip()
            new_file = request.form.get("resources-new-file", "").strip()
            new_file_label = request.form.get("resources-new-file-label", "").strip()

            new_image_upload = request.files.get("resources-new-image-upload")
            if new_image_upload and new_image_upload.filename:
                saved_new_image = _save_upload(new_image_upload, "resource-new-image")
                if saved_new_image:
                    new_image = saved_new_image

            new_file_upload = request.files.get("resources-new-file-upload")
            if new_file_upload and new_file_upload.filename:
                saved_new_file = _save_upload(new_file_upload, "resource-new-file")
                if saved_new_file:
                    new_file = saved_new_file

            if any([new_title, new_description, new_image, new_file, new_image_alt, new_file_label]):
                entry = {"title": new_title}
                if new_description:
                    entry["description"] = new_description
                if new_image:
                    entry["image"] = new_image
                if new_image_alt:
                    entry["image_alt"] = new_image_alt
                if new_file:
                    entry["file"] = new_file
                if new_file_label:
                    entry["file_label"] = new_file_label
                entries.append(entry)

            save_yaml("resources.yaml", entries)
            flash("Ressourcen aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="resources"))

        if form_name == "services":
            entries = []
            used_slugs = set()
            for idx in range(len(services_data)):
                if request.form.get(f"services-{idx}-delete") == "on":
                    continue

                name = request.form.get(f"services-{idx}-name", "").strip()
                description = request.form.get(f"services-{idx}-description", "").strip()
                offered_by = request.form.get(f"services-{idx}-offered-by", "").strip()
                image = request.form.get(f"services-{idx}-image", "").strip()
                image_alt = request.form.get(f"services-{idx}-image-alt", "").strip()
                status = request.form.get(f"services-{idx}-status", "open")
                if status not in {"open", "closed"}:
                    status = "open"
                contacts_raw = request.form.get(f"services-{idx}-contacts", "")
                gallery_raw = request.form.get(f"services-{idx}-gallery", "")
                prices_raw = request.form.get(f"services-{idx}-prices", "")
                badge_discounted = request.form.get(f"services-{idx}-badge-discounted") == "on"
                badge_limited = request.form.get(f"services-{idx}-badge-limited") == "on"
                slug_input = request.form.get(f"services-{idx}-slug", "").strip()

                upload = request.files.get(f"services-{idx}-image-upload")
                saved_path = _save_upload(upload, f"service-{idx}") if upload else None
                if saved_path:
                    image = saved_path

                contacts = _parse_socials(contacts_raw)
                gallery_items = []
                gallery_size_raw = request.form.get(f"services-{idx}-gallery-size")
                try:
                    gallery_size = int(gallery_size_raw)
                except (TypeError, ValueError):
                    existing_gallery = services_data[idx].get("gallery") if idx < len(services_data) else []
                    gallery_size = len(existing_gallery) if isinstance(existing_gallery, list) else 0
                for gallery_idx in range(max(0, gallery_size)):
                    if request.form.get(f"services-{idx}-gallery-{gallery_idx}-delete") == "on":
                        continue
                    image_value = (request.form.get(
                        f"services-{idx}-gallery-{gallery_idx}-image", ""
                    ) or "").strip()
                    upload_field = request.files.get(
                        f"services-{idx}-gallery-{gallery_idx}-upload"
                    )
                    if upload_field and upload_field.filename:
                        saved_gallery = _save_upload(
                            upload_field, f"service-{idx}-gallery-{gallery_idx + 1}"
                        )
                        if saved_gallery:
                            image_value = saved_gallery
                    alt_value = (request.form.get(
                        f"services-{idx}-gallery-{gallery_idx}-alt", ""
                    ) or "").strip()
                    if image_value:
                        gallery_items.append(
                            _compact_dict({"image": image_value, "alt": alt_value})
                        )

                new_gallery_image = (request.form.get(
                    f"services-{idx}-gallery-new-image", ""
                ) or "").strip()
                new_gallery_upload = request.files.get(
                    f"services-{idx}-gallery-new-upload"
                )
                if new_gallery_upload and new_gallery_upload.filename:
                    saved_new_gallery = _save_upload(
                        new_gallery_upload, f"service-{idx}-gallery-new"
                    )
                    if saved_new_gallery:
                        new_gallery_image = saved_new_gallery
                new_gallery_alt = (request.form.get(
                    f"services-{idx}-gallery-new-alt", ""
                ) or "").strip()
                if new_gallery_image:
                    gallery_items.append(
                        _compact_dict({"image": new_gallery_image, "alt": new_gallery_alt})
                    )

                gallery = gallery_items or _parse_gallery_lines(gallery_raw)
                prices = _parse_price_lines(prices_raw)
                badges = []
                if badge_discounted:
                    badges.append("discounted")
                if badge_limited:
                    badges.append("limited")

                if any([name, description, offered_by, image, image_alt, contacts, gallery, prices, badges]):
                    slug = _ensure_unique_slug(slug_input or name or f"service-{idx + 1}", used_slugs, f"service-{idx + 1}")
                    entry = {
                        "slug": slug,
                        "name": name,
                        "description": description,
                        "offered_by": offered_by,
                        "image": image,
                        "image_alt": image_alt,
                        "status": status,
                    }
                    if contacts:
                        entry["contacts"] = contacts
                    if badges:
                        entry["badges"] = badges
                    if gallery:
                        entry["gallery"] = gallery
                    if prices:
                        entry["prices"] = prices
                    entries.append(entry)

            new_name = request.form.get("services-new-name", "").strip()
            new_description = request.form.get("services-new-description", "").strip()
            new_offered_by = request.form.get("services-new-offered-by", "").strip()
            new_image = request.form.get("services-new-image", "").strip()
            new_image_alt = request.form.get("services-new-image-alt", "").strip()
            new_status = request.form.get("services-new-status", "open")
            if new_status not in {"open", "closed"}:
                new_status = "open"
            new_contacts_raw = request.form.get("services-new-contacts", "")
            new_gallery_raw = request.form.get("services-new-gallery", "")
            new_prices_raw = request.form.get("services-new-prices", "")
            new_badge_discounted = request.form.get("services-new-badge-discounted") == "on"
            new_badge_limited = request.form.get("services-new-badge-limited") == "on"
            new_upload = request.files.get("services-new-image-upload")
            new_saved = _save_upload(new_upload, "service-new") if new_upload else None
            if new_saved:
                new_image = new_saved
            new_contacts = _parse_socials(new_contacts_raw)
            new_gallery_image = (request.form.get("services-new-gallery-image", "") or "").strip()
            new_gallery_upload = request.files.get("services-new-gallery-upload")
            if new_gallery_upload and new_gallery_upload.filename:
                saved_new_gallery = _save_upload(new_gallery_upload, "service-new-gallery")
                if saved_new_gallery:
                    new_gallery_image = saved_new_gallery
            new_gallery_alt = (request.form.get("services-new-gallery-alt", "") or "").strip()
            new_gallery = []
            if new_gallery_image:
                new_gallery.append(
                    _compact_dict({"image": new_gallery_image, "alt": new_gallery_alt})
                )
            if not new_gallery:
                new_gallery = _parse_gallery_lines(new_gallery_raw)
            new_prices = _parse_price_lines(new_prices_raw)
            new_badges = []
            if new_badge_discounted:
                new_badges.append("discounted")
            if new_badge_limited:
                new_badges.append("limited")

            if any([new_name, new_description, new_offered_by, new_image, new_image_alt, new_contacts, new_gallery, new_prices, new_badges]):
                new_slug = _ensure_unique_slug(new_name or f"service-{len(entries) + 1}", used_slugs, f"service-{len(entries) + 1}")
                new_entry = {
                    "slug": new_slug,
                    "name": new_name,
                    "description": new_description,
                    "offered_by": new_offered_by,
                    "image": new_image,
                    "image_alt": new_image_alt,
                    "status": new_status,
                }
                if new_contacts:
                    new_entry["contacts"] = new_contacts
                if new_badges:
                    new_entry["badges"] = new_badges
                if new_gallery:
                    new_entry["gallery"] = new_gallery
                if new_prices:
                    new_entry["prices"] = new_prices
                entries.append(new_entry)

            save_yaml("services.yaml", entries)
            flash("Services aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="services"))

        if form_name == "about":
            hero = about_data.get("hero", {})
            goals_source = about_data.get("goals", [])
            offer_source = about_data.get("offer", [])
            faq_source = about_data.get("faq", [])
            team_source = about_data.get("team", [])

            hero_image = request.form.get("about-hero-image", hero.get("image", "")).strip()
            hero_upload = request.files.get("about-hero-image-upload")
            saved_hero = _save_upload(hero_upload, "about-hero") if hero_upload else None
            if saved_hero:
                hero_image = saved_hero

            hero_scale_default = hero.get("image_scale", 100)
            try:
                hero_scale_default = int(hero_scale_default)
            except (TypeError, ValueError):
                hero_scale_default = 100
            scale_raw = request.form.get("about-hero-image-scale", "").strip()
            hero_scale = hero_scale_default
            if scale_raw:
                try:
                    hero_scale = int(scale_raw)
                except ValueError:
                    hero_scale = hero_scale_default
            hero_scale = max(10, min(hero_scale, 300))

            hero_entry = {
                "title": request.form.get("about-hero-title", hero.get("title", "")).strip(),
                "lead": request.form.get("about-hero-lead", hero.get("lead", "")).strip(),
                "body": request.form.get("about-hero-body", hero.get("body", "")).strip(),
                "image": hero_image,
                "image_alt": request.form.get("about-hero-image-alt", hero.get("image_alt", "")).strip(),
                "image_scale": hero_scale,
            }

            goals = []
            goal_count_value = request.form.get("about-goals-count")
            try:
                goal_count = int(goal_count_value)
            except (TypeError, ValueError):
                goal_count = len(goals_source)
            for idx in range(goal_count):
                if request.form.get(f"about-goals-{idx}-delete") == "on":
                    continue
                title = request.form.get(f"about-goals-{idx}-title", "").strip()
                description = request.form.get(f"about-goals-{idx}-description", "").strip()
                if title or description:
                    goals.append({"title": title, "description": description})

            new_goal_title = request.form.get("about-goals-new-title", "").strip()
            new_goal_description = request.form.get("about-goals-new-description", "").strip()
            if new_goal_title or new_goal_description:
                goals.append({"title": new_goal_title, "description": new_goal_description})

            offers = []
            offer_count_value = request.form.get("about-offer-count")
            try:
                offer_count = int(offer_count_value)
            except (TypeError, ValueError):
                offer_count = len(offer_source)
            for idx in range(offer_count):
                if request.form.get(f"about-offer-{idx}-delete") == "on":
                    continue
                title = request.form.get(f"about-offer-{idx}-title", "").strip()
                description = request.form.get(f"about-offer-{idx}-description", "").strip()
                if title or description:
                    offers.append({"title": title, "description": description})

            new_offer_title = request.form.get("about-offer-new-title", "").strip()
            new_offer_description = request.form.get("about-offer-new-description", "").strip()
            if new_offer_title or new_offer_description:
                offers.append({"title": new_offer_title, "description": new_offer_description})

            faq_entries = []
            faq_count_value = request.form.get("about-faq-count")
            try:
                faq_count = int(faq_count_value)
            except (TypeError, ValueError):
                faq_count = len(faq_source)
            for idx in range(faq_count):
                if request.form.get(f"about-faq-{idx}-delete") == "on":
                    continue
                question = request.form.get(f"about-faq-{idx}-question", "").strip()
                answer = request.form.get(f"about-faq-{idx}-answer", "").strip()
                if question or answer:
                    faq_entries.append({"question": question, "answer": answer})

            new_question = request.form.get("about-faq-new-question", "").strip()
            new_answer = request.form.get("about-faq-new-answer", "").strip()
            if new_question or new_answer:
                faq_entries.append({"question": new_question, "answer": new_answer})

            team_entries = []
            team_count_value = request.form.get("about-team-count")
            try:
                team_count = int(team_count_value)
            except (TypeError, ValueError):
                team_count = len(team_source)
            for idx in range(team_count):
                if request.form.get(f"about-team-{idx}-delete") == "on":
                    continue
                name = request.form.get(f"about-team-{idx}-name", "").strip()
                role = request.form.get(f"about-team-{idx}-role", "").strip()
                bio = request.form.get(f"about-team-{idx}-bio", "").strip()
                existing_member = team_source[idx] if idx < len(team_source) else {}
                image_value = request.form.get(
                    f"about-team-{idx}-image", existing_member.get("image", "")
                ).strip()
                upload = request.files.get(f"about-team-{idx}-image-upload")
                saved_image = _save_upload(upload, f"about-team-{idx}") if upload else None
                if saved_image:
                    image_value = saved_image
                if any([name, role, bio, image_value]):
                    team_entries.append(
                        {
                            "name": name,
                            "role": role,
                            "bio": bio,
                            "image": image_value,
                        }
                    )

            new_team_name = request.form.get("about-team-new-name", "").strip()
            new_team_role = request.form.get("about-team-new-role", "").strip()
            new_team_bio = request.form.get("about-team-new-bio", "").strip()
            new_team_image = request.form.get("about-team-new-image", "").strip()
            new_team_upload = request.files.get("about-team-new-image-upload")
            new_saved_image = _save_upload(new_team_upload, "about-team-new") if new_team_upload else None
            if new_saved_image:
                new_team_image = new_saved_image
            if any([new_team_name, new_team_role, new_team_bio, new_team_image]):
                team_entries.append(
                    {
                        "name": new_team_name,
                        "role": new_team_role,
                        "bio": new_team_bio,
                        "image": new_team_image,
                    }
                )

            about_payload = {
                "hero": hero_entry,
                "goals": goals,
                "offer": offers,
                "faq": faq_entries,
                "team": team_entries,
            }
            save_yaml("about.yaml", about_payload)
            flash("Über-uns-Inhalte aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="about"))

        if form_name == "talents":
            updated_members = []
            member_count_raw = request.form.get("talent-count", len(talents_data))
            try:
                member_count = int(member_count_raw)
            except (TypeError, ValueError):
                member_count = len(talents_data)

            for idx in range(member_count):
                prefix = f"talent-{idx}"
                if request.form.get(f"{prefix}-delete") == "on":
                    continue
                slug = request.form.get(f"{prefix}-slug", "").strip()
                if not slug:
                    continue

                name = request.form.get(f"{prefix}-name", "").strip() or slug.title()
                birthday = request.form.get(f"{prefix}-birthday", "").strip()
                species = request.form.get(f"{prefix}-species", "").strip()
                height = request.form.get(f"{prefix}-height", "").strip()
                motto = request.form.get(f"{prefix}-motto", "").strip()
                introduction = request.form.get(f"{prefix}-introduction", "").strip()
                specialties = request.form.get(f"{prefix}-specialties", "").strip()
                favorites = _parse_collection(request.form.get(f"{prefix}-favorites", ""))
                socials = _parse_socials(request.form.get(f"{prefix}-socials", ""))
                profile_image = request.form.get(f"{prefix}-profile-image", "").strip()
                fullbody_image = request.form.get(f"{prefix}-fullbody-image", "").strip()

                profile_upload = request.files.get(f"{prefix}-profile-upload")
                if profile_upload:
                    saved_profile = _save_upload(profile_upload, f"{slug}-profile")
                    if saved_profile:
                        profile_image = saved_profile

                fullbody_upload = request.files.get(f"{prefix}-fullbody-upload")
                if fullbody_upload:
                    saved_fullbody = _save_upload(fullbody_upload, f"{slug}-fullbody")
                    if saved_fullbody:
                        fullbody_image = saved_fullbody

                updated_members.append(
                    {
                        "slug": slug,
                        "name": name,
                        "birthday": birthday,
                        "species": species,
                        "height": height,
                        "profile_image": profile_image,
                        "fullbody_image": fullbody_image or profile_image,
                        "favorites": favorites,
                        "specialties": specialties,
                        "motto": motto,
                        "socials": socials,
                        "introduction": introduction,
                    }
                )

            new_slug = request.form.get("talent-new-slug", "").strip()
            if new_slug:
                new_profile = request.form.get("talent-new-profile-image", "").strip()
                new_fullbody = request.form.get("talent-new-fullbody-image", "").strip()
                new_profile_upload = request.files.get("talent-new-profile-upload")
                if new_profile_upload:
                    saved_profile = _save_upload(new_profile_upload, f"{new_slug}-profile")
                    if saved_profile:
                        new_profile = saved_profile
                new_fullbody_upload = request.files.get("talent-new-fullbody-upload")
                if new_fullbody_upload:
                    saved_fullbody = _save_upload(new_fullbody_upload, f"{new_slug}-fullbody")
                    if saved_fullbody:
                        new_fullbody = saved_fullbody

                updated_members.append(
                    {
                        "slug": new_slug,
                        "name": request.form.get("talent-new-name", new_slug.title()).strip() or new_slug.title(),
                        "birthday": request.form.get("talent-new-birthday", "").strip(),
                        "species": request.form.get("talent-new-species", "").strip(),
                        "height": request.form.get("talent-new-height", "").strip(),
                        "profile_image": new_profile,
                        "fullbody_image": new_fullbody or new_profile,
                        "favorites": _parse_collection(request.form.get("talent-new-favorites", "")),
                        "specialties": request.form.get("talent-new-specialties", "").strip(),
                        "motto": request.form.get("talent-new-motto", "").strip(),
                        "socials": _parse_socials(request.form.get("talent-new-socials", "")),
                        "introduction": request.form.get("talent-new-introduction", "").strip(),
                    }
                )

            save_yaml("talents.yaml", {"members": updated_members})
            get_talent_data.cache_clear()
            flash("Talente aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="talents"))

        if form_name == "shop":
            current_items = shop_items
            updated_items = []
            seen_slugs = set()
            for index, existing in enumerate(current_items):
                if request.form.get(f"shop-{index}-delete") == "on":
                    continue

                title = request.form.get(f"shop-{index}-title", existing["title"]).strip()
                streamer = request.form.get(f"shop-{index}-streamer", existing["streamer"]).strip()
                description = request.form.get(f"shop-{index}-description", existing["description"]).strip()
                est_arrival = request.form.get(f"shop-{index}-arrival", existing["est_arrival"]).strip()
                price_value = _clean_price_input(
                    request.form.get(f"shop-{index}-price"), existing.get("price_value")
                )
                original_price_value = _clean_price_input(
                    request.form.get(f"shop-{index}-original-price"),
                    existing.get("original_price_value"),
                )
                purchase_url = request.form.get(
                    f"shop-{index}-purchase-url", existing["purchase_url"]
                ).strip()
                badge = request.form.get(f"shop-{index}-badge", existing["badge"]).strip()
                effect = request.form.get(f"shop-{index}-effect", existing.get("effect", "")).strip()
                if effect not in SHOP_EFFECTS:
                    effect = ""
                stock_value = _parse_int(
                    request.form.get(f"shop-{index}-stock"), minimum=0
                )
                max_per_order = _parse_int(
                    request.form.get(f"shop-{index}-max-per-order"), minimum=1
                )
                image = request.form.get(f"shop-{index}-image", existing["image"]).strip()
                slug_candidate = request.form.get(f"shop-{index}-slug", existing["slug"]).strip()
                if not slug_candidate:
                    slug_candidate = _slugify(title)
                if not slug_candidate:
                    slug_candidate = f"item-{uuid4().hex[:8]}"
                image_upload = request.files.get(f"shop-{index}-image-upload")
                if image_upload:
                    saved_image = _save_upload(image_upload, slug_candidate)
                    if saved_image:
                        image = saved_image
                slug = slug_candidate
                base_slug = slug
                suffix = 2
                while slug in seen_slugs:
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1
                seen_slugs.add(slug)
                option_prefix = f"shop-{index}-option"
                option_labels = [
                    value.strip() for value in request.form.getlist(f"{option_prefix}-label")
                ]
                option_prices = request.form.getlist(f"{option_prefix}-price")
                option_notes = [
                    value.strip() for value in request.form.getlist(f"{option_prefix}-note")
                ]
                options = []
                for label, raw_price, note in zip_longest(
                    option_labels, option_prices, option_notes, fillvalue=""
                ):
                    if not label:
                        continue
                    price_clean = _clean_price_input(raw_price, None)
                    option_entry = {"label": label}
                    if note:
                        option_entry["note"] = note
                    if price_clean is not None:
                        option_entry["price"] = price_clean
                    options.append(option_entry)

                item_payload = {
                    "slug": slug,
                    "title": title or "Unbenannt",
                    "streamer": streamer,
                    "description": description,
                    "est_arrival": est_arrival,
                    "price": price_value,
                    "original_price": original_price_value,
                    "purchase_url": purchase_url,
                    "badge": badge,
                    "effect": effect,
                    "image": image,
                    "options": options,
                    "stock": stock_value,
                    "max_per_order": max_per_order,
                }
                updated_items.append(_compact_dict(item_payload))

            new_title = request.form.get("shop-new-title", "").strip()
            new_slug = request.form.get("shop-new-slug", "").strip()
            new_streamer = request.form.get("shop-new-streamer", "").strip()
            new_description = request.form.get("shop-new-description", "").strip()
            new_arrival = request.form.get("shop-new-arrival", "").strip()
            new_price_value = _clean_price_input(request.form.get("shop-new-price"), None)
            new_original_price = _clean_price_input(
                request.form.get("shop-new-original-price"), None
            )
            new_purchase_url = request.form.get("shop-new-purchase-url", "").strip()
            new_badge = request.form.get("shop-new-badge", "").strip()
            new_effect = request.form.get("shop-new-effect", "").strip()
            if new_effect not in SHOP_EFFECTS:
                new_effect = ""
            new_image = request.form.get("shop-new-image", "").strip()
            new_stock = _parse_int(request.form.get("shop-new-stock"), minimum=0)
            new_max_per_order = _parse_int(
                request.form.get("shop-new-max-per-order"), minimum=1
            )
            new_option_labels = [
                value.strip() for value in request.form.getlist("shop-new-option-label")
            ]
            new_option_prices = request.form.getlist("shop-new-option-price")
            new_option_notes = [
                value.strip() for value in request.form.getlist("shop-new-option-note")
            ]
            new_options = []
            for label, raw_price, note in zip_longest(
                new_option_labels, new_option_prices, new_option_notes, fillvalue=""
            ):
                if not label:
                    continue
                cleaned_price = _clean_price_input(raw_price, None)
                entry_option = {"label": label}
                if note:
                    entry_option["note"] = note
                if cleaned_price is not None:
                    entry_option["price"] = cleaned_price
                new_options.append(entry_option)

            if (
                new_title
                or new_streamer
                or new_description
                or new_slug
                or new_price_value is not None
                or new_original_price is not None
            ):
                slug = new_slug or _slugify(new_title)
                if not slug:
                    slug = f"item-{uuid4().hex[:8]}"
                base_slug = slug
                suffix = 2
                while slug in seen_slugs:
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1
                seen_slugs.add(slug)
                new_image_upload = request.files.get("shop-new-image-upload")
                if new_image_upload:
                    saved_new_image = _save_upload(new_image_upload, slug)
                    if saved_new_image:
                        new_image = saved_new_image
                new_payload = {
                    "slug": slug,
                    "title": new_title or "Unbenannt",
                    "streamer": new_streamer,
                    "description": new_description,
                    "est_arrival": new_arrival,
                    "price": new_price_value,
                    "original_price": new_original_price,
                    "purchase_url": new_purchase_url,
                    "badge": new_badge,
                    "effect": new_effect,
                    "image": new_image,
                    "options": new_options,
                    "stock": new_stock,
                    "max_per_order": new_max_per_order,
                }
                updated_items.append(_compact_dict(new_payload))

            save_yaml("shop.yaml", updated_items)
            flash("Shop-Produkte aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="shop"))

        if form_name == "artworks":
            intro_input = request.form.get("artworks-intro", "")
            intro_clean = intro_input.strip()
            count_raw = request.form.get("artworks-count")
            try:
                talent_count = int(count_raw)
            except (TypeError, ValueError):
                talent_count = len(artworks_admin.get("talents", []))
            updated_talents = []
            for idx in range(max(0, talent_count)):
                delete_flag = request.form.get(f"artworks-{idx}-delete") == "on"
                slug_value = (request.form.get(f"artworks-{idx}-slug", "") or "").strip()
                name_value = (request.form.get(f"artworks-{idx}-name", "") or "").strip()
                if delete_flag and not is_admin:
                    # Talent-Nutzer dürfen eigene Einträge nicht komplett löschen
                    delete_flag = False
                if delete_flag:
                    continue
                slug = slug_value or _slugify(name_value)
                if not slug:
                    slug = f"talent-{idx + 1}"
                if not name_value:
                    name_value = slug
                tagline_value = (request.form.get(f"artworks-{idx}-tagline", "") or "").strip()
                description_value = (request.form.get(f"artworks-{idx}-description", "") or "").strip()
                interval_raw = request.form.get(f"artworks-{idx}-interval", "")
                interval_value = _parse_int(interval_raw, minimum=1000)

                gallery_items = []
                gallery_size_raw = request.form.get(f"artworks-{idx}-gallery-size")
                try:
                    gallery_size = int(gallery_size_raw)
                except (TypeError, ValueError):
                    existing = artworks_admin.get("talents", [])
                    gallery_size = len(existing[idx]["gallery"]) if idx < len(existing) else 0
                for gallery_idx in range(max(0, gallery_size)):
                    if request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-delete") == "on":
                        continue
                    image_value = (request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-image", "") or "").strip()
                    upload_field = request.files.get(f"artworks-{idx}-gallery-{gallery_idx}-upload")
                    if upload_field and upload_field.filename:
                        saved_image = _save_upload(upload_field, f"artwork-{slug}-{gallery_idx + 1}")
                        if saved_image:
                            image_value = saved_image
                    alt_value = (request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-alt", "") or "").strip()
                    caption_value = (request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-caption", "") or "").strip()
                    credits_value = (request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-credits", "") or "").strip()
                    credits_url_value = (
                        request.form.get(f"artworks-{idx}-gallery-{gallery_idx}-credits-url", "") or ""
                    ).strip()
                    watermark_value = request.form.get(
                        f"artworks-{idx}-gallery-{gallery_idx}-watermark"
                    )
                    watermark_flag = bool(watermark_value == "on")
                    if not image_value:
                        continue
                    gallery_items.append(
                        _compact_dict(
                            {
                                "image": image_value,
                                "alt": alt_value,
                                "caption": caption_value,
                                "credits": credits_value,
                                "credits_url": credits_url_value,
                                "watermark": watermark_flag,
                            }
                        )
                    )

                new_entries = []
                new_count_raw = request.form.get(f"artworks-{idx}-gallery-new-count", "0")
                try:
                    new_count = max(0, int(new_count_raw))
                except (TypeError, ValueError):
                    new_count = 0
                for new_idx in range(new_count):
                    prefix = f"artworks-{idx}-gallery-new-{new_idx}"
                    new_image_value = (request.form.get(f"{prefix}-image", "") or "").strip()
                    new_upload = request.files.get(f"{prefix}-upload")
                    if new_upload and new_upload.filename:
                        saved_new_image = _save_upload(new_upload, f"artwork-{slug}-new-{new_idx + 1}")
                        if saved_new_image:
                            new_image_value = saved_new_image
                    new_alt_value = (request.form.get(f"{prefix}-alt", "") or "").strip()
                    new_caption_value = (request.form.get(f"{prefix}-caption", "") or "").strip()
                    new_credits_value = (request.form.get(f"{prefix}-credits", "") or "").strip()
                    new_credits_url_value = (request.form.get(f"{prefix}-credits-url", "") or "").strip()
                    new_watermark_flag = request.form.get(f"{prefix}-watermark") == "on"
                    if new_image_value:
                        new_entries.append(
                            _compact_dict(
                                {
                                    "image": new_image_value,
                                    "alt": new_alt_value,
                                    "caption": new_caption_value,
                                    "credits": new_credits_value,
                                    "credits_url": new_credits_url_value,
                                    "watermark": new_watermark_flag,
                                }
                            )
                        )

                if not new_entries:
                    legacy_new_image = (request.form.get(f"artworks-{idx}-gallery-new-image", "") or "").strip()
                    legacy_new_upload = request.files.get(f"artworks-{idx}-gallery-new-upload")
                    if legacy_new_upload and legacy_new_upload.filename:
                        saved_new_image = _save_upload(legacy_new_upload, f"artwork-{slug}-new")
                        if saved_new_image:
                            legacy_new_image = saved_new_image
                    legacy_new_alt = (request.form.get(f"artworks-{idx}-gallery-new-alt", "") or "").strip()
                    legacy_new_caption = (request.form.get(f"artworks-{idx}-gallery-new-caption", "") or "").strip()
                    legacy_new_credits = (request.form.get(f"artworks-{idx}-gallery-new-credits", "") or "").strip()
                    legacy_new_credits_url = (request.form.get(f"artworks-{idx}-gallery-new-credits-url", "") or "").strip()
                    legacy_new_watermark = request.form.get(f"artworks-{idx}-gallery-new-watermark") == "on"
                    if legacy_new_image:
                        new_entries.append(
                            _compact_dict(
                                {
                                    "image": legacy_new_image,
                                    "alt": legacy_new_alt,
                                    "caption": legacy_new_caption,
                                    "credits": legacy_new_credits,
                                    "credits_url": legacy_new_credits_url,
                                    "watermark": legacy_new_watermark,
                                }
                            )
                        )

                gallery_items.extend(new_entries)

                talent_payload = {
                    "slug": slug,
                    "name": name_value,
                    "tagline": tagline_value,
                    "description": description_value,
                    "gallery": gallery_items,
                }
                if interval_value is not None:
                    talent_payload["interval"] = interval_value
                updated_talents.append(talent_payload)

            new_name_value = (request.form.get("artworks-new-name", "") or "").strip()
            new_slug_input = (request.form.get("artworks-new-slug", "") or "").strip() if is_admin else ""
            new_tagline_value = (request.form.get("artworks-new-tagline", "") or "").strip()
            new_interval_raw = request.form.get("artworks-new-interval", "")
            new_interval_value = _parse_int(new_interval_raw, minimum=1000)

            slug_hint = _slugify(new_slug_input or new_name_value) or f"talent-{len(updated_talents) + 1}"

            new_gallery_items = []
            new_gallery_count_raw = request.form.get("artworks-new-gallery-count", "0")
            try:
                new_gallery_count = max(0, int(new_gallery_count_raw))
            except (TypeError, ValueError):
                new_gallery_count = 0

            for new_idx in range(new_gallery_count):
                prefix = f"artworks-new-gallery-{new_idx}"
                image_value = (request.form.get(f"{prefix}-image", "") or "").strip()
                upload_field = request.files.get(f"{prefix}-upload")
                if upload_field and upload_field.filename:
                    saved_new_gallery = _save_upload(upload_field, f"artwork-{slug_hint}-new-{new_idx + 1}")
                    if saved_new_gallery:
                        image_value = saved_new_gallery
                alt_value = (request.form.get(f"{prefix}-alt", "") or "").strip()
                caption_value = (request.form.get(f"{prefix}-caption", "") or "").strip()
                credits_value = (request.form.get(f"{prefix}-credits", "") or "").strip()
                credits_url_value = (request.form.get(f"{prefix}-credits-url", "") or "").strip()
                watermark_flag = request.form.get(f"{prefix}-watermark") == "on"
                if image_value:
                    new_gallery_items.append(
                        _compact_dict(
                            {
                                "image": image_value,
                                "alt": alt_value,
                                "caption": caption_value,
                                "credits": credits_value,
                                "credits_url": credits_url_value,
                                "watermark": watermark_flag,
                            }
                        )
                    )

            if not new_gallery_items:
                legacy_image = (request.form.get("artworks-new-gallery-image", "") or "").strip()
                legacy_upload = request.files.get("artworks-new-gallery-upload")
                if legacy_upload and legacy_upload.filename:
                    saved_new_gallery = _save_upload(legacy_upload, f"artwork-{slug_hint}-new")
                    if saved_new_gallery:
                        legacy_image = saved_new_gallery
                legacy_alt = (request.form.get("artworks-new-gallery-alt", "") or "").strip()
                legacy_caption = (request.form.get("artworks-new-gallery-caption", "") or "").strip()
                legacy_credits = (request.form.get("artworks-new-gallery-credits", "") or "").strip()
                legacy_credits_url = (request.form.get("artworks-new-gallery-credits-url", "") or "").strip()
                legacy_watermark = request.form.get("artworks-new-gallery-watermark") == "on"
                if legacy_image:
                    new_gallery_items.append(
                        _compact_dict(
                            {
                                "image": legacy_image,
                                "alt": legacy_alt,
                                "caption": legacy_caption,
                                "credits": legacy_credits,
                                "credits_url": legacy_credits_url,
                                "watermark": legacy_watermark,
                            }
                        )
                    )

            if any(
                [
                    new_name_value,
                    new_slug_input,
                    new_tagline_value,
                    new_interval_raw,
                    new_gallery_items,
                ]
            ):
                new_slug = _slugify(new_slug_input or new_name_value) or slug_hint
                new_name = new_name_value or new_slug
                new_talent_payload = {
                    "slug": new_slug,
                    "name": new_name,
                    "tagline": new_tagline_value,
                    "gallery": new_gallery_items,
                }
                if new_interval_value is not None:
                    new_talent_payload["interval"] = new_interval_value
                updated_talents.append(new_talent_payload)

            save_yaml(
                "artworks.yaml",
                {
                    "intro": intro_clean,
                    "talents": updated_talents,
                },
            )
            flash("Galerie wurde aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="artworks"))

        if form_name == "privacy":
            body = request.form.get("privacy-body", "")
            save_markdown(PRIVACY_FILE, body)
            flash("Datenschutzerklärung aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="privacy"))

        if form_name == "users" and is_admin:
            action = (request.form.get("user-action") or "").strip()
            username = (request.form.get("username") or "").strip()
            if action == "create":
                password = request.form.get("password", "")
                make_admin = request.form.get("is_admin") == "on"
                ok, message = _create_user(username, password, is_admin=make_admin)
                flash(message, "success" if ok else "error")
            elif action == "delete":
                ok, message = _delete_user(username)
                flash(message, "success" if ok else "error")
            elif action == "reset_password":
                password = request.form.get("password", "")
                ok, message = _update_user_password(username, password)
                flash(message, "success" if ok else "error")
            else:
                flash("Unbekannte Benutzeraktion.", "error")
            return redirect(url_for("admin_dashboard", tab="users"))

        if form_name == "settings":
            maintenance_value = request.form.get("settings-maintenance") == "on"
            shop_enabled = request.form.get("settings-shop") == "on"
            updated = deepcopy(settings_data)
            updated["maintenance_mode"] = maintenance_value
            updated["shop_enabled"] = shop_enabled
            updated["artworks_panel_enabled"] = request.form.get("settings-artworks-enabled") == "on"
            updated["talents_no_teams"] = request.form.get("settings-talents-no-teams") == "on"
            updated["site_name"] = request.form.get("settings-site-name", "").strip() or DEFAULT_SITE_SETTINGS["site_name"]
            updated["site_tagline"] = request.form.get("settings-site-tagline", "").strip() or DEFAULT_SITE_SETTINGS["site_tagline"]
            updated["site_subtitle"] = request.form.get("settings-site-subtitle", "").strip() or DEFAULT_SITE_SETTINGS["site_subtitle"]

            footer = updated.get("footer", {})
            if not isinstance(footer, dict):
                footer = {}
            footer["about_heading"] = request.form.get("settings-footer-about-heading", "").strip() or "About"
            footer["about_text"] = request.form.get("settings-footer-about-text", "").strip()
            footer["social_heading"] = request.form.get("settings-footer-social-heading", "").strip() or "Socials"
            footer["legal_heading"] = request.form.get("settings-footer-legal-heading", "").strip() or "Rechtliches"
            footer["fine_print"] = request.form.get("settings-footer-fine-print", "").strip() or DEFAULT_SITE_SETTINGS["footer"]["fine_print"]

            socials = []
            existing_socials = footer.get("socials") or []
            for idx, _ in enumerate(existing_socials):
                label = request.form.get(f"settings-social-{idx}-label", "").strip()
                url_value = request.form.get(f"settings-social-{idx}-url", "").strip()
                delete_flag = request.form.get(f"settings-social-{idx}-delete") == "on"
                if delete_flag or not (label and url_value):
                    continue
                socials.append({"label": label, "url": url_value})
            new_social_label = request.form.get("settings-social-new-label", "").strip()
            new_social_url = request.form.get("settings-social-new-url", "").strip()
            if new_social_label and new_social_url:
                socials.append({"label": new_social_label, "url": new_social_url})
            footer["socials"] = socials

            legal_links = []
            existing_legal = footer.get("legal_links") or []
            for idx, _ in enumerate(existing_legal):
                label = request.form.get(f"settings-legal-{idx}-label", "").strip()
                url_value = request.form.get(f"settings-legal-{idx}-url", "").strip()
                delete_flag = request.form.get(f"settings-legal-{idx}-delete") == "on"
                if delete_flag or not (label and url_value):
                    continue
                legal_links.append({"label": label, "url": url_value})
            new_legal_label = request.form.get("settings-legal-new-label", "").strip()
            new_legal_url = request.form.get("settings-legal-new-url", "").strip()
            if new_legal_label and new_legal_url:
                legal_links.append({"label": new_legal_label, "url": new_legal_url})
            footer["legal_links"] = legal_links

            star_defaults = DEFAULT_SITE_SETTINGS.get("starfield", {})
            current_starfield = updated.get("starfield")
            if not isinstance(current_starfield, dict):
                current_starfield = {}
            starfield = {}
            starfield["density_divisor"] = _parse_float_setting(
                request.form.get("settings-star-density"),
                current_starfield.get(
                    "density_divisor", star_defaults.get("density_divisor", 9000)
                ),
                min_value=500,
                max_value=50000,
            )
            starfield["min_count"] = _parse_int_setting(
                request.form.get("settings-star-min-count"),
                current_starfield.get("min_count", star_defaults.get("min_count", 90)),
                min_value=10,
                max_value=5000,
            )
            starfield["speed_min"] = _parse_float_setting(
                request.form.get("settings-star-speed-min"),
                current_starfield.get("speed_min", star_defaults.get("speed_min", 0.03)),
                min_value=0.001,
                max_value=5,
            )
            starfield["speed_max"] = _parse_float_setting(
                request.form.get("settings-star-speed-max"),
                current_starfield.get("speed_max", star_defaults.get("speed_max", 0.12)),
                min_value=0.001,
                max_value=10,
            )
            fast_percent_default = (
                current_starfield.get("fast_fraction", star_defaults.get("fast_fraction", 0.2))
                * 100
            )
            fast_percent = _parse_float_setting(
                request.form.get("settings-star-fast-percent"),
                fast_percent_default,
                min_value=0,
                max_value=100,
            )
            starfield["fast_fraction"] = fast_percent / 100
            starfield["fast_multiplier"] = _parse_float_setting(
                request.form.get("settings-star-fast-multiplier"),
                current_starfield.get(
                    "fast_multiplier", star_defaults.get("fast_multiplier", 2.5)
                ),
                min_value=1,
                max_value=10,
            )
            if starfield["speed_max"] < starfield["speed_min"]:
                starfield["speed_max"] = starfield["speed_min"]

            updated["footer"] = footer
            updated["starfield"] = starfield
            save_settings(updated)
            if not maintenance_value:
                session.pop("maintenance_access", None)
            flash("Einstellungen aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="settings"))

        flash("Unbekanntes Formular.", "error")
        return redirect(url_for("admin_dashboard", tab=tab))

    return render_template(
        "admin/dashboard.html",
        tab=tab,
        news=news_data,
        projects=projects_data,
        services=services_data,
        service_badge_options=SERVICE_BADGES,
        about=about_data,
        talents=talents_data,
        settings=settings_data,
        shop_items=shop_items,
        shop_effects=SHOP_EFFECTS,
        partners=partners_data,
        resources=resources_data,
        homepage=homepage_display,
        calendar_icons=CALENDAR_ICONS,
        calendar_recurrence_options=CALENDAR_RECURRENCE_OPTIONS,
        artworks=artworks_admin,
        artworks_enabled=artworks_enabled,
        is_admin=is_admin,
        is_artworks_manager=is_artworks_manager,
        privacy=privacy_markdown,
        users=users_data,
    )


@app.route("/robots.txt")
def robots_txt():
    sitemap_url = url_for("sitemap_xml", _external=True)
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {sitemap_url}",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    settings = get_settings()
    urls = []
    now_date = datetime.utcnow().date().isoformat()

    def add_url(endpoint, changefreq="monthly", priority="0.5", lastmod=None, **values):
        try:
            loc = url_for(endpoint, _external=True, **values)
        except Exception:
            return
        entry = {
            "loc": loc,
            "changefreq": changefreq,
            "priority": priority,
            "lastmod": lastmod or now_date,
        }
        urls.append(entry)

    add_url("index", changefreq="weekly", priority="1.0")
    add_url("about", changefreq="monthly", priority="0.6")
    add_url("talents", changefreq="weekly", priority="0.8")
    add_url("projects", changefreq="weekly", priority="0.7")
    add_url("services", changefreq="weekly", priority="0.7")
    add_url("partners", changefreq="monthly", priority="0.5")
    add_url("news", changefreq="daily", priority="0.8")
    add_url("contact", changefreq="yearly", priority="0.3")
    add_url("legal_impressum", changefreq="yearly", priority="0.2")
    add_url("legal_privacy", changefreq="yearly", priority="0.2")

    if settings.get("artworks_panel_enabled", True):
        add_url("artworks", changefreq="monthly", priority="0.4")

    if settings.get("shop_enabled", True):
        add_url("shop", changefreq="weekly", priority="0.6")

    _, talent_index = get_talent_data()
    for talent in talent_index.values():
        add_url(
            "talent_detail",
            slug=talent.get("slug"),
            changefreq="weekly",
            priority="0.6",
        )

    for project in get_project_entries():
        add_url(
            "project_detail",
            slug=project.get("slug"),
            changefreq="monthly",
            priority="0.6",
        )

    for service in get_services():
        add_url(
            "service_detail",
            slug=service.get("slug"),
            changefreq="monthly",
            priority="0.5",
        )

    for article in get_news_entries():
        add_url(
            "news_detail",
            slug=article.get("slug"),
            changefreq="weekly",
            priority="0.7",
            lastmod=_sitemap_lastmod(article.get("date")) or now_date,
        )

    if settings.get("shop_enabled", True):
        for item in get_shop_items():
            add_url(
                "shop_detail",
                slug=item.get("slug"),
                changefreq="weekly",
                priority="0.5",
            )

    xml_parts = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in urls:
        xml_parts.append("  <url>")
        xml_parts.append(f"    <loc>{entry['loc']}</loc>")
        if entry.get("lastmod"):
            xml_parts.append(f"    <lastmod>{entry['lastmod']}</lastmod>")
        if entry.get("changefreq"):
            xml_parts.append(f"    <changefreq>{entry['changefreq']}</changefreq>")
        if entry.get("priority"):
            xml_parts.append(f"    <priority>{entry['priority']}</priority>")
        xml_parts.append("  </url>")
    xml_parts.append("</urlset>")
    return Response("\n".join(xml_parts), mimetype="application/xml")


@app.route("/")
def index():
    projects = get_project_entries()[:6]
    news = get_news_entries()[:3]
    partners_data = load_yaml("partners.yaml")
    if not isinstance(partners_data, list):
        partners_data = []
    partners = _prepare_partner_entries(partners_data[:12])
    _, member_index = get_talent_data()
    live_status_map = _get_live_status_map()
    live_profiles = []
    for slug, member in sorted(
        member_index.items(), key=lambda item: (item[1].get("name") or item[0]).lower()
    ):
        status = live_status_map.get(slug, {})
        if status.get("state") != "live":
            continue
        image_url = _resolve_media_url(member.get("profile_image"))
        if not image_url:
            default_asset = f"images/talents/members/{slug}.svg"
            image_url = asset_url(default_asset)
        live_profiles.append(
            {
                "slug": slug,
                "name": member.get("name") or slug,
                "profile_image_url": image_url,
                "title": status.get("title") or "Live auf Twitch",
                "game": status.get("game") or "",
            }
        )
    homepage_config = _prepare_homepage_for_display(get_homepage_settings())
    sections = [section for section in homepage_config.get("sections", []) if section.get("enabled", True)]
    sections.sort(key=lambda item: item.get("order", 0))
    section_lookup = {section.get("id"): section for section in sections if section.get("id")}
    all_talents = sorted(
        [
            {"slug": slug, "name": (member.get("name") or slug)}
            for slug, member in member_index.items()
        ],
        key=lambda item: item["name"].lower(),
    )
    online_now = live_profiles[:6]
    calendar_events = homepage_config.get("calendar", {}).get("events", [])
    upcoming_events = calendar_events[:5] if isinstance(calendar_events, list) else []
    hero_meta = homepage_config.get("hero", {})
    hero_image = hero_meta.get("background_url") or hero_meta.get("logo_url")
    meta_description = hero_meta.get("subtitle") or hero_meta.get("kicker")
    page_meta = build_seo_metadata(
        description=meta_description,
        image=hero_image,
        canonical=url_for("index", _external=True),
    )
    return render_template(
        "index.html",
        projects=projects,
        news=news,
        partners=partners,
        homepage=homepage_config,
        home_sections=sections,
        section_lookup=section_lookup,
        live_profiles=live_profiles,
        all_talents=all_talents,
        online_now=online_now,
        upcoming_events=upcoming_events,
        page_meta=page_meta,
    )


@app.route("/about")
def about():
    data = load_yaml("about.yaml") or {}
    if not isinstance(data, dict):
        data = {}
    hero = data.get("hero", {})
    goals = data.get("goals", [])
    offers = data.get("offer", [])
    faq = data.get("faq", [])
    team = data.get("team", [])
    hero_image = _resolve_media_url(hero.get("image")) if isinstance(hero, dict) else ""
    page_meta = build_seo_metadata(
        title="Über uns",
        description=hero.get("lead") if isinstance(hero, dict) else None,
        image=hero_image,
        canonical=url_for("about", _external=True),
    )
    return render_template(
        "about.html",
        hero=hero,
        goals=goals,
        offers=offers,
        faq=faq,
        team=team,
        page_meta=page_meta,
    )


@app.route("/talents", strict_slashes=False)
def talents():
    teams, _ = get_talent_data()
    settings = get_settings()
    site_name = settings.get("site_name") or DEFAULT_SITE_SETTINGS["site_name"]
    no_teams_mode = True
    live_status = _get_live_status_map()
    all_members = []
    for team in teams:
        all_members.extend(team.get("members", []))
    page_meta = build_seo_metadata(
        title="Talente",
        description=f"Lerne die Talente von {site_name} kennen.",
        canonical=url_for("talents", _external=True),
    )
    return render_template(
        "talents.html",
        teams=teams,
        all_members=all_members,
        no_teams_mode=no_teams_mode,
        live_status=live_status,
        page_meta=page_meta,
    )


@app.route("/talents/<slug>", strict_slashes=False)
def talent_detail(slug):
    _, member_index = get_talent_data()
    talent = member_index.get(slug)
    if not talent:
        abort(404)
    live_status = _get_live_status_map()
    status = live_status.get(slug, {})
    is_live = status.get("state") == "live"
    twitch_embed_url = _build_twitch_embed_url(slug, request.host) if is_live else ""
    description = talent.get("introduction") or talent.get("motto")
    profile_image = talent.get("profile_image") or talent.get("fullbody_image")
    image_url = _resolve_media_url(profile_image)
    page_meta = build_seo_metadata(
        title=talent.get("name"),
        description=description,
        image=image_url,
        canonical=url_for("talent_detail", slug=slug, _external=True),
    )
    can_edit_profile = _user_can_edit_slug(slug)
    return render_template(
        "talent_detail.html",
        talent=talent,
        team=talent["team"],
        live_status=live_status,
        is_live=is_live,
        twitch_embed_url=twitch_embed_url,
        can_edit_profile=can_edit_profile,
        current_auth_slug=session.get("auth_slug"),
        page_meta=page_meta,
    )


@app.route("/projects")
def projects():
    data = get_project_entries()
    settings = get_settings()
    site_name = settings.get("site_name") or DEFAULT_SITE_SETTINGS["site_name"]
    page_meta = build_seo_metadata(
        title="Projekte",
        description=f"Aktuelle und vergangene Projekte von {site_name} im Überblick.",
        canonical=url_for("projects", _external=True),
    )
    return render_template("projects.html", projects=data, page_meta=page_meta)


@app.route("/projects/<slug>")
def project_detail(slug):
    projects = get_project_entries()
    project = next((entry for entry in projects if entry["slug"] == slug), None)
    if not project:
        abort(404)
    related = [entry for entry in projects if entry["slug"] != slug][:3]
    image_url = project.get("image_url") or _resolve_media_url(project.get("image"))
    page_meta = build_seo_metadata(
        title=project.get("title"),
        description=project.get("summary"),
        image=image_url,
        canonical=url_for("project_detail", slug=slug, _external=True),
        section="Projekte",
    )
    return render_template(
        "project_detail.html",
        project=project,
        related=related,
        page_meta=page_meta,
    )

@app.route("/partners")
def partners():
    data = load_yaml("partners.yaml")
    if not isinstance(data, list):
        data = []
    partners = _prepare_partner_entries(data)
    page_meta = build_seo_metadata(
        title="Partner",
        description="Unsere Netzwerk- und Technologiepartner auf einen Blick.",
        canonical=url_for("partners", _external=True),
    )
    return render_template("partners.html", partners=partners, page_meta=page_meta)


@app.route("/internal-resources")
def internal_resources():
    if not (session.get("artworks_manager") or session.get("is_admin")):
        next_target = request.full_path.rstrip("?") if request else None
        return redirect(url_for("gallery_login", next=next_target or request.path))
    resources = get_resource_entries()
    return render_template("internal_resources.html", resources=resources)


@app.route("/artworks")
def artworks():
    settings = get_settings()
    if not settings.get("artworks_panel_enabled", True):
        abort(404)
    data = get_artworks()
    page_meta = build_seo_metadata(
        title="Galerie",
        description=data.get("intro"),
        canonical=url_for("artworks", _external=True),
    )
    return render_template(
        "artworks.html",
        intro=data.get("intro", ""),
        talents=data.get("talents", []),
        page_meta=page_meta,
    )


@app.route("/services")
def services():
    services_list = get_services()
    page_meta = build_seo_metadata(
        title="Services",
        description="Produktions- und Community-Angebote von Astralia im Überblick.",
        canonical=url_for("services", _external=True),
    )
    return render_template("services.html", services=services_list, page_meta=page_meta)


@app.route("/services/<slug>")
def service_detail(slug):
    services_list = get_services()
    service = next((entry for entry in services_list if entry["slug"] == slug), None)
    if not service:
        abort(404)
    related = [entry for entry in services_list if entry["slug"] != slug][:3]
    page_meta = build_seo_metadata(
        title=service.get("name"),
        description=service.get("description"),
        image=service.get("image_url") or service.get("image"),
        canonical=url_for("service_detail", slug=slug, _external=True),
        section="Services",
    )
    return render_template(
        "service_detail.html",
        service=service,
        related=related,
        page_meta=page_meta,
    )


@app.route("/news")
def news():
    data = get_news_entries()
    page_meta = build_seo_metadata(
        title="News",
        description="Aktuelle Neuigkeiten und Events aus dem Astralia-Kosmos.",
        canonical=url_for("news", _external=True),
    )
    return render_template("news.html", posts=data, page_meta=page_meta)


@app.route("/news/<slug>")
def news_detail(slug):
    articles = get_news_entries()
    article = next((entry for entry in articles if entry["slug"] == slug), None)
    if not article:
        abort(404)
    related = [entry for entry in articles if entry["slug"] != slug][:3]
    canonical_url = url_for("news_detail", slug=slug, _external=True)
    image_url = article.get("image_url") or _resolve_media_url(article.get("image"))
    meta_defaults = _compute_meta_defaults(get_settings())
    page_meta = build_seo_metadata(
        title=article.get("title"),
        description=article.get("summary"),
        image=image_url,
        canonical=canonical_url,
        og_type="article",
        published=article.get("date"),
        section="News",
    )
    published_iso = page_meta.get("published_time")
    article_schema = None
    if published_iso:
        article_schema = build_article_schema(
            headline=article.get("title"),
            description=page_meta.get("description"),
            image=page_meta.get("image"),
            canonical=canonical_url,
            date_published=published_iso,
            date_modified=page_meta.get("modified_time") or published_iso,
            publisher=meta_defaults.get("publisher"),
        )
        if article_schema:
            if page_meta.get("structured_data"):
                page_meta["structured_data"].append(article_schema)
            else:
                page_meta["structured_data"] = [article_schema]
    return render_template(
        "news_detail.html",
        article=article,
        related=related,
        page_meta=page_meta,
    )


@app.route("/shop")
def shop():
    settings = get_settings()
    if not settings.get("shop_enabled", True):
        abort(404)
    items = get_shop_items()
    page_meta = build_seo_metadata(
        title="Shop",
        description="Merch und digitale Goodies von Astralia.",
        canonical=url_for("shop", _external=True),
    )
    return render_template("shop.html", items=items, page_meta=page_meta)


@app.route("/shop/<slug>")
def shop_detail(slug):
    settings = get_settings()
    if not settings.get("shop_enabled", True):
        abort(404)
    items = get_shop_items()
    item = next((entry for entry in items if entry["slug"] == slug), None)
    if not item:
        abort(404)
    related = [entry for entry in items if entry["slug"] != slug][:3]
    canonical_url = url_for("shop_detail", slug=slug, _external=True)
    image_url = _resolve_media_url(item.get("image"))
    page_meta = build_seo_metadata(
        title=item.get("title"),
        description=item.get("description"),
        image=image_url,
        canonical=canonical_url,
        og_type="product",
        section="Shop",
    )
    availability = "https://schema.org/OutOfStock" if item.get("sold_out") else "https://schema.org/InStock"
    product_schema = build_product_schema(
        name=item.get("title"),
        description=page_meta.get("description"),
        image=page_meta.get("image"),
        canonical=canonical_url,
        price=item.get("price_value") if item.get("price_value") is not None else item.get("price"),
        currency="EUR",
        availability=availability,
    )
    if product_schema:
        if page_meta.get("structured_data"):
            page_meta["structured_data"].append(product_schema)
        else:
            page_meta["structured_data"] = [product_schema]
    return render_template(
        "shop_detail.html",
        item=item,
        related=related,
        page_meta=page_meta,
    )


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form_data = {
        "name": request.form.get("name", "").strip(),
        "email": request.form.get("email", "").strip(),
        "subject": request.form.get("subject", "").strip(),
        "message": request.form.get("message", "").strip(),
    }

    if request.method == "POST":
        if not all(form_data.values()):
            flash("Bitte fülle alle Felder aus.", "error")
        else:
            msg = Message(
                subject=f"[Astralia Kontakt] {form_data['subject']}",
                recipients=[CONTACT_RECIPIENT],
                reply_to=form_data["email"],
            )
            msg.body = (
                "Neue Kontaktanfrage von Astralia.de\n\n"
                f"Name: {form_data['name']}\n"
                f"E-Mail: {form_data['email']}\n"
                f"Betreff: {form_data['subject']}\n\n"
                f"Nachricht:\n{form_data['message']}\n"
            )
            try:
                mail.send(msg)
            except Exception as exc:  # pragma: no cover - depends on mail backend
                app.logger.exception("Fehler beim Versenden der Kontakt-E-Mail")
                flash(
                    "Beim Versenden der Nachricht ist ein Fehler aufgetreten. Bitte versuche es später erneut.",
                    "error",
                )
            else:
                flash("Vielen Dank! Deine Nachricht wurde erfolgreich versendet.", "success")
                return redirect(url_for("contact"))

    page_meta = build_seo_metadata(
        title="Kontakt",
        description="Schreibe uns für Kooperationen, Presse oder Community-Anfragen.",
        canonical=url_for("contact", _external=True),
    )
    return render_template("contact.html", form_data=form_data, page_meta=page_meta)


@app.route("/impressum")
def legal_impressum():
    page_meta = build_seo_metadata(
        title="Impressum",
        canonical=url_for("legal_impressum", _external=True),
    )
    return render_template("legal_impressum.html", page_meta=page_meta)


@app.route("/datenschutz")
def legal_privacy():
    page_meta = build_seo_metadata(
        title="Datenschutzerklärung",
        canonical=url_for("legal_privacy", _external=True),
    )
    privacy_markdown = load_markdown(PRIVACY_FILE)
    return render_template(
        "legal_privacy.html",
        page_meta=page_meta,
        privacy=privacy_markdown,
    )


@app.errorhandler(404)
def not_found(error):  # pragma: no cover - relies on Flask internals
    page_meta = build_seo_metadata(
        title="Seite nicht gefunden",
        description="Diese Seite existiert nicht oder hat sich in den Tiefen des Alls verirrt.",
        robots="noindex",
    )
    return render_template("404.html", page_meta=page_meta), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8073, debug=True)
