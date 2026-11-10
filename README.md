# Dataset pipelines

Server-side **Explore** and **Export** pipelines for the Mobility Data Space (MDS) data viewer.

## The project

[Mobility Data Space](https://mobility-dataspace.eu) is a data space where organisations share mobility data (traffic counts, parking, public transport, …) through **EDC connectors** (Eclipse Dataspace Components). A consumer finds a data offer in the catalogue, negotiates a contract with the provider, and starts a transfer. For an `HttpData-PULL` transfer, the consumer's connector receives an **EDR** (Endpoint Data Reference): a provider URL plus a short-lived access token.

The **data viewer** is the MDS connector UI (Next.js, separate repo). It lets a consumer browse offers, negotiate, start transfers and look at the data they received.

This repo is the part that **looks at the data**. Given the EDR of a running transfer, the pipelines fetch the dataset from the provider and:

- **Explore**: return a summary: rows and columns, column types, missing values, statistics, value ranges, the first rows, and a data-health score.
- **Export**: return the dataset as a file, either in its original format or converted (CSV, TSV, JSON, Excel, Parquet).

The pipelines replace the UI's built-in data preview. They run as their own service; the UI calls them from its server.

```
Browser ──► Data viewer UI (Next.js)
              │ checks the transfer, gets the current EDR (connector management API)
              │ POST /v1/explore or /v1/download  (X-Pipelines-Key)
              ▼
           Dataset pipelines (this repo, Cloud Run)
              │ endpoint policy: HTTPS, /public, allowlisted provider, pinned public IP
              ▼
           Provider EDC /public  ──►  dataset
```

## What the pipelines do

**Explore** (pandas)
1. Fetch the dataset (following pagination where it can be detected) and detect the format.
2. Load the records; set aside nested columns.
3. Detect leading-zero codes (postcodes, IDs) so they stay text.
4. Normalise placeholders (`N/A`, `null`, `-`, …) to missing.
5. Infer types (number, date, text) from a sample.
6. Compute statistics, ranges and health, and return JSON, including a `schema` that Export reuses.

**Export** (Polars)
- **Original format**: the received data, pages merged, unchanged.
- **Converted**: CSV, TSV, JSON, XLSX or Parquet. Nested fields are flattened for tabular formats, Excel limits are checked, and spreadsheet formula prefixes are escaped.

Export fetches the data again, so the EDR must still be valid.

**Input formats:** CSV, TSV, JSON, GeoJSON, XML, and ZIP containing one of those.

## API

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/health` | none | `{"status": "ok"}` |
| POST | `/v1/explore` | `X-Pipelines-Key` | Dataset summary + `schema` |
| POST | `/v1/download` | `X-Pipelines-Key` | File in the requested format |

Call it from a server only, never from the browser: the request contains the provider token. Errors always have the shape `{code, message, detail}`.

Full request/response spec: [`pipelines-api-contract.md`](pipelines-api-contract.md).

## Security

- **API key** on every `/v1` call (`X-Pipelines-Key`, constant-time check, two keys for rotation).
- **Endpoint policy** on every request, including pagination: HTTPS only, port 443, path under `/public`, host on `ALLOWED_PROVIDER_HOSTS` **and** equal to the transfer's counterparty, no userinfo or encoded slashes.
- **DNS pinning**: the provider host must resolve to public IPv4 only; the connection goes to that address. Environment proxies are ignored.
- No redirects, no compressed responses, size, time and page limits.
- Provider credentials are forwarded exactly as EDC supplied them and never logged or echoed in errors.

## Configuration

Environment variables (see `pipelines/config.py` for all of them):

| Variable | Default | Purpose |
|---|---|---|
| `PIPELINES_API_KEY` | — (**required**; no key = every `/v1` call refused) | API key |
| `PIPELINES_API_KEY_NEXT` | — | Second key during rotation |
| `ALLOWED_PROVIDER_HOSTS` | the 4 approved MDS provider hosts | Comma-separated hosts the pipelines may fetch from |
| `MAX_RESPONSE_MB` | `500` | Maximum fetched size |
| `FETCH_DEADLINE_S` / `REQUEST_TIMEOUT_S` | `300` / `30` | Total fetch time / per request |
| `MAX_PAGES` | `1000` | Pagination limit |
| `PAGINATION_OVERRIDES` | — | Per-asset pagination settings (JSON) |
| `CORS_ORIGINS` | — | Only if a browser calls the API directly (not the intended setup) |

## Project structure

```
pipelines/
├─ api.py            FastAPI app, routes, API key, error mapping
├─ config.py         settings from env vars
├─ models.py         request models (EDR, schema, pagination)
├─ transfer.py       EDR → Transfer, auth header
├─ policy.py         endpoint policy and pinned DNS
├─ fetch.py          guarded HTTP fetch and pagination
├─ formats.py        format detection, ZIP, JSON/GeoJSON/CSV/XML helpers
├─ explore/          pipeline 1 (pandas)
├─ export/           pipeline 2 (Polars)
└─ requirements.txt  pinned dependencies
Dockerfile, .dockerignore
```

Which change affects what: [`pipelines/CONTEXT.md`](pipelines/CONTEXT.md).

## Run locally

```bash
pip install -r pipelines/requirements.txt
PIPELINES_API_KEY=dev-key uvicorn pipelines.api:app --port 8080
```

With Docker:

```bash
docker build -t pipelines .
docker run -p 8080:8080 -e PIPELINES_API_KEY=dev-key pipelines
```

A live call needs a real EDR from a started `HttpData-PULL` transfer against one of the allowed providers.

## Deployment

Google Cloud Run (free tier), service `pipelines`, region `europe-west1`: 2 vCPU, 4 GiB, scale to zero, at most 2 instances, one request per instance.

- Every push to `main` builds the `Dockerfile` (Cloud Build) and deploys a new revision. There's no staging.
- The API key comes from Secret Manager (`pipelines-key`).
- **Rollback:** Cloud Run → `pipelines` → Revisions → Manage traffic → 100 % to the previous revision.

Steps and settings: [`pipelines-deployment-plan.md`](pipelines-deployment-plan.md).

## Known limitations

- **Pagination:** the connector doesn't forward query parameters, so live transfers currently return only the first page. That's provider/connector side.
- **Sample mode** (a labelled saved sample instead of a live fetch) isn't built yet.
- **New providers** need their host added to `ALLOWED_PROVIDER_HOSTS` and a redeploy.
- **Cold start:** the first call after idle takes several seconds longer.
