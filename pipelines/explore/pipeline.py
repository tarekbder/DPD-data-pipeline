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


def _raw_note(fmt: str) -> str:
    note = "Computed on the values as read from the source: no placeholders normalized, no types converted."
    if fmt in ("csv", "tsv", "xml"):
        note += " All columns are read as strings."
    return note


def preview_rows(n_rows: int) -> int:
    """Rows sent for head and for tail: 50, or 20 when the dataset has fewer than 100 rows."""
    return 50 if n_rows >= 100 else 20


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


def _raw_type(s: pd.Series) -> str:
    """Native types only: no parsing of strings."""
    if pd.api.types.is_bool_dtype(s) or _all_bool(s):
        return "categorical"
    if pd.api.types.is_numeric_dtype(s):
        return "numeric"
    return "categorical"


def _info_columns(frame: pd.DataFrame, types: dict, lz: dict, coerced: dict) -> list:
    return [
        {
            "column": c,
            "dtype": str(frame[c].dtype),
            "type": types[c],
            "leading_zeros": lz[c],
            "coerced": int(coerced[c]),
            "non_null": int(frame[c].notna().sum()),
            "null_pct": round(float(frame[c].isna().mean() * 100), 2) if len(frame) else 0.0,
        }
        for c in types
    ]


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


# ---------------------------------------------------------------------- geo

# Column names (lowercased, separators removed) that hold latitude / longitude.
LAT_NAMES = ("latitude", "lat", "gpslat", "gpslatitude", "ylat", "latdeg", "latwgs84", "wgs84lat")
LON_NAMES = ("longitude", "lon", "lng", "long", "gpslon", "gpslng", "gpslongitude", "xlon", "londeg", "lonwgs84", "wgs84lon")
LAT_SUFFIX = ("latitude", "lat")
LON_SUFFIX = ("longitude", "lon", "lng")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _leaf(name: str) -> str:
    """Last part of a flattened name: 'position.lat' -> 'lat', 'gps_lon' -> 'lon'."""
    return _key(re.split(r"[._\-\s/]", name.strip())[-1]) if name.strip() else ""


def _find_coord(cols: list, exact: tuple, suffix: tuple):
    """Best-named column: exact name first, then a name whose last part matches."""
    for want in exact:
        for c in cols:
            if _key(c) == want:
                return c
    for c in cols:
        if _leaf(c) in suffix:
            return c
    return None


def _in_range(s: pd.Series, limit: float) -> bool:
    vals = s.dropna()
    return len(vals) > 0 and bool(((vals >= -limit) & (vals <= limit)).mean() >= config.TYPE_PARSE_THRESHOLD)


def _cells(lat: np.ndarray, lon: np.ndarray, d: float) -> np.ndarray:
    """Grid cell id per point for square cells of d degrees."""
    ky = np.floor((lat - lat.min()) / d).astype(np.int64)
    kx = np.floor((lon - lon.min()) / d).astype(np.int64)
    return ky * (int(kx.max()) + 1) + kx


def _grid_size(lat: np.ndarray, lon: np.ndarray, limit: int) -> float:
    """Smallest cell size (degrees) that leaves at most `limit` occupied cells."""
    span = max(float(lat.max() - lat.min()), float(lon.max() - lon.min()), 1e-9)
    lo, hi = span / 1e5, span
    if len(np.unique(_cells(lat, lon, lo))) <= limit:
        return lo
    for _ in range(30):
        mid = (lo * hi) ** 0.5
        if len(np.unique(_cells(lat, lon, mid))) <= limit:
            hi = mid
        else:
            lo = mid
    return hi


def _sample(lat: pd.Series, lon: pd.Series):
    """Valid (lat, lon) points, grid-thinned to MAP_MAX_POINTS.

    Above the cap, a grid of square cells is laid over the points and each occupied
    cell keeps its first row, with `count` = rows in that cell. Every area with data
    is shown; density is carried by `count`. Returns (points, total, cell_deg).
    """
    # Points within 1 degree of 0,0 ("null island", open sea) are placeholders, not real locations.
    ok = lat.notna() & lon.notna() & lat.between(-90, 90) & lon.between(-180, 180) & ~((lat.abs() < 1) & (lon.abs() < 1))
    lat, lon = lat[ok], lon[ok]
    total = int(len(lat))
    rows = np.asarray(lat.index)
    la, lo = lat.to_numpy(dtype=float), lon.to_numpy(dtype=float)
    if total <= config.MAP_MAX_POINTS:
        return [[round(float(a), 7), round(float(b), 7), int(r), 1] for a, b, r in zip(la, lo, rows)], total, None

    d = _grid_size(la, lo, config.MAP_MAX_POINTS)
    _, first, counts = np.unique(_cells(la, lo, d), return_index=True, return_counts=True)
    order = np.argsort(first)
    first, counts = first[order], counts[order]
    points = [[round(float(la[i]), 7), round(float(lo[i]), 7), int(rows[i]), int(n)] for i, n in zip(first, counts)]
    return points, total, d


