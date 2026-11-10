# Dataset pipelines — API contract (v1)

For the UI developer. Status: **v1, deployed and tested** (2026-11-11) against a live `HttpData-PULL` transfer (Copenhagen bicycle parking, provider `annualtrafficnumbers`). Open for changes.

## Basics

- **Base URL:** sent separately (a `https://…run.app` URL for now; `https://pipelines.<domain>` once the domain exists).
- **Server-to-server only.** Call from the UI's Next.js server route, never from the browser. The API key and the provider token must not reach the client. No CORS is configured.
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
annualtrafficnumbers.connector.mds.think-it.io
opendatahub-prod-1.connector.mds.think-it.io
ua-mds-prod-1.connector.mds.think-it.io
connector-mds-prod.edc.aws.bmw.cloud
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
| `head` | First 5 rows |
| `info` | `note`, `columns[]` (`column`, `dtype`, `type`, `leading_zeros`, `coerced`, `non_null`, `null_pct`) |
| `describe` | `note`, `stats` |
| `ranges` | Per-column ranges |
| `schema` | `source`. **Store it and send it back unchanged** to `/v1/download` |

**`partial_warning: true`** means the provider reports more data than was fetched (e.g. a total of 47,415 records, but only the first page of 100 came through). Show a "partial data" notice. This is expected today: the connector doesn't forward pagination parameters.

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
