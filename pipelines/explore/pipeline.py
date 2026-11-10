"""Pipeline 1 — Explore: fetch, normalize and summarize a dataset as JSON (pandas)."""
import dataclasses
import gc
import io
import json
import math
import re
import warnings
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .. import config, formats
from ..errors import PipelineError, parse_failed
from ..fetch import Fetcher, auto_pagination, follow, initial_params
from ..models import load_overrides
from ..transfer import Transfer

LEADING_ZERO = re.compile(r"^0\d+(\.\d+)?$")
TYPE_SAMPLE = 1000


# ------------------------------------------------------------------ helpers

def jsonable(v):
    """NaN/NaT/inf -> None, numpy -> python, timestamps -> ISO strings."""
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, np.ndarray):
        return [jsonable(x) for x in v.tolist()]
    if isinstance(v, np.datetime64):
        v = pd.Timestamp(v)
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, pd.Timedelta):
        return None if pd.isna(v) else str(v)
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _is_text(s: pd.Series) -> bool:
    """Object or string dtype (pandas 3 reads strings as 'str', not object)."""
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _is_nested(v) -> bool:
    return isinstance(v, (dict, list))


def _strip(v):
    return v.strip() if isinstance(v, str) else v


def _note() -> str:
    return ("Computed after normalizing empty strings, whitespace and placeholders "
            f"({', '.join(config.PLACEHOLDERS_RAW)}) to NaN.")


# ------------------------------------------------------------------ loading

def _read_delimited(data: bytes, sep: str) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), sep=sep, dtype=str, keep_default_na=False, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except pd.errors.ParserError as e:
            raise parse_failed("csv" if sep != "\t" else "tsv", error=str(e)[:300])
    raise parse_failed("csv", error="encoding")


def _geo_frame(rows: list) -> pd.DataFrame:
    props = pd.json_normalize([{k: v for k, v in r.items() if k != "geometry"} for r in rows])
    props["geometry"] = pd.Series([r.get("geometry") for r in rows], dtype=object)
    return props


def _load(fmt, delim, pages, record_path):
    """Returns (DataFrame, xml_row_path)."""
    if fmt in ("csv", "tsv"):
        return _read_delimited(formats.concat_delimited([p.content for p in pages]), delim), None
    if fmt == "json":
        records = [r for p in pages for r in formats.records_from(p.body, fmt, record_path)]
        return pd.json_normalize(formats.as_dicts(records)), None
    if fmt == "geojson":
        features = [f for p in pages for f in formats.records_from(p.body, fmt, None)]
        return _geo_frame(formats.geojson_rows(features)), None
    # xml: all values are strings
    trees = [formats.parse_xml(p.content) for p in pages]
    row_path = formats.detect_row_path(trees[0])
    rows = [formats.flatten_xml(el) for t in trees for el in formats.xml_records(t, row_path)]
    return pd.DataFrame(rows, dtype=object), row_path


# ----------------------------------------------------------------- analysis

