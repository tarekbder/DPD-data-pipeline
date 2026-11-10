"""HTTP fetch with guards, and pagination following."""
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import requests

from . import config, formats, policy
from .errors import PipelineError, too_large, upstream_timeout
from .models import BodyNext, LinkHeader
from .transfer import Transfer

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TOTAL_KEYS = ("total", "total_count", "totalCount", "count", "total_results", "totalResults")
_PAGES_KEYS = ("total_pages", "totalPages", "pages", "page_count", "pageCount")
_META_KEYS = ("meta", "pagination", "page_info", "pageInfo", "paging")


@dataclass
class Page:
    url: str
    content: bytes
    content_type: str
    link_next: str | None = None
    body: object = field(default=None, repr=False)  # parsed JSON for json/geojson


@dataclass
class FetchResult:
    pages: list
    partial: bool = False


def _origin(url: str):
    p = urlsplit(url)
    scheme = p.scheme.lower()
    return scheme, (p.hostname or "").lower(), p.port or _DEFAULT_PORTS.get(scheme)


def _full_url(url: str, params) -> str:
    return requests.Request("GET", url, params=params).prepare().url


class Fetcher:
    """One fetch = all pages. Enforces MAX_PAGES, MAX_RESPONSE_MB, REQUEST_TIMEOUT_S and FETCH_DEADLINE_S."""

    def __init__(self, transfer: Transfer):
        self.transfer = transfer
        self.session = policy.pinned_session()
        self.start = time.monotonic()
        self.bytes = 0
        self.pages = 0

    def _elapsed(self) -> float:
        return time.monotonic() - self.start

    def get(self, url: str, params=None) -> Page:
        if self.pages >= config.MAX_PAGES:
            raise too_large(pages=self.pages, max_pages=config.MAX_PAGES)
        remaining = config.FETCH_DEADLINE_S - self._elapsed()
        if remaining <= 0:
            raise upstream_timeout(timeout="fetch_deadline", elapsed_s=round(self._elapsed(), 1))
        policy.check_url(url, self.transfer.provider_host)
        headers = {**self.transfer.headers(), "Accept-Encoding": "identity"}
        try:
            r = self.session.get(url, params=params, headers=headers, allow_redirects=False,
                                 timeout=min(config.REQUEST_TIMEOUT_S, remaining), stream=True)
        except requests.Timeout:
            raise upstream_timeout(timeout="request_timeout", elapsed_s=round(self._elapsed(), 1))
        except requests.RequestException as e:
            raise PipelineError(502, "upstream_error", "Data provider not reachable. Try again.",
                                {"error": type(e).__name__})
        with r:
            if r.status_code in (401, 403):
                raise PipelineError(r.status_code, "transfer_revoked", "Access expired. Start a new transfer.",
                                    {"upstream_status": r.status_code})
            if r.status_code == 404:
                raise PipelineError(502, "not_found_upstream", "Dataset not found at provider.",
                                    {"upstream_status": 404})
            if 300 <= r.status_code < 400:
                raise PipelineError(502, "upstream_error", "Data provider not reachable. Try again.",
                                    {"upstream_status": r.status_code, "reason": "redirect_blocked"})
            if not 200 <= r.status_code < 300:
                raise PipelineError(502, "upstream_error", "Data provider not reachable. Try again.",
                                    {"upstream_status": r.status_code})
            encoding = r.headers.get("Content-Encoding", "").strip().lower()
            if encoding not in ("", "identity"):
                raise PipelineError(415, "unsupported_format", "Unsupported data format: compressed response.",
                                    {"content_encoding": encoding})
            chunks = []
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    self.bytes += len(chunk)
                    if self.bytes > config.MAX_RESPONSE_BYTES:
                        raise too_large(mb_fetched=round(self.bytes / 1048576, 1), max_mb=config.MAX_RESPONSE_MB)
                    if self._elapsed() > config.FETCH_DEADLINE_S:
                        raise upstream_timeout(timeout="fetch_deadline", elapsed_s=round(self._elapsed(), 1))
                    chunks.append(chunk)
            except requests.Timeout:
                raise upstream_timeout(timeout="request_timeout", elapsed_s=round(self._elapsed(), 1))
            except requests.RequestException as e:
                raise PipelineError(502, "upstream_error", "Data provider not reachable. Try again.",
                                    {"error": type(e).__name__})
            nxt = r.links.get("next", {}).get("url")
            self.pages += 1
            return Page(
                url=r.url,
                content=b"".join(chunks),
                content_type=r.headers.get("Content-Type", ""),
                link_next=urljoin(r.url, nxt) if nxt else None,
            )


