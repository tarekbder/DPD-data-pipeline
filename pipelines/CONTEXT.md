# pipelines/ — the code

FastAPI app with two pipelines and the modules they share. Behaviour is specified in `../docs/dataset-pipelines-plan.md`; this file maps it to code.

## Modules

| File | Job | Used by |
|---|---|---|
| `api.py` | `GET /health`, `POST /v1/explore`, `POST /v1/download`; API key (`X-Pipelines-Key`); maps errors to `{code, message, detail}`; optional CORS | entry point |
| `config.py` | Env-var settings (limits, placeholders, thresholds, `PAGINATION_OVERRIDES`, `CORS_ORIGINS`, `ALLOWED_PROVIDER_HOSTS`, `PIPELINES_API_KEY`/`_NEXT`) | all |
| `errors.py` | `PipelineError` + helpers (`parse_failed`, `too_large`, `upstream_timeout`) | all |
| `transfer.py` | Builds `Transfer` from the EDR sent by the UI; auth header `Authorization` or `X-Api-Key`, value as given (no `Bearer `) | both pipelines |
| `policy.py` | Endpoint policy (HTTPS, `/public`, port 443, allowlisted host = counterparty host, no encoded slashes); DNS pinned to public IPv4 | transfer, fetch |
| `models.py` | Pydantic: pagination union, `schema.source`, request bodies, override loader | api, export, fetch |
| `fetch.py` | HTTP GET with size/time/page guards, policy check on every page, no redirects, no compressed responses; pagination detection and following; partial-data hint | both pipelines |
| `formats.py` | Format detection and sniffing; zip, JSON record path, GeoJSON rows, CSV page merge, XML parse/flatten/merge | both pipelines |
| `explore/pipeline.py` | Pipeline 1 (pandas): load → exclude nested → leading zeros → normalize → type → stats → JSON | api |
| `export/pipeline.py` | Pipeline 2 (Polars): Route A (original format) / Route B (convert), flatten nested, Excel limits, writers | api |

## If you change this, it hits

| Change | Hits | Does not hit |
|---|---|---|
| `transfer.py` auth header | Every fetch in both pipelines | Error mapping (`api.py`) |
| `policy.py` / `ALLOWED_PROVIDER_HOSTS` | Which providers can be fetched at all (both pipelines) | Parsing, typing, exports |
| `fetch.py` pagination detection | `schema.source.pagination` from explore → what export re-fetches | Format detection |
| `formats.py` record path / XML row path | Explore rows **and** export rows (both read the stored path) | Typing rules in explore |
| `formats.flatten_xml` | Column names in explore and in every export format | JSON/CSV sources |
| `models.Source` fields | Explore output `schema.source`, export validation, the UI (sends it back unchanged) | Explore stats |
| `explore` typing / normalization | Summary only (`info`, `describe`, `ranges`, `health`) | Downloads (always raw) |
| `export` flattening / writers | Download files only | Explore summary |
| `config.py` defaults | Limits and labels in both pipelines; `excel_allowed` | Plan text: update it too |

## Not built yet

- Sample/replay mode from `../docs/specs/` (saved response instead of a live fetch, labelled as sample).
- Following CKAN `result._links.next`: blocked until the connector forwards query params (see plan, "Blockers").
