# Deployment plan — dataset pipelines only (Google Cloud Run)

Status: draft, 2026-11-10. Replaces the pipelines parts of `deployment-plan.md`. The UI and the consumer connector are out of scope: the UI developer hosts the UI elsewhere and connects it.

| Decision | Choice |
|---|---|
| Scope | Pipelines only |
| Hosting | **Google Cloud Run**, free tier, scale to zero |
| Cost target | $0: stay inside the always-free allowance; budget alert as a tripwire |
| UI hosting | Elsewhere, run by the UI developer |
| How the UI reaches the pipelines | Public HTTPS URL, called **server-to-server** from the UI's Next.js server, with an API key |
| Request contract | `pipelines-api-contract.md` |
| Provider host allowlist | Env var `ALLOWED_PROVIDER_HOSTS`, starting with the 4 hosts from the UI's `data-preview-policy.ts` |
| Domain | Cloudflare domain, provided later; use the `*.run.app` URL until then |

---

## 1. Architecture

```
Browser ──► UI (Next.js, hosted elsewhere)
              │ resolves transfer + current EDR (management API, stays in the UI)
              │ POST https://<service>.run.app/v1/explore   X-Pipelines-Key: <key>
              ▼
           Cloud Run service (FastAPI :8080, 0–2 instances)
              │ key check → endpoint policy (allowlist, HTTPS, /public, DNS pinning)
              ▼
           Provider EDC /public
```

- The browser never calls the pipelines. Key and provider token stay on servers, so **no CORS** (`CORS_ORIGINS` empty).
- Cloud Run is deployed with `--allow-unauthenticated`; the **app** checks the key. Cloud Run IAM would need the UI to mint Google ID tokens from outside Google Cloud: too much setup for the UI developer.

---

## 2. Cloud Run settings

| Setting | Value | Why |
|---|---|---|
| Region | `europe-west1` | Close to Berlin, lowest price tier. Check the free-tier region list when creating |
| CPU / memory | 2 vCPU / 4 GiB | pandas + Polars on large files. Re-check after the load test |
| Concurrency | 1 | One big dataset per instance, avoids out-of-memory |
| Min / max instances | 0 / 2 | Scale to zero = no cost when idle; max caps cost and abuse |
| Request timeout | 600 s (max 3600 s) | Covers large datasets; final value after the load test |
| CPU billing | Request-based (default) | Free tier counts only while requests run |
| Port | 8080 | Matches the Dockerfile |
| Secrets | `PIPELINES_API_KEY` in Secret Manager, mounted as env var | Never in the repo or image |
| Config | Env vars from `config.py` + `ALLOWED_PROVIDER_HOSTS` | |

**Free-tier maths:** one heavy call at 2 vCPU / 4 GiB for 100 s ≈ 200 vCPU-s and 400 GiB-s. The monthly allowance covers several hundred such calls. Light calls cost close to nothing.

**Where cost can leak, and the guard:**
| Risk | Guard |
|---|---|
| Egress beyond 1 GB/month (big downloads) | Keep the export size limit; watch the billing report |
| Container image storage (Artifact Registry, 0.5 GB free) | Slim image; cleanup policy keeping only the last 2 images |
| Builds | Build in GitHub Actions, not Cloud Build |
| Runaway traffic | `max-instances 2`, key check before any work, budget alert at €1 |

A budget alert **notifies, it doesn't stop charges**. If you want a hard stop, add a budget → Pub/Sub → function that disables billing (optional).

---

## 3. What's missing

### 3.1 Packaging and repo
| Item | Status |
|---|---|
| New repo `dataset-pipelines/` (code, `tests/`, fixtures) | missing; `tests/` isn't in this folder |
| `Dockerfile`: `python:3.13-slim`, non-root user, `CMD uvicorn pipelines.api:app --host 0.0.0.0 --port ${PORT:-8080}` | missing |
| `.dockerignore` (tests, data, docs, `.git`) | missing; keeps the image small |
| `GET /health` (no auth, no provider call) | missing |
| Pinned dependencies (`==` versions) | `requirements.txt` uses `>=` only |

### 3.2 Code changes
| Change | Why |
|---|---|
| **API key check** (`X-Pipelines-Key`, constant-time compare, accepts 2 keys for rotation) as a FastAPI dependency on `/v1/*` | Cloud Run has no Worker in front, so the app does it |
| New request contract: EDR in (`/v1/explore`, `/v1/download`) | Agreed shape for the UI developer |
| Auth header from EDR: `Authorization` **or** `X-Api-Key`, value sent as given | Matches the UI's `authorizationHeaders()` |
| Endpoint policy: HTTPS only, `/public` path, no custom port or userinfo, host in `ALLOWED_PROVIDER_HOSTS` **and** equal to the counterparty host, encoded-slash check | `transfer.py` accepts plain `http` and any host today |
| DNS pinning to public IPv4; block redirects; refuse or safely decode compressed responses | Cloud Run has **no free outbound allowlist**, so the code is the only guard against SSRF |
| Formula-prefix escaping (`=`, `+`, `-`, `@`) in CSV, TSV, XLSX | Not done in `export/` |
| Never log tokens, keys or request bodies | Check uvicorn access log and error handlers |
| Sample mode (labelled Copenhagen sample, never a silent fallback) | Can ship after v1 |

### 3.3 Still to decide
- **Limits** after the load test (step 6.3): memory, timeout, `MAX_RESPONSE_MB`.
- **Custom domain:** Cloud Run domain mapping (DNS-only CNAME in Cloudflare) is only available in some regions; check `europe-west1` when the domain arrives. The `run.app` URL works meanwhile.
- **Staging:** none; deploy straight to `pipelines`, roll back via revisions.

---

## 4. Initial `ALLOWED_PROVIDER_HOSTS`

```
annualtrafficnumbers.connector.mds.think-it.io
opendatahub-prod-1.connector.mds.think-it.io
ua-mds-prod-1.connector.mds.think-it.io
connector-mds-prod.edc.aws.bmw.cloud
```

---

## 5. CI/CD (Cloud Build trigger, set up in the Console)

One service, `pipelines`. Every push to `main` builds the `Dockerfile` and deploys a new revision. No staging: test on `main` carefully; roll back via **Cloud Run → Revisions → Manage traffic** (100 % to the previous revision).

---

## 6. Rollout (each step has a gate)

| # | Step | Gate |
|---|---|---|
| 6.1 | Code changes (3.2) | Done; dependencies pinned |
| 6.2 | Repo pushed, `pipelines` service created | `/health` 200; no key → 401; disallowed host → 403 |
| 6.3 | Load test on `pipelines` | Memory and timeout adjusted |
| 6.4 | Hand contract, URL and key to the UI developer | Preview, summary and every download format work for one live transfer |
| 6.5 | Domain arrives: domain mapping + Cloudflare DNS record | Contract base URL updated |

---

## 7. Risks

- **Cold start:** Python with pandas/Polars takes several seconds on the first call after idle. Acceptable; the UI shows a loading state.
- **Pagination blocker:** the connector doesn't forward query params, so live transfers return page 1 only. Provider-side, unchanged by deployment.
- **Public URL:** protected by the key, `max-instances` and the endpoint policy. A leaked key allows calls only to allowlisted provider hosts with a valid EDR.
- **Free-tier changes:** Google can change the allowance with notice. The budget alert catches it.
