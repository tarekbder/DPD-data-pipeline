"""Pipeline 2 — Export: re-fetch raw data and deliver it in the requested format (Polars)."""
import dataclasses
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from .. import config, formats
from ..errors import PipelineError, parse_failed
from ..fetch import Fetcher, follow, initial_params
from ..models import Schema
from ..transfer import Transfer

CONVERT_TARGETS = ("csv", "tsv", "json", "xlsx", "parquet")
TYPE_WARNING = "Column types could not be kept. Download the original format for the raw data."
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "json": "application/json",
    "geojson": "application/geo+json",
    "xml": "application/xml",
    "zip": "application/zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "parquet": "application/vnd.apache.parquet",
}


@dataclass
class ExportResult:
    content: bytes
    filename: str
    media_type: str
    warning: str | None = None


def _filename(asset_id: str, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", asset_id).strip("._") or "dataset"
    return f"{safe}_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%MZ')}.{ext}"


def _result(transfer, content: bytes, ext: str, warning=None) -> ExportResult:
    return ExportResult(content, _filename(transfer.asset_id, ext), MEDIA_TYPES[ext], warning)


# ----------------------------------------------------------------- loading

def _resolve(first, src):
    """Unzip if needed and re-run detection if the source format changed since explore.

    Returns (content, fmt, delimiter, record_path, row_path, inner_name).
    """
    content, inner = first.content, None
    zipped = formats.is_zip(content)
    if zipped:
        inner, content = formats.open_zip(content, src.zip_inner)

    fmt, delim = src.format, src.delimiter
    record_path, row_path = src.json_record_path, src.xml_row_path
    sniffed, _ = formats.sniff(content)
    changed = zipped != (src.zip_inner is not None) or (sniffed is not None and not formats.same_family(sniffed, fmt))
    if changed:
        fmt, delim = formats.detect(content, "" if zipped else first.content_type, inner or first.url)
        if fmt == "zip":
            raise PipelineError(415, "unsupported_format", "Unsupported data format: nested zip.",
                                {"content_type": "application/zip", "sniffed": "zip"})
        record_path = row_path = None
    if fmt in ("csv", "tsv") and not delim:
        delim = "\t" if fmt == "tsv" else ","
    return content, fmt, delim, record_path, row_path, inner


def _read_delimited(data: bytes, sep: str) -> pl.DataFrame:
    try:
        return pl.read_csv(
            io.BytesIO(data),
            separator=sep,
            infer_schema_length=0,               # all columns as strings
            null_values=None,
            missing_utf8_is_empty_string=True,   # empty cells stay ""
            try_parse_dates=False,
            encoding="utf8-lossy",
        )
    except pl.exceptions.NoDataError:
        return pl.DataFrame()
    except pl.exceptions.PolarsError as e:
        raise parse_failed("tsv" if sep == "\t" else "csv", error=str(e)[:300])


def _to_str(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _string_frame(rows: list) -> pl.DataFrame:
    cols = list(dict.fromkeys(k for r in rows for k in r))
    return pl.DataFrame({c: [_to_str(r.get(c)) for r in rows] for c in cols}, schema={c: pl.Utf8 for c in cols})


def _json_frame(rows: list):
    """Typed read; on failure re-read all-string with a warning."""
    try:
        return pl.DataFrame(rows, infer_schema_length=None), None
    except Exception:
        return _string_frame(rows), TYPE_WARNING


def _json_rows(pages, fmt, record_path) -> list:
    records = [r for p in pages for r in formats.records_from(p.body, fmt, record_path)]
    return formats.geojson_rows(records) if fmt == "geojson" else formats.as_dicts(records)


# ------------------------------------------------------------- transforms

def _flatten_nested(df: pl.DataFrame) -> pl.DataFrame:
    """Structs -> 'parent.child' columns (recursive); lists -> JSON strings."""
    while any(isinstance(t, pl.Struct) for t in df.schema.values()):
        exprs = []
        for name, dtype in df.schema.items():
            if isinstance(dtype, pl.Struct):
                exprs += [pl.col(name).struct.field(f.name).alias(f"{name}.{f.name}") for f in dtype.fields]
            else:
                exprs.append(pl.col(name))
        df = df.select(exprs)

    list_types = tuple(t for t in (pl.List, getattr(pl, "Array", None), pl.Object) if t is not None)
    nested = [n for n, t in df.schema.items() if isinstance(t, list_types)]
    if nested:
        df = df.with_columns([
            pl.col(n).map_elements(_json_cell, return_dtype=pl.Utf8) for n in nested
        ])
    return df


def _json_cell(v) -> str:
    if isinstance(v, pl.Series):
        v = v.to_list()
    return json.dumps(v, ensure_ascii=False, default=str)


def _geometry_to_json(rows: list) -> list:
    return [
        {**r, "geometry": json.dumps(r["geometry"], ensure_ascii=False) if r.get("geometry") is not None else None}
        for r in rows
    ]


def _check_excel(df: pl.DataFrame):
    longest = 0
    for name, dtype in df.schema.items():
        if dtype == pl.Utf8 and df.height:
            longest = max(longest, df.select(pl.col(name).str.len_chars().max()).item() or 0)
    if df.height > config.EXCEL_MAX_ROWS or df.width > config.EXCEL_MAX_COLS or longest > config.EXCEL_MAX_CELL_CHARS:
        raise PipelineError(400, "excel_limit", "Too large for Excel, download as CSV.", {
            "rows": df.height, "max_rows": config.EXCEL_MAX_ROWS,
            "cols": df.width, "max_cols": config.EXCEL_MAX_COLS,
            "longest_cell": longest, "max_cell_chars": config.EXCEL_MAX_CELL_CHARS,
        })


_FORMULA_START = r"^[=+\-@\t\r]"


def _escape_formulas(df: pl.DataFrame) -> pl.DataFrame:
    """Prefix string cells that a spreadsheet would run as a formula with a single quote."""
    exprs = [
        pl.when(pl.col(c).str.contains(_FORMULA_START)).then(pl.lit("'") + pl.col(c)).otherwise(pl.col(c)).alias(c)
        for c, dtype in df.schema.items() if dtype == pl.String
    ]
    return df.with_columns(exprs) if exprs else df


def _write(df: pl.DataFrame, target: str) -> bytes:
    buf = io.BytesIO()
    if target == "csv":
        df.write_csv(buf)
    elif target == "tsv":
        df.write_csv(buf, separator="\t")
    elif target == "json":
        buf.write(json.dumps(df.to_dicts(), ensure_ascii=False, default=str).encode("utf-8"))
    elif target == "xlsx":
        df.write_excel(buf)  # uses xlsxwriter
    elif target == "parquet":
        df.write_parquet(buf)
    return buf.getvalue()


# ------------------------------------------------------------ route A

def _concat_original(fmt, pages, contents, record_path, row_path) -> bytes:
    if fmt in ("csv", "tsv"):
        return formats.concat_delimited(contents)
    if fmt == "json":
        records = [r for p in pages for r in formats.records_from(p.body, fmt, record_path)]
        return json.dumps(records, ensure_ascii=False).encode("utf-8")
    if fmt == "geojson":
        base = dict(pages[0].body)
        base["features"] = [f for p in pages for f in formats.records_from(p.body, fmt, None)]
        return json.dumps(base, ensure_ascii=False).encode("utf-8")
    return formats.concat_xml(contents, row_path)


# --------------------------------------------------------------------- main

def export(transfer: Transfer, target: str, schema: Schema) -> ExportResult:
    src = schema.source
    pagination = None if src.zip_inner is not None else src.pagination

    fetcher = Fetcher(transfer)
    first = fetcher.get(transfer.endpoint, params=initial_params(pagination))
    content, fmt, delim, record_path, row_path, inner = _resolve(first, src)
    zipped = inner is not None

    allowed = set(CONVERT_TARGETS) | {fmt} | ({"zip"} if zipped else set())
    if target not in allowed:
        raise PipelineError(400, "invalid_schema", "Download request is invalid. Re-run explore.",
                            {"format": target, "allowed": sorted(allowed)})

    # Route A: original zip, unchanged
    if zipped and target == "zip":
        return _result(transfer, first.content, "zip")

    if zipped:
        pagination = None
        first = dataclasses.replace(first, content=content, link_next=None)
    if fmt in formats.JSON_FORMATS:
        first.body = formats.parse_json(content)
        if fmt == "json" and record_path is None:
            record_path = formats.find_record_path(first.body)
    if fmt == "xml" and row_path is None:
        row_path = formats.detect_row_path(formats.parse_xml(content))

    pages = follow(fetcher, first, pagination, fmt, record_path, strict=True).pages
    contents = [p.content for p in pages]

    # Route A: original format
    if target == fmt:
        data = contents[0] if len(pages) == 1 else _concat_original(fmt, pages, contents, record_path, row_path)
        return _result(transfer, data, fmt)

    # Route B: convert
    warning = None
    if fmt in formats.JSON_FORMATS:
        rows = _json_rows(pages, fmt, record_path)
        if target == "json":
            return _result(transfer, json.dumps(rows, ensure_ascii=False).encode("utf-8"), "json")
        if fmt == "geojson":
            rows = _geometry_to_json(rows)
        df, warning = _json_frame(rows)
    elif fmt in ("csv", "tsv"):
        df = _read_delimited(formats.concat_delimited(contents), delim)
    else:  # xml
        trees = [formats.parse_xml(c) for c in contents]
        df = _string_frame([formats.flatten_xml(el) for t in trees for el in formats.xml_records(t, row_path)])

    if target in ("csv", "tsv", "xlsx"):
        df = _flatten_nested(df)
        df = _escape_formulas(df)
    if target == "xlsx":
        _check_excel(df)

    data = _write(df, target)
    del df, pages, contents
    return _result(transfer, data, target, warning)
