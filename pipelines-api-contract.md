# Dataset pipelines — API contract (v1)

For the UI developer. Status: **v1, deployed and tested** (2026-11-11) against a live `HttpData-PULL` transfer (Copenhagen bicycle parking, provider `annualtrafficnumbers`). Revised 2026-11-16 (CORS, pagination). Open for changes.

## Basics

- **Base URL:** sent separately (a `https://…run.app` URL for now; `https://pipelines.<domain>` once the domain exists).
- **Server-to-server only.** Call from the UI's Next.js server route, never from the browser. The API key and the provider token must not reach the client. CORS is **off** by default. It can be turned on (`CORS_ORIGINS`: POST only; exposes `Content-Disposition` and `X-Export-Warning`), but that isn't the intended setup.
- **Auth:** every request except `/health` needs the header `X-Pipelines-Key: <key>`. Missing or wrong → `401`. The key is sent separately; keep it in a server env var.
- **Requests:** JSON (`Content-Type: application/json`). Unknown fields are rejected.
- **Responses:** `Cache-Control: no-store`. Don't cache them.

## What the UI does before calling

Same as the current preview route (`src/app/connector/data-preview/route.ts`):
1. Load the transfer; require `type = CONSUMER`, `state = STARTED`, `transferType = HttpData-PULL`.
2. Get the **current EDR** for that transfer (`/v3/edrs/request` → `/v3/edrs/{id}/dataaddress`).
3. Send the fields below as **plain strings**: unwrap JSON-LD values first (`[{"@value": "…"}]` → `"…"`, as the existing `edcValue()` does). Never send the management API key or the whole TransferProcess.

## The `edr` object

| Field | From | Rules |
|---|---|---|
| `endpoint` | EDR data address `endpoint` | HTTPS, port 443, path `/public…`, host on the allowlist (below). Max 4096 chars |
| `auth_key` | `"Authorization"` when the EDR has `authorization`; otherwise the EDR's `authKey` | `Authorization` or `X-Api-Key` (case-insensitive) |
| `auth_value` | EDR `authorization` (or `authCode`) | Sent to the provider **exactly as given**: don't add `Bearer `. Max 16 KiB, no line breaks |
| `counter_party_address` | transfer `counterPartyAddress` | Its host must equal the `endpoint` host |
| `asset_id` | transfer `assetId` | Used for download file names. Max 256 chars |

Example (token shortened):
```json
{ "edr": {
    "endpoint": "https://annualtrafficnumbers.connector.mds.think-it.io/public",
    "auth_key": "Authorization",
    "auth_value": "eyJraWQiOi…",
    "counter_party_address": "https://annualtrafficnumbers.connector.mds.think-it.io/api/dsp/2025-1",
    "asset_id": "counts,-bicycle-parking-1.1" } }
```

### Allowed providers

Only these hosts are fetched; anything else gets `403 endpoint_not_allowed`. New providers are added on request (config change + redeploy).