def _leading_zeros(s: pd.Series) -> bool:
    if not _is_text(s):
        return False
    return any(isinstance(v, str) and LEADING_ZERO.match(v.strip()) for v in s.dropna())


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Empty strings, whitespace-only strings and PLACEHOLDERS -> NaN."""
    def fix(v):
        if isinstance(v, str):
            t = v.strip()
            if not t or t.lower() in config.PLACEHOLDERS:
                return np.nan
        return v

    for c in df.columns:
        if _is_text(df[c]):
            df[c] = df[c].astype(object).map(fix)
    return df


def _all_bool(s: pd.Series) -> bool:
    vals = s.dropna()
    return len(vals) > 0 and all(isinstance(v, (bool, np.bool_)) for v in vals)


def _to_datetime(s: pd.Series) -> pd.Series:
    s = s.map(lambda v: v if isinstance(v, str) else None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return pd.to_datetime(s, errors="coerce", utc=True, format="mixed")
        except (TypeError, ValueError):
            return pd.to_datetime(s, errors="coerce", utc=True)


def _passes(parsed: pd.Series, nonnull: int) -> bool:
    return nonnull > 0 and parsed.notna().sum() / nonnull >= config.TYPE_PARSE_THRESHOLD


def _type_column(s: pd.Series, leading_zeros: bool):
    """(type, converted series, coerced count)."""
    nonnull = int(s.notna().sum())
    if nonnull == 0:  # no values left: nothing to type
        return "categorical", s.astype(object), 0
    if pd.api.types.is_bool_dtype(s) or _all_bool(s):
        return "categorical", s, 0
    if pd.api.types.is_numeric_dtype(s):
        return "numeric", s.replace([np.inf, -np.inf], np.nan), 0
    if leading_zeros:
        return "categorical", s, 0

    stripped = s.map(_strip)
    sample = stripped.dropna().head(TYPE_SAMPLE)

    if _passes(pd.to_numeric(sample, errors="coerce"), len(sample)):
        num = pd.to_numeric(stripped, errors="coerce")
        if _passes(num, nonnull):
            return "numeric", num.replace([np.inf, -np.inf], np.nan), nonnull - int(num.notna().sum())

    if _passes(_to_datetime(sample), len(sample)):
        dt = _to_datetime(stripped)
        if _passes(dt, nonnull):
            return "date", dt, nonnull - int(dt.notna().sum())

    return "categorical", s, 0


def _ranges(df: pd.DataFrame, types: dict) -> dict:
    out = {"numeric": {}, "categorical": {}, "date": {}}
    for c, t in types.items():
        s = df[c].dropna()
        if t == "numeric":
            if s.empty:
                out["numeric"][c] = {"min": None, "q1": None, "median": None, "q3": None, "max": None}
                continue
            q = s.quantile([0.25, 0.5, 0.75])
            out["numeric"][c] = {"min": s.min(), "q1": q[0.25], "median": q[0.5], "q3": q[0.75], "max": s.max()}
        elif t == "date":
            out["date"][c] = {"min": s.min() if not s.empty else None, "max": s.max() if not s.empty else None}
        else:
            counts = s.map(str).value_counts()
            top = counts.head(config.TOP_N)
            out["categorical"][c] = {
                "top": [{"label": label, "count": int(n)} for label, n in top.items()],
                "other_count": int(counts.sum() - top.sum()),
            }
    return out


def _cell_len(v) -> int:
    if isinstance(v, str):
        return len(v)
    if _is_nested(v):
        return len(json.dumps(v, default=str))
    return 0


def _excel_allowed(df: pd.DataFrame) -> bool:
    if len(df) > config.EXCEL_MAX_ROWS or len(df.columns) > config.EXCEL_MAX_COLS:
        return False
    for c in df.columns:
        if _is_text(df[c]) and len(df) and df[c].map(_cell_len).max() > config.EXCEL_MAX_CELL_CHARS:
            return False
    return True


def _describe(df: pd.DataFrame) -> dict:
    if df.empty or not len(df.columns):
        return {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        desc = df.describe(include="all")
    return {
        str(c): {str(k): jsonable(v) for k, v in desc[c].items() if jsonable(v) is not None}
        for c in desc.columns
    }


# --------------------------------------------------------------------- main

def explore(transfer: Transfer) -> dict:
    override = load_overrides().get(transfer.asset_id)
    pagination = override.pagination if override else None

    # 1-3. fetch, detect, locate records, paginate
    fetcher = Fetcher(transfer)
    first = fetcher.get(transfer.endpoint, params=initial_params(pagination))
    fmt, delim = formats.detect(first.content, first.content_type, first.url)

    container = zip_inner = None
    if fmt == "zip":
        container = "zip"
        zip_inner, inner = formats.open_zip(first.content)
        fmt, delim = formats.detect(inner, "", zip_inner)
        if fmt == "zip":
            raise PipelineError(415, "unsupported_format", "Unsupported data format: nested zip.",
                                {"content_type": "application/zip", "sniffed": "zip"})
        first = dataclasses.replace(first, content=inner, link_next=None)
        pagination = None

    record_path = None
    if fmt in formats.JSON_FORMATS:
        first.body = formats.parse_json(first.content)
        if fmt == "json" and isinstance(first.body, dict) and first.body.get("type") == "FeatureCollection":
            fmt = "geojson"
        if fmt == "json":
            if override and override.json_record_path is not None:
                record_path = override.json_record_path
            else:
                record_path = formats.find_record_path(first.body)

    if container is None and pagination is None:
        pagination = auto_pagination(first, fmt)
    result = follow(fetcher, first, pagination, fmt, record_path, strict=False)

    # 4. load raw
    df, row_path = _load(fmt, delim, result.pages, record_path)
    df.columns = [str(c) for c in df.columns]

    # 5. exclude nested columns
    nested = [c for c in df.columns if _is_text(df[c]) and df[c].map(_is_nested).any()]
    analysis_cols = [c for c in df.columns if c not in nested]

    # 6. leading zeros (before normalization)
    lz = {c: _leading_zeros(df[c]) for c in analysis_cols}

    # 7. health
    df = _normalize(df)
    excel_allowed = _excel_allowed(df)
    total_cells = len(df) * len(analysis_cols)
    missing_cells = int(df[analysis_cols].isna().sum().sum()) if analysis_cols else 0

    # 8. type for analysis
    types, coerced = {}, {}
    for c in analysis_cols:
        types[c], df[c], coerced[c] = _type_column(df[c], lz[c])

    # 9-10. explore and ranges
    analyzed = df[analysis_cols]
    columns = [
        {
            "column": c,
            "dtype": str(analyzed[c].dtype),
            "type": types[c],
            "leading_zeros": lz[c],
            "coerced": int(coerced[c]),
            "non_null": int(analyzed[c].notna().sum()),
            "null_pct": round(float(analyzed[c].isna().mean() * 100), 2) if len(analyzed) else 0.0,
        }
        for c in analysis_cols
    ]

    # 12. build JSON
    out = {
        "meta": {
            "asset_id": transfer.asset_id,
            "format": fmt,
            "container": container,
            "rows": int(len(df)),
            "cols": int(len(df.columns)),
            "pages": len(result.pages),
            "partial_warning": result.partial,
            "excel_allowed": excel_allowed,
            "excluded_columns": nested,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "health": {
            "missing_pct": round(missing_cells / total_cells * 100, 2) if total_cells else 0.0,
            "missing_cells": missing_cells,
            "total_cells": total_cells,
        },
        "head": df.head(5).to_dict(orient="records"),
        "info": {"note": _note(), "columns": columns},
        "describe": {"note": _note(), "stats": _describe(analyzed)},
        "ranges": _ranges(analyzed, types),
        "schema": {
            "source": {
                "format": fmt,
                "delimiter": delim if fmt in ("csv", "tsv") else None,
                "zip_inner": zip_inner,
                "xml_row_path": row_path,
                "json_record_path": record_path,
                "geojson": fmt == "geojson",
                "pagination": pagination.model_dump() if pagination is not None else None,
            }
        },
    }
    out = jsonable(out)

    # 13. flush
    del df, analyzed, result, first
    gc.collect()
    return out