def _regions(points: list, total: int):
    """City + country per point (offline GeoNames lookup), weighted by each point's row count.

    Returns {"items": [...top MAP_MAX_REGIONS...], "other_count": rows in the rest}, or None when
    the lookup library isn't installed or fails: regions are optional and never break explore.
    """
    try:
        import reverse_geocoder as rg
        hits = rg.search([(p[0], p[1]) for p in points], mode=1, verbose=False)
    except Exception:
        return None
    tally = {}
    for p, h in zip(points, hits):
        key = (h.get("name") or None, h.get("admin1") or None, h.get("cc") or None)
        tally[key] = tally.get(key, 0) + p[3]
    ranked = sorted(tally.items(), key=lambda kv: -kv[1])
    top = ranked[:config.MAP_MAX_REGIONS]
    return {
        "items": [
            {"city": city, "admin1": admin1, "country_code": cc, "count": int(n), "pct": round(n / total * 100, 1)}
            for (city, admin1, cc), n in top
        ],
        "other_count": int(sum(n for _, n in ranked[len(top):])),
    }


def _geo_block(source, lat_col, lon_col, lat: pd.Series, lon: pd.Series):
    points, total, cell_deg = _sample(lat, lon)
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return {
        "source": source,
        "lat_column": lat_col,
        "lon_column": lon_col,
        "points": points,
        "point_count": len(points),
        "total_valid": total,
        "sampled": total > len(points),
        "cell_deg": cell_deg,
        "regions": _regions(points, total),
        "bounds": {"min_lat": min(lats), "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)},
    }


def _point_coords(g):
    """GeoJSON Point -> (lon, lat), else (None, None)."""
    if isinstance(g, dict) and g.get("type") == "Point":
        c = g.get("coordinates")
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                return float(c[0]), float(c[1])
            except (TypeError, ValueError):
                pass
    return None, None


def _mostly_fractional(s: pd.Series) -> bool:
    """At least half the values have decimals: GPS degrees do, pixel or index columns usually don't."""
    vals = s.dropna()
    return len(vals) > 0 and bool((vals % 1 != 0).mean() >= 0.5)


def _find_xy(df: pd.DataFrame, numeric: list):
    """An x/y column pair with the same prefix ("scoordinate.x" + "scoordinate.y", "coord_x" + "coord_y", "x" + "y")
    that holds degrees: x = longitude (within 180), y = latitude (within 90). Projected coordinates (UTM, metres) fail the ranges."""
    for x in numeric:
        if _leaf(x) != "x" and _key(x) != "x":
            continue
        y = x[:-1] + ("Y" if x.endswith("X") else "y")
        if y == x or y not in numeric:
            continue
        if _in_range(df[x], 180) and _in_range(df[y], 90) and _mostly_fractional(df[x]) and _mostly_fractional(df[y]):
            return y, x
    return None, None


def _geo(df: pd.DataFrame, types: dict):
    """Find latitude/longitude columns (or GeoJSON Point geometry) and return map points, or None."""
    numeric = [c for c, t in types.items() if t == "numeric"]
    lat_col = _find_coord(numeric, LAT_NAMES, LAT_SUFFIX)
    lon_col = _find_coord([c for c in numeric if c != lat_col], LON_NAMES, LON_SUFFIX)
    if lat_col and lon_col and _in_range(df[lat_col], 90) and _in_range(df[lon_col], 180):
        block = _geo_block("columns", lat_col, lon_col, df[lat_col].astype(float), df[lon_col].astype(float))
        if block:
            return block

    lat_col, lon_col = _find_xy(df, numeric)
    if lat_col and lon_col:
        block = _geo_block("columns", lat_col, lon_col, df[lat_col].astype(float), df[lon_col].astype(float))
        if block:
            return block

    if "geometry" in df.columns:
        pairs = df["geometry"].map(_point_coords)
        lon = pd.Series([p[0] for p in pairs], index=df.index, dtype=float)
        lat = pd.Series([p[1] for p in pairs], index=df.index, dtype=float)
        return _geo_block("geojson", None, None, lat, lon)
    return None


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

    # 6b. raw view: summarized before normalization changes df
    n_preview = preview_rows(len(df))
    raw_frame = df[analysis_cols]
    raw_types = {c: _raw_type(raw_frame[c]) for c in analysis_cols}
    raw_view = {
        "head": df.head(n_preview).to_dict(orient="records"),
        "tail": df.tail(n_preview).to_dict(orient="records"),
        "info": {"note": _raw_note(fmt), "columns": _info_columns(raw_frame, raw_types, lz, dict.fromkeys(raw_types, 0))},
        "describe": {"note": _raw_note(fmt), "stats": _describe(raw_frame)},
        "ranges": _ranges(raw_frame.copy(), raw_types),
    }
    raw_view = jsonable(raw_view)
    del raw_frame

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
    columns = _info_columns(analyzed, types, lz, coerced)

    # 11. map points (lat/lon columns or GeoJSON Point geometry)
    geo = _geo(df, types)

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
        "head": df.head(n_preview).to_dict(orient="records"),
        "tail": df.tail(n_preview).to_dict(orient="records"),
        "info": {"note": _note(), "columns": columns},
        "describe": {"note": _note(), "stats": _describe(analyzed)},
        "ranges": _ranges(analyzed, types),
        "raw": raw_view,
        "geo": geo,
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
