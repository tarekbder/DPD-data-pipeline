"""Format detection and format-specific helpers (standard library; lxml imported only for XML)."""
import csv
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import config
from .errors import PipelineError, parse_failed, too_large

SNIFF_BYTES = 8192
JSON_FORMATS = ("json", "geojson")
PREFERRED_RECORD_KEYS = ("data", "results", "items", "records")

CONTENT_TYPES = {
    "text/csv": "csv",
    "application/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/json": "json",
    "text/json": "json",
    "application/geo+json": "geojson",
    "application/vnd.geo+json": "geojson",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
}
EXTENSIONS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".json": "json",
    ".geojson": "geojson",
    ".xml": "xml",
    ".zip": "zip",
}


# ---------------------------------------------------------------- detection

def _base_type(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def declared_format(content_type: str, url_or_name: str):
    fmt = CONTENT_TYPES.get(_base_type(content_type))
    if fmt:
        return fmt
    suffix = PurePosixPath(urlsplit(url_or_name or "").path).suffix.lower()
    return EXTENSIONS.get(suffix)


def is_zip(content: bytes) -> bool:
    return content[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def sniff(content: bytes):
    """(format, delimiter) from the first few KB, or (None, None)."""
    if is_zip(content):
        return "zip", None
    head = content[:SNIFF_BYTES]
    text = head.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")
    if not text:
        return None, None
    if text[0] == "<":
        return "xml", None
    if text[0] in "{[":
        if re.search(r'"type"\s*:\s*"FeatureCollection"', text):
            return "geojson", None
        return "json", None
    lines = text.splitlines()
    if len(content) > SNIFF_BYTES and len(lines) > 1:
        lines = lines[:-1]  # last line may be cut off
    try:
        delim = csv.Sniffer().sniff("\n".join(lines[:50]), delimiters=",;\t|").delimiter
    except csv.Error:
        return None, None
    return ("tsv" if delim == "\t" else "csv"), delim


def detect(content: bytes, content_type: str, url_or_name: str):
    """Content-Type, then extension, then sniffing; a clear sniff result wins."""
    declared = declared_format(content_type, url_or_name)
    sniffed, delim = sniff(content)
    if sniffed is None:
        if declared is None:
            ct = _base_type(content_type) or "unknown"
            raise PipelineError(415, "unsupported_format", f"Unsupported data format: {ct}.",
                                {"content_type": ct, "sniffed": None})
        fmt = declared
    elif declared == "geojson" and sniffed == "json":
        fmt = "geojson"
    else:
        fmt = sniffed
    if fmt in ("csv", "tsv") and delim is None:
        delim = "\t" if fmt == "tsv" else ","
    return fmt, delim


def same_family(a: str, b: str) -> bool:
    families = ({"csv", "tsv"}, {"json", "geojson"}, {"xml"}, {"zip"})
    return any(a in f and b in f for f in families)


# ---------------------------------------------------------------------- zip

def open_zip(content: bytes, preferred: str | None = None):
    """(inner filename, inner bytes). Uses `preferred` if present, else the largest known data file."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise parse_failed("zip", error=type(e).__name__)
    members = [
        i for i in zf.infolist()
        if not i.is_dir()
        and not i.filename.startswith("__MACOSX/")
        and not PurePosixPath(i.filename).name.startswith(".")
    ]
    pick = None
    if preferred:
        pick = next((i for i in members if PurePosixPath(i.filename).name == preferred), None)
    if pick is None:
        known = [i for i in members if EXTENSIONS.get(PurePosixPath(i.filename).suffix.lower()) not in (None, "zip")]
        candidates = known or members
        if not candidates:
            raise parse_failed("zip", reason="no data file in zip")
        pick = max(candidates, key=lambda i: i.file_size)
    if pick.file_size > config.MAX_RESPONSE_BYTES:
        raise too_large(mb_unzipped=round(pick.file_size / 1048576, 1), max_mb=config.MAX_RESPONSE_MB)
    return PurePosixPath(pick.filename).name, zf.read(pick)


# --------------------------------------------------------------------- json

def parse_json(content: bytes):
    try:
        return json.loads(content.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise parse_failed("json", error=type(e).__name__)
    except json.JSONDecodeError as e:
        raise parse_failed("json", error=e.msg, line=e.lineno, column=e.colno)


def _is_record_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, dict) for x in value[:50])


def find_record_path(body) -> str:
    """'' for a top-level list, else the key holding the list of objects (one dotted level allowed)."""
    if isinstance(body, list):
        return ""
    if not isinstance(body, dict):
        raise parse_failed("json", reason="no list of records found")
    candidates = []  # (path, depth, length)
    for k, v in body.items():
        if _is_record_list(v):
            candidates.append((k, 0, len(v)))
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if _is_record_list(v2):
                    candidates.append((f"{k}.{k2}", 1, len(v2)))
    if not candidates:
        raise parse_failed("json", reason="no list of records found")

    def rank(c):
        last = c[0].rsplit(".", 1)[-1]
        pref = PREFERRED_RECORD_KEYS.index(last) if last in PREFERRED_RECORD_KEYS else len(PREFERRED_RECORD_KEYS)
        return (pref, -c[2], c[1])

    return min(candidates, key=rank)[0]


def records_from(body, fmt: str, record_path: str | None) -> list:
    if fmt == "geojson":
        feats = body.get("features") if isinstance(body, dict) else None
        if not isinstance(feats, list):
            raise parse_failed("geojson", reason="no features array")
        return feats
    cur = body
    for key in (record_path or "").split(".") if record_path else []:
        if not isinstance(cur, dict) or key not in cur:
            raise parse_failed("json", reason="json_record_path not found", json_record_path=record_path)
        cur = cur[key]
    if not isinstance(cur, list):
        raise parse_failed("json", reason="records are not a list", json_record_path=record_path)
    return cur


def as_dicts(records: list) -> list:
    return [r if isinstance(r, dict) else {"value": r} for r in records]


def geojson_rows(features: list) -> list:
    """One row per feature: properties plus the geometry object."""
    rows = []
    for f in features:
        if not isinstance(f, dict):
            continue
        props = f.get("properties")
        row = dict(props) if isinstance(props, dict) else {}
        row["geometry"] = f.get("geometry")
        rows.append(row)
    return rows


# ------------------------------------------------------------- csv / tsv

def concat_delimited(pages: list) -> bytes:
    """Header from page 1, data rows from all pages."""
    if len(pages) == 1:
        return pages[0]
    parts = [pages[0].rstrip(b"\r\n")]
    for p in pages[1:]:
        rows = p.split(b"\n", 1)[1] if b"\n" in p else b""
        rows = rows.rstrip(b"\r\n")
        if rows.strip():
            parts.append(rows)
    return b"\n".join(parts) + b"\n"


# ---------------------------------------------------------------------- xml

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_xml(content: bytes):
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True,
                             remove_comments=True, remove_pis=True)
    try:
        return etree.fromstring(content, parser).getroottree()
    except etree.XMLSyntaxError as e:
        raise parse_failed("xml", error=getattr(e, "msg", "syntax error"), line=getattr(e, "lineno", None))


def detect_row_path(tree) -> str:
    """Slash path of local names to the repeated record element, e.g. 'root/items/item'."""
    root = tree.getroot()
    counts, structured, depth = Counter(), set(), {}

    def walk(el, path, d):
        for child in el:
            if not isinstance(child.tag, str):
                continue
            p = f"{path}/{_local(child.tag)}"
            counts[p] += 1
            depth[p] = d
            if child.attrib or any(isinstance(c.tag, str) for c in child):
                structured.add(p)
            walk(child, p, d + 1)

    root_path = _local(root.tag)
    walk(root, root_path, 1)
    if not counts:
        return root_path
    return max(counts, key=lambda p: (counts[p] > 1 and p in structured, counts[p], -depth[p]))


def xml_records(tree, row_path: str) -> list:
    parts = row_path.split("/")
    root = tree.getroot()
    if _local(root.tag) != parts[0]:
        raise parse_failed("xml", reason="xml_row_path not found", xml_row_path=row_path)
    level = [root]
    for name in parts[1:]:
        level = [c for el in level for c in el if _local(c.tag) == name]
    return level


def flatten_xml(el) -> dict:
    """Generic flattening: 'child.grandchild' keys, '@attr' for attributes, '[n]' for repeated tags. All strings."""
    row = {}

    def walk(e, prefix):
        for k, v in e.attrib.items():
            row[f"{prefix}@{_local(k)}" if prefix else f"@{_local(k)}"] = v
        children = [c for c in e if isinstance(c.tag, str)]
        if not children:
            text = (e.text or "").strip()
            row[prefix or "#text"] = text
            return
        totals, seen = Counter(_local(c.tag) for c in children), Counter()
        for c in children:
            name = _local(c.tag)
            if totals[name] > 1:
                seen[name] += 1
                name = f"{name}[{seen[name]}]"
            walk(c, f"{prefix}.{name}" if prefix else name)

    walk(el, "")
    return row


def concat_xml(pages: list, row_path: str | None) -> bytes:
    """Root/header from page 1; record elements from later pages inserted after page 1's records."""
    from lxml import etree

    base = parse_xml(pages[0])
    row_path = row_path or detect_row_path(base)
    records = xml_records(base, row_path)
    if records:
        parent, index = records[-1].getparent(), records[-1].getparent().index(records[-1]) + 1
    else:
        parents = xml_records(base, row_path.rsplit("/", 1)[0]) if "/" in row_path else []
        parent = parents[0] if parents else base.getroot()
        index = len(parent)
    if parent is None:  # row path is the root itself; nothing to merge into
        return pages[0]
    for content in pages[1:]:
        for rec in xml_records(parse_xml(content), row_path):
            parent.insert(index, rec)
            index += 1
    return etree.tostring(base, xml_declaration=True, encoding=base.docinfo.encoding or "UTF-8")