# --------------------------------------------------------------- pagination

def _get_path(body, path: str):
    cur = body
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _next_link(body, path: str):
    value = _get_path(body, path)
    if isinstance(value, dict):
        value = value.get("href")
    return value.strip() if isinstance(value, str) and value.strip() else None


def initial_params(pagination):
    if pagination is None:
        return None
    if pagination.type == "offset":
        return {pagination.param: 0, pagination.limit_param: pagination.page_size}
    if pagination.type == "page":
        return {pagination.param: pagination.start}
    return None


def auto_pagination(first: Page, fmt: str):
    """Only body_next and link_header are auto-detected."""
    if fmt in formats.JSON_FORMATS and isinstance(first.body, dict):
        for path in ("next", "links.next"):
            if _next_link(first.body, path):
                return BodyNext(type="body_next", next_path=path)
    if first.link_next:
        return LinkHeader(type="link_header")
    return None


def _count(page: Page, fmt: str, record_path):
    """Records on a page; None when it can't be counted (XML)."""
    if fmt in formats.JSON_FORMATS:
        try:
            return len(formats.records_from(page.body, fmt, record_path))
        except PipelineError:
            return 0
    if fmt in ("csv", "tsv"):
        lines = [line for line in page.content.splitlines() if line.strip()]
        return max(len(lines) - 1, 0)
    return None


def _more_hint(body, records: int, pages: int, record_path=None) -> bool:
    """Response fields like total/count/pages suggest more data than was fetched.

    Looked up at the top level, in meta-like objects, and next to the records (e.g. CKAN result.total).
    """
    if not isinstance(body, dict):
        return False
    scopes = [body] + [body[k] for k in _META_KEYS if isinstance(body.get(k), dict)]
    if record_path and "." in record_path:
        parent = _get_path(body, record_path.rsplit(".", 1)[0])
        if isinstance(parent, dict):
            scopes.append(parent)
    for scope in scopes:
        for key in _TOTAL_KEYS:
            v = scope.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > records:
                return True
        for key in _PAGES_KEYS:
            v = scope.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > pages:
                return True
    return False


def follow(fetcher: Fetcher, first: Page, pagination, fmt: str, record_path, strict: bool) -> FetchResult:
    """Follow pages after `first`.

    strict=False (explore): guards stop the loop and set partial.
    strict=True (export): guards raise 413/504.
    """
    pages = [first]
    records = _count(first, fmt, record_path) or 0
    if pagination is None:
        return FetchResult(pages, partial=not strict and _more_hint(first.body, records, 1, record_path))

    endpoint = fetcher.transfer.endpoint
    origin = _origin(endpoint)
    seen = {_full_url(first.url, None)}
    counter = pagination.start if pagination.type == "page" else 0
    partial = False

    while True:
        last = pages[-1]
        url, params = endpoint, None
        if pagination.type == "body_next":
            nxt = _next_link(last.body, pagination.next_path)
            if not nxt:
                break
            url = urljoin(last.url, nxt)
        elif pagination.type == "link_header":
            if not last.link_next:
                break
            url = last.link_next
        elif pagination.type == "offset":
            n = _count(last, fmt, record_path)
            if not n or n < pagination.page_size:
                break
            counter += pagination.page_size
            params = {pagination.param: counter, pagination.limit_param: pagination.page_size}
        else:  # page
            if not _count(last, fmt, record_path):
                break
            counter += 1
            params = {pagination.param: counter}

        full = _full_url(url, params)
        reason = None
        if _origin(full) != origin:
            reason = "next_link_other_origin"  # never send the token elsewhere
        elif full in seen:
            reason = "next_link_repeated"
        if reason:
            if strict:
                raise too_large(reason=reason, pages=len(pages))
            partial = True
            break
        seen.add(full)

        try:
            page = fetcher.get(url, params)
        except PipelineError as e:
            if strict or e.code not in ("too_large", "upstream_timeout"):
                raise
            partial = True
            break
        if fmt in formats.JSON_FORMATS:
            page.body = formats.parse_json(page.content)
        pages.append(page)
        records += _count(page, fmt, record_path) or 0

    if not partial and not strict and pagination.type in ("body_next", "link_header"):
        partial = _more_hint(pages[-1].body, records, len(pages), record_path)
    return FetchResult(pages, partial)
