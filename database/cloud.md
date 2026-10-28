# Private team hosting

Deployed 2026-10-15. The private service is available at https://ups-team-selector.onrender.com and runs on cloud Python with Turso persistence. The laptop does not need to remain awake. The reusable deployment blueprint remains in `render.yaml`.

## Verified catalog v2 workflow

The v2 projection stores manufacturer-verified identity and W/VA separately from legacy observations, separates battery-backed outlet groups from total/surge/hardwire outputs, and binds each runtime curve to its battery configuration. Missing or conflicting facts remain unknown and cannot pass automatic allocation. Historical model-only/SKU merges remain quarantined until their bundle identity is resolved. A regional end-of-sale notice is displayed only when the manufacturer's product URL matches both the stated region and verified manufacturer SKU; Thai notices are shown separately from actual Thai stock and sale availability. Original prices and source references stay in the private catalog and backup; public code must not contain partner costs, raw source paths or credentials.

Eaton source validation also recognizes the exact `assets.tripplite.com` host for official Eaton/Tripp Lite documents; sibling and nested hosts are not accepted. Eaton [confirmed its acquisition of Tripp Lite](https://www.eaton.com/us/en-us/company/news-insights/news-releases/2021/eaton-completes-the-acquisition-of-tripp-lite--expanding-eaton-s.html). Link to manufacturer documents rather than copying restricted drawings into the repository.

Create an immutable local backup of the current remote catalog before staging. Use `stage_verified_catalog.py` with a private reviewed evidence manifest to create a new v2 SQLite file; use `append_verified_catalog.py` only for source records with a verified identity, manufacturer W/VA and a projected Dealer/NDP offer. Both tools refuse to overwrite their inputs. The staged file contains a fingerprint of the remote snapshot it was built from; successive staging/append operations preserve that base.

The following PowerShell example snapshots only this app's two catalog tables; it is not a general backup of unrelated Turso data. Set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in a private process environment first. Use a fresh ignored filename for each backup, keep it private, and do not modify it after creation. A failed attempt can leave an incomplete file; choose a new filename and investigate before retrying.

```powershell
$upsPython = (uv python find --no-project 3.11)
@'
import os, sqlite3
from pathlib import Path
from database.turso_store import TursoStore
from database.publish_turso import _read_catalog

path = Path('database/local/current-remote-backup.sqlite3')
path.parent.mkdir(parents=True, exist_ok=True)
source = TursoStore(os.environ['TURSO_DATABASE_URL'], os.environ['TURSO_AUTH_TOKEN'], cache_seconds=0).connect()
try:
    with path.open('xb'):
        pass
    with sqlite3.connect(path) as target:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed')
finally:
    source.close()
_, rows = _read_catalog(path, expected=True)
print('Private catalog backup verified; models:', len(rows))
'@ | & $upsPython -B -X utf8 -
```

Deploy the v2-capable server first: v1 data without verified metadata fails closed. Then publish the reviewed v2 file using `publish_turso.py --db <staged-file> --expected-source <remote-backup>`. The publisher checks JSON consistency, retains existing model identities and original price/source evidence, checks the staged fingerprint, and compares both the original and written catalog inside its transaction. Unexpected tables, triggers, views, or other user-defined schema objects abort publication. A mismatch aborts instead of overwriting a concurrent change. Identity splits and intentional price/evidence changes require a separately reviewed migration, not this ordinary refresh. The staging fingerprint is removed from live metadata. After publishing, verify the remote data and an authenticated browser flow; the server may cache a prior catalog for up to 60 seconds.

## Release record - 2026-10-28

- PR #8 was merged as commit `c9e577195a381f0f48344fb678a85c537523732e`. Render reported that exact commit as Live; the public health endpoint returned 200 and unauthenticated page and script requests returned 401.
- The first v2 Turso update retained the original 223 models and added 14 verified, priced models. A fresh private snapshot verified SQLite integrity and matched all 237 staged rows and live metadata. The previous private snapshot remains available for recovery.
- A subsequent private v2 update retained all 237 rows and added 15 manufacturer-verified priced models, for 252 rows. A fresh snapshot passed SQLite integrity and matched the staged rows and metadata. Source conflicts remain flagged and unverified facts remain unknown.
- PR #9 was merged as commit `825bf40638c538d4f7f3730d41812ebe979116d0` and that commit was deployed Live on Render. After the user signed in, an authenticated hosted-browser test allocated two synthetic 80 W devices to one CyberPower configuration (22.18 minutes, connector type unchecked) and to one APC BR900MI (27.22 minutes, IEC C14 to C13 verified). The operator did not receive the team password.
- This is an incremental evidence release, not a claim that every legacy model, connector, battery configuration, runtime, price, or local availability is verified.

The 2026-10-15 records below are historical setup evidence, not current catalog counts or a current deployment acceptance result.

## Deployment record - 2026-10-15

- Render Free web service in Singapore, deployed from `feat/catalog-filters-turso` at commit `7b1c44e10e9e6f42bcbaa250569e98d52f94644c`; automatic deployment is off. PR #5 remains separate from this deployment.
- Dedicated Turso database in Tokyo contains the reviewed private projection: 223 models. A remote SQL count and a snapshot load with the application's read-only token both returned 223.
- Three Render environment secrets are configured: database URL, read-only database token, and one user-supplied login hash. No passwords, tokens, or private projection files are committed. The import token expires after one day; the application token expires after one year and must be rotated before expiry.
- Render reported `Deploy succeeded | Live`. HTTPS checks returned 200 for `/health` and 401 with the `UPS Team` authentication challenge for `/`, `/catalog.js`, and POST `/api/catalog/search` without credentials.
- Successful login and authenticated hosted calculations still require the user's acceptance check; the deployment agent did not receive the user's plaintext password.

## Release validation and promotion - 2026-10-15

Before promoting PR #5, a fresh verification run passed all 156 Python tests (`python -B -X utf8 -m unittest discover -s database/tests`), all 68 JavaScript tests (`npm test`), the CSS build (`npm run build:css`), and `git diff --check`. The CSS build retained the existing outdated Browserslist-data warning. GitHub reported no combined commit statuses, so these are local verification results rather than a GitHub CI claim.

After merging the reviewed PR, configure Render's deployment branch as `main` and manually deploy its merged commit. Keep automatic deployment off. Confirm Render reports the expected commit as Live, then repeat HTTPS health and unauthenticated access checks. The initial feature-branch deployment above remains the historical setup record.

## 1. Create the catalog database

Create a Turso account and a dedicated empty database for this application. Keep it separate from unrelated customer or business data. Export the derived catalog with `published_catalog.py`; for a populated destination, use the v2 staging and expected-source workflow above. Include dealer prices only in the private team export; original documents, raw extraction text, and private file paths must not be uploaded.

The historical private projection kept catalog entries with recorded W and VA. V2 keeps those observations for review but requires separate manufacturer verification before automatic allocation. A staged refresh uses the current remote backup as its base:

```text
python database/stage_verified_catalog.py --source database/local/current-remote-backup.sqlite3 --output database/local/team-catalog-v2.sqlite3 --manifest database/local/reviewed-evidence.json
python database/publish_turso.py --db database/local/team-catalog-v2.sqlite3 --expected-source database/local/current-remote-backup.sqlite3
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

Authentication hashing is limited to two concurrent requests. Additional requests wait up to ten seconds for a hashing slot, allowing the browser's parallel protected CSS/JavaScript requests to finish without an immediate 429. Sustained contention can still return a generic 429 after that bounded wait. After five failed credential attempts within a minute, that connection IP receives generic HTTP 429 responses until the failure window expires; a successful login before the limit clears its failures. The failure limit is checked again after waiting and the result is recorded before releasing the slot. Failure tracking holds at most 1,024 addresses and resets when the process restarts. Initial requests without credentials receive the browser login challenge and do not consume this failure allowance. Behind the hosted proxy, clients may share the same connection IP and allowance; the app deliberately does not trust arbitrary client-supplied forwarded IP headers. This in-process bound is suitable for the single-instance prototype; use a trusted identity gateway for deployment-wide account controls.

Fix recorded 2026-10-15: live browser requests reproduced HTTP 429 for protected scripts and `UPScalculations is not defined`, leaving the page buttons unusable. The four-asset concurrency regression reproduced two successful responses and two 429 responses before the fix. The handler now queues normal asset bursts while preserving authentication on every asset and API request.

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
