# Dataset pipelines — API contract (v1, draft)

For the UI developer. Status: draft, 2026-11-10, open for changes.

## Basics

- Base URL: given separately (a `https://…run.app` URL for now; `https://pipelines.<domain>` later).
- **Call from the UI server only** (Next.js route), never from the browser. The key and the provider token must not reach the client.
- Every request except `/health` needs the header `X-Pipelines-Key: <key>`. Missing or wrong → `401`.
- Request bodies are JSON (`Content-Type: application/json`). Unknown fields are rejected.

## What the UI does before calling

Same as the current preview route (`src/app/connector/data-preview/route.ts`):
1. Load the transfer; require `CONSUMER`, `STARTED`, `HttpData-PULL`.
2. Get the current EDR for that transfer and its data address.
3. Send the fields below. Don't send the management API key or the whole TransferProcess.

## The `edr` object

| Field | Type | From | Rules |
|---|---|---|---|
| `endpoint` | string | EDR data address `endpoint` | HTTPS, `/public` path, host on the allowlist |
| `auth_key` | `"Authorization"` \| `"X-Api-Key"` | EDR `authKey` (or `Authorization` when `authorization` is set) | |
| `auth_value` | string | EDR `authorization` / `authCode` | Sent to the provider exactly as given, no `Bearer ` added. Max 16 KiB, no line breaks |
| `counter_party_address` | string | transfer `counterPartyAddress` | Its host must equal the `endpoint` host |
| `asset_id` | string | transfer `assetId` | Used for file names |

## Endpoints

### `GET /health`
No auth. `200 {"status": "ok"}`.

### `POST /v1/explore`
```json
{ "edr": { "endpoint": "...", "auth_key": "Authorization", "auth_value": "...",
           "counter_party_address": "...", "asset_id": "..." } }
```
`200` with:

| Key | Content |
|---|---|
| `meta` | `asset_id`, `format`, `container`, `rows`, `cols`, `pages`, `partial_warning`, `excel_allowed`, `excluded_columns`, `generated_at` |
| `health` | `missing_pct`, `missing_cells`, `total_cells` |
| `head` | First 5 rows |
| `info` | `note`, `columns[]` (`column`, `dtype`, `type`, `leading_zeros`, `coerced`, `non_null`, `null_pct`) |
| `describe` | `note`, `stats` |
| `ranges` | Per-column ranges |
| `schema` | `source`. **Keep it and send it back unchanged** to `/v1/download` |

### `POST /v1/download`
```json
{ "edr": { ... }, "format": "csv", "schema": { "source": { ... } } }
```
`format`: `csv` `tsv` `json` `xlsx` `parquet` `geojson` `xml` `zip`. Only offer `xlsx` when `meta.excel_allowed` is true.

`200` with the file as the body.
- `Content-Disposition: attachment; filename="..."`: pass it through to the browser.
- `X-Export-Warning` (optional): show it to the user.

Downloads re-fetch from the provider, so the EDR must still be valid.

## Errors

Every error has the body `{"code", "message", "detail"}`. `message` is safe to show to the user. `detail` is for logs and never contains credentials.

| HTTP | `code` | Meaning / UI action |
|---|---|---|
| 400 | `invalid_transfer` | Bad or missing `edr` field |
| 400 | `invalid_schema` | Bad `schema`: run explore again |
| 400 | `excel_limit` | Too big for Excel: offer CSV |
| 401 | `unauthorized` | Missing or wrong `X-Pipelines-Key` |
| 401/403 | `transfer_revoked` | Provider rejected the token: start a new transfer |
| 403 | `endpoint_not_allowed` | Host not allowed, not HTTPS, or not matching the counterparty |
| 413 | `too_large` | Dataset over the size limit |
| 415 | `unsupported_format` | Format not supported |
| 422 | `parse_failed` | Data couldn't be read |
| 429 | (from Cloud Run, body may not be JSON) | Busy, all instances in use: retry after a few seconds |
| 502 | `upstream_error` / `not_found_upstream` | Provider unreachable / dataset not found |
| 504 | `upstream_timeout` | Provider too slow: retry |
| 500 | `internal_error` | Bug: report it |

## Timeouts and limits

Final values follow the load test. Plan for **up to 10 minutes** on large datasets (set your server-side fetch timeout accordingly) and show a loading state. The first call after idle takes several seconds longer (cold start). Only one dataset is processed per instance, so under load a call can get `429`: retry.

## Versioning

Breaking changes go to `/v2/`. New optional response fields can be added to v1 at any time, so ignore unknown fields.
