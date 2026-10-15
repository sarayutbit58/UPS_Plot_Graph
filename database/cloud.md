# Private team hosting

Deployed 2026-10-15. The private service is available at https://ups-team-selector.onrender.com and runs on cloud Python with Turso persistence. The laptop does not need to remain awake. The reusable deployment blueprint remains in `render.yaml`.

## Deployment record - 2026-10-15

- Render Free web service in Singapore, deployed from `feat/catalog-filters-turso` at commit `7b1c44e10e9e6f42bcbaa250569e98d52f94644c`; automatic deployment is off. PR #5 remains separate from this deployment.
- Dedicated Turso database in Tokyo contains the reviewed private projection: 223 models. A remote SQL count and a snapshot load with the application's read-only token both returned 223.
- Three Render environment secrets are configured: database URL, read-only database token, and one user-supplied login hash. No passwords, tokens, or private projection files are committed. The import token expires after one day; the application token expires after one year and must be rotated before expiry.
- Render reported `Deploy succeeded | Live`. HTTPS checks returned 200 for `/health` and 401 with the `UPS Team` authentication challenge for `/`, `/catalog.js`, and POST `/api/catalog/search` without credentials.
- Successful login and authenticated hosted calculations still require the user's acceptance check; the deployment agent did not receive the user's plaintext password.

## 1. Create the catalog database

Create a Turso account and a dedicated empty database for this application. Keep it separate from unrelated customer or business data. Export the derived catalog with `published_catalog.py`, then publish that export with `publish_turso.py --db <curated-export.sqlite3>`. Check each tool's `--help` for the current export arguments. Include dealer prices only in the private team export; original documents, raw extraction text, and private file paths must not be uploaded.

The default private team projection keeps real UPS models with known W and VA. It retains unknown outlets, runtime, and expansion as unknown so the nearby browser can explain them. This preserves every model currently eligible for automatic allocation while leaving capacity-less rows out of the hosted dataset:

```text
python database/published_catalog.py --db database/local/ups-products.sqlite3 --output database/local/team-catalog.sqlite3 --include-prices
python database/publish_turso.py --db database/local/team-catalog.sqlite3
```

Choose a fresh output filename if that export already exists. The source database is opened read-only. Publishing is a separate explicit network action after the export has been reviewed.

Add --include-incomplete only when the team wants models with missing capacity as well. On 2026-10-15 the default projection retained 223 of 782 real UPS models, omitted 559 lacking complete capacity, and measured 1,097,728 bytes instead of the 39,026,688-byte source. This is a dated catalog observation, not a current stock or price guarantee. Safe counts and the projection timestamp appear in the catalog response. The generated private file remains under the ignored database/local directory. The publisher refuses empty exports and a model_count that differs from the actual rows before making any remote request.

The publisher reads `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` from the local process environment. Use a write-capable token for this explicit import, then create a separate read-only database token for the hosted application. Set secrets through a private environment or secret manager; do not paste tokens into source code, terminal commands, tickets, or chat. The import replaces the dedicated catalog transactionally and refuses unrelated remote tables.

## 2. Prepare individual team logins

Run from the repository root with user-space Python:

```text
python database/cloud_api.py hash-password
```

Enter a password twice at the hidden prompts. The command prints only a salted PBKDF2-SHA256 hash. Repeat for each teammate. Store this JSON as the secret `UPS_USERS_JSON`, substituting the generated hash for each value:

```json
{"teammate-1":"<generated pbkdf2_sha256 hash>","teammate-2":"<generated pbkdf2_sha256 hash>"}
```

There are no default users or passwords. Do not use the example placeholder as an actual hash. Each browser asks for a username and password before loading the page or dealer prices. To revoke a user, remove their entry and restart/redeploy the service. Browser Basic authentication can remain cached until the browser session closes. All configured users have access to the same private catalog; this version has no per-user commercial permission tiers or SSO.

Authentication hashing is limited to two concurrent requests. After five failed credential attempts within a minute, that connection IP receives generic HTTP 429 responses until the failure window expires; a successful login before the limit clears its failures. Failure tracking holds at most 1,024 addresses and resets when the process restarts. Initial requests without credentials receive the browser login challenge and do not consume this failure allowance. Behind the hosted proxy, clients may share the same connection IP and allowance; the app deliberately does not trust arbitrary client-supplied forwarded IP headers. This in-process bound is suitable for the single-instance prototype; use a trusted identity gateway for deployment-wide account controls.

## 3. Create the hosted service

Create a Render account, connect the repository, and create a Blueprint service from `render.yaml`. Enter the three secret values requested by the blueprint:

| Variable | Value |
|---|---|
| `TURSO_DATABASE_URL` | Dedicated database HTTPS or libsql URL |
| `TURSO_AUTH_TOKEN` | Hosted application's read-only token |
| `UPS_USERS_JSON` | Individual usernames mapped to generated hashes |

Render supplies `PORT` and `RENDER_EXTERNAL_HOSTNAME`. The server accepts that exact hostname and the matching HTTPS Origin. For a custom domain, set `UPS_ALLOWED_HOST` to the exact desired hostname, without scheme, slash, or wildcard. Only one hostname is accepted at a time. The service requires `X-Forwarded-Proto: https`; run it behind the trusted Render TLS proxy, which must set that header. Do not expose the Python socket directly on an untrusted network while treating client-provided forwarding headers as proof of TLS.

The build command only parses the Python source. There is no package installation or CSS build; the service serves committed assets. Automatic deploy is disabled. After reviewing the catalog and secrets, start a manual deployment in Render. Open its HTTPS URL and confirm a login prompt appears before page content. `/health` is public liveness only and intentionally does not report database status, model counts, secrets, or private records.

## Limits and operational behavior

The API exposes fixed recommendation, catalog, allocation, and optimization operations. It provides no arbitrary SQL endpoint. Request bodies are limited to 1,000,000 bytes and must be finite JSON objects. One optimizer request runs at a time; concurrent optimization receives HTTP 429 and can be retried. Source/runtime rules remain in the existing engines. Missing backend data returns a generic HTTP 503. Database snapshots are cached for 60 seconds and recreated as read-only in-memory SQLite connections per request; stored catalog changes can take up to that cache interval to appear.

This is a small private team prototype. HTTP Basic authentication uses individual salted password hashes over HTTPS, with no default credentials. It is not an enterprise identity gateway: use an external authenticated gateway or extend the service before requiring SSO, central audit trails, or stronger account management.

Official hosting facts checked 2026-10-15: Render Free web services sleep after 15 idle minutes and can take about a minute to restart. Their local filesystem is ephemeral, so the catalog stays in Turso. Free services have 750 instance hours per workspace per month, and Render describes Free as unsuitable for production. [Render Free documentation](https://render.com/docs/free).

At the same check, Turso Free listed 100 databases, 5 GB storage, 500 million rows read per month, and 10 million rows written per month. These are quotas rather than a promise of unlimited free operation; check account usage and current terms before committing. [Turso pricing](https://turso.tech/pricing).

## Local verification

```text
python -B -X utf8 -m unittest discover -s database/tests -p test_cloud_api.py
```

The handler tests use synthetic data and loopback ports. `create_cloud_server(..., allow_insecure=True)` is an explicit development-only option restricted to loopback clients and loopback configured hostnames. Production startup does not enable it and binds `0.0.0.0` only for the hosted service.