```

## Endpoints

### `GET /health`
No auth. `200 {"status": "ok"}`.

### `POST /v1/explore`
Body: `{ "edr": { … } }`. `200` with:

| Key | Content |
|---|---|
| `meta` | `asset_id`, `format` (`csv` `tsv` `json` `geojson` `xml`), `container` (`"zip"` or `null`), `rows`, `cols`, `pages`, `partial_warning`, `excel_allowed`, `excluded_columns`, `generated_at` |
| `health` | `missing_pct`, `missing_cells`, `total_cells` |
| `head` | First 50 rows (20 when `meta.rows` < 100) |
| `tail` | Last 50 rows (20 when `meta.rows` < 100) |
| `info` | `note`, `columns[]` (`column`, `dtype`, `type`, `leading_zeros`, `coerced`, `non_null`, `null_pct`) |
| `describe` | `note`, `stats` |
| `ranges` | Per-column ranges |
| `raw` | Same `head`, `tail`, `info`, `describe`, `ranges`, on the values as read (no normalization, native types only; CSV/TSV/XML columns are all strings). Top-level fields are the normalized view |
| `geo` | Map points, or `null` when no coordinates are found. `source` (`"columns"` or `"geojson"`), `lat_column`, `lon_column` (`null` for GeoJSON), `points` (`[lat, lon, row, count]`, WGS84; `row` is the 0-based row index, `count` the rows the point stands for), `point_count`, `total_valid`, `sampled` (`true` when thinned to `MAP_MAX_POINTS`, default 5,000), `cell_deg` (grid cell size in degrees, `null` when not thinned), `bounds` (`min_lat`, `max_lat`, `min_lon`, `max_lon`) |
| `schema` | `source` (format, delimiter, zip inner file, record path, pagination). **Store it and send it back unchanged** to `/v1/download` |

**`geo`**: latitude/longitude columns are found by name (`latitude`/`lat`, `longitude`/`lon`/`lng`/`long`, or names ending in those such as `gps_lat`, `position.lng`), must be numeric, and at least 90% of values must be in range. Rows with missing, out-of-range or `0,0` coordinates are dropped. Above `MAP_MAX_POINTS`, points are grid-thinned: square cells of `cell_deg` degrees are laid over the data and each occupied cell keeps its first row, with `count` = rows in that cell, so every area with data appears. If no such columns exist, GeoJSON `Point` features are used. Other geometry (lines, polygons, WKT/WKB strings, projected coordinates such as UTM) is not mapped.

**`partial_warning: true`** means the provider reports more data than was fetched (e.g. a total of 47,415 records, but only the first page of 100 came through). Show a "partial data" notice. `false` doesn't guarantee complete data: it only catches responses with fields like `total`, `count` or `page`.

### Pagination

Handled server-side; the UI does nothing beyond storing `schema`.
- **Auto-detected:** a next link in the body (`next` / `links.next`) or a `Link: rel="next"` header. Pages are followed until none remains (same origin as `endpoint` only, up to `MAX_PAGES`).
- **Per asset:** `offset` and `page` schemes are set server-side in `PAGINATION_OVERRIDES` (keyed by `asset_id`, config change + redeploy). Ask for one when a dataset comes back partial.
- The detected scheme is stored in `schema.source.pagination`, so downloads follow the same pages as explore.
- **Current blocker:** the connector doesn't forward query parameters, so live transfers return page 1 only, overrides included. The fix is provider-side (`proxyQueryParams: true` on the asset).

### `POST /v1/download`
Body: `{ "edr": { … }, "format": "<format>", "schema": <schema from explore> }`.

Which formats to offer:

| Format | When | What you get |
|---|---|---|
| `csv` `tsv` `json` `parquet` | always | Converted. Nested fields flattened (CSV/TSV/XLSX); cells starting with `= + - @` prefixed with `'` (CSV/TSV/XLSX) |
| `xlsx` | only if `meta.excel_allowed` | Converted, as above |
| the source format (`meta.format`) | always | **Original** data, unchanged (pages merged). For `json` sources, `json` returns the original response |
| `zip` | only if `meta.container == "zip"` | Original ZIP, unchanged |

Any other format → `400 invalid_schema`.

`200` with the file as the body, plus:
- `Content-Type` per format (e.g. `text/csv; charset=utf-8`, `application/vnd.apache.parquet`)
- `Content-Disposition: attachment; filename="<asset_id>_<UTC time>.<ext>"`: pass it through to the browser
- `X-Export-Warning` (optional): show it to the user

Downloads **fetch from the provider again**, so the EDR must still be valid; get the current EDR before each call.

## Errors

Body: `{"code", "message", "detail"}`. `message` is safe to show to users. `detail` is for logs and never contains credentials.

| HTTP | `code` | Meaning / UI action |
|---|---|---|
| 400 | `invalid_transfer` | Bad or missing `edr` field (`detail.errors` lists which) |
| 400 | `invalid_schema` | Bad `schema` or format not allowed: run explore again |
| 400 | `excel_limit` | Too big for Excel: offer CSV |
| 401 | `unauthorized` | Missing or wrong `X-Pipelines-Key` |
| 401/403 | `transfer_revoked` | Provider rejected the token: get a new EDR or start a new transfer |
| 403 | `endpoint_not_allowed` | `detail.reason`: `not_https`, `custom_port`, `userinfo`, `not_public_path`, `encoded_slash`, `host_not_allowed`, `host_not_counterparty`, `counterparty_invalid`, `non_public_address` |
| 413 | `too_large` | Dataset over the size limit |
| 415 | `unsupported_format` | Format not supported, nested ZIP, or compressed response |
| 422 | `parse_failed` | Data couldn't be read |
| 429 | (from Cloud Run; body may not be JSON) | All instances busy: retry after a few seconds |
| 502 | `upstream_error` / `not_found_upstream` | Provider unreachable, redirected, or dataset not found |
| 504 | `upstream_timeout` | Provider too slow: retry |
| 500 | `internal_error` | Bug: report it with the time of the call |

## Timing

- Measured: explore of a 100-row JSON dataset ≈ **3 s**.
- First call after idle: **several seconds longer** (cold start; the service scales to zero).
- Server limit: **10 minutes** per request. Set your server-side fetch timeout to at least that, and show a loading state.
- One dataset is processed per instance, at most 2 instances: under load a call can get `429`.

## Example (Next.js server route)

```ts
const res = await fetch(`${process.env.PIPELINES_URL}/v1/explore`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Pipelines-Key": process.env.PIPELINES_API_KEY! },
  body: JSON.stringify({ edr }),
  cache: "no-store",
  signal: AbortSignal.timeout(600_000),
});
const body = await res.json(); // on !res.ok: { code, message, detail }
```

## Versioning

Breaking changes go to `/v2/`. New optional response fields can be added to v1 at any time, so ignore unknown fields.
