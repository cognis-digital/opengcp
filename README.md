# opengcp

## Usage — step by step

A typical local-cloud lifecycle with the `opengcp` console command:

1. **Install** the CLI (puts `opengcp` on your PATH):

   ```bash
   pipx install git+https://github.com/cognis-digital/opengcp.git
   ```

2. **Start the all-in-one server.** With no data dir everything is in-memory; pass `--data-dir` to persist storage and the document DB (the `serve` subcommand also accepts `--host` and `--port`):

   ```bash
   opengcp serve --port 8085 --data-dir ./opengcp-data
   ```

3. **Exercise the services from the CLI.** The convenience subcommands operate on the same `--data-dir`, so you can script storage, Firestore, and Pub/Sub without an SDK. Note `storage cp` takes `<file> <bucket/name>` and `cat` writes the object to stdout:

   ```bash
   opengcp --data-dir ./opengcp-data storage mb mybucket
   opengcp --data-dir ./opengcp-data storage cp ./photo.jpg mybucket/photo.jpg
   opengcp --data-dir ./opengcp-data fs set users u1 '{"name":"ada"}'
   opengcp --data-dir ./opengcp-data pubsub publish events "hello"
   ```

4. **Read the output.** `fs get` prints the document as indented JSON, `storage ls` prints `<size>  <name>` rows, and `storage cat` streams raw bytes — all pipeable:

   ```bash
   opengcp --data-dir ./opengcp-data fs get users u1            # indented JSON
   opengcp --data-dir ./opengcp-data storage ls mybucket        # size + name
   opengcp --data-dir ./opengcp-data storage cat mybucket/photo.jpg > out.jpg
   ```

5. **Use it in CI.** Seed fixtures against a throwaway data dir and assert on the JSON — fast, free, deterministic, no credentials:

   ```bash
   opengcp version
   opengcp --data-dir ./ci fs set users u1 '{"name":"ada"}'
   test "$(opengcp --data-dir ./ci fs get users u1 | jq -r .name)" = "ada"
   ```

## What is this?

**opengcp** is an independent, open-source **local reimplementation of the core
primitives of a major cloud platform's developer surface** — object storage, a
document database, a publish/subscribe broker, and an event-driven function
runner — that you run **on your own machine**. It is meant for local
development, automated testing, and offline work, in the same spirit as tools
like LocalStack, MinIO, and the Firebase Emulator Suite.

In plain terms: if your app talks to cloud object storage, a document DB, a
message queue, and cloud functions, you normally need a cloud account, network
access, and (often) money to run or test it. opengcp gives you a **single small
process** that stands in for those services so you can build and test entirely
on `localhost` — fast, free, deterministic, and with no credentials.

**Who it's for:** developers and CI pipelines that want to exercise
cloud-shaped code paths without a real cloud account; people learning how these
primitives fit together; and anyone who wants reproducible, offline integration
tests.

It is written in **pure Python standard library** (no third-party runtime
dependencies) and runs the same on Linux, macOS, and Windows.

> ### DISCLAIMER
> opengcp is an **independent, open reimplementation for LOCAL development and
> testing**. It is **NOT affiliated with, endorsed by, or sponsored by** Google
> LLC or any cloud vendor. Vendor and product names (e.g. "Google Cloud
> Platform", "Cloud Storage", "Firestore", "Pub/Sub", "Cloud Functions") are
> used **only nominatively** to describe the API shapes opengcp is *compatible
> with*. opengcp implements a **compatible SUBSET** of those models and is
> **not intended for production use**.

## Architecture

opengcp is a `opengcp/` package with **one module per service**, a single HTTP
server that exposes all of them, and a CLI:

```
opengcp/
  storage.py            # GCS-style object storage  (local-FS or in-memory)
  firestore.py          # document database          (SQLite or in-memory)
  pubsub.py             # publish/subscribe broker   (in-process)
  functions.py          # event-driven function runner (http + pubsub + storage + firestore triggers)
  datastore.py          # Datastore-style entity store (SQLite or in-memory)
  bigtable.py           # Bigtable-lite (instances/tables/column-families/rows, in-memory)
  bigquery.py           # BigQuery-lite (datasets/tables/insertAll/SELECT, SQLite or in-memory)
  tasks.py              # Cloud Tasks-lite (queues, scheduled tasks, dispatcher)
  scheduler.py          # Cloud Scheduler-lite (cron jobs, run_now, history)
  cloudrun.py           # Cloud Run-lite (deploy Python handler as service, invoke over HTTP)
  iam.py                # Cloud IAM (roles, policy bindings, testIamPermissions)
  secretmanager.py      # Secret Manager (secrets + versions + access + state machine)
  kms.py                # Cloud KMS (key rings/keys, encrypt/decrypt, generateDataKey)
  logging_service.py    # Cloud Logging (write/list/filter log entries, tail, delete)
  monitoring.py         # Cloud Monitoring (metric descriptors, time-series, alignment)
  identityplatform.py   # Identity Platform Auth (sign-up/sign-in, ID tokens, user mgmt)
  server.py             # one http.server exposing all services under path prefixes
  cli.py                # console entry point: `opengcp serve` + convenience commands
  __main__.py           # `python -m opengcp`
```

Each service is a self-contained, thread-safe Python class you can import and
use directly. The server wires all sixteen together and maps HTTP routes onto
them. Storage, Firestore, Datastore, BigQuery, IAM, Secret Manager, Logging,
Monitoring, and Identity Platform persist under a local **data dir**
(`--data-dir`); with no data dir everything runs **in-memory** (ideal for
tests). KMS is always in-memory. The function runner auto-wires to storage and
pub/sub so that writing an object or publishing a message can trigger your
registered handlers.

## Services

| Service              | Module                   | Models a la                  | Backend             | Path prefix           |
|----------------------|--------------------------|------------------------------|---------------------|-----------------------|
| Object storage       | `storage.py`             | Cloud Storage (GCS)          | local files / RAM   | `/storage`            |
| Document DB          | `firestore.py`           | Firestore                    | SQLite / RAM        | `/firestore`          |
| Pub/Sub broker       | `pubsub.py`              | Cloud Pub/Sub                | in-process queues   | `/pubsub`             |
| Function runner      | `functions.py`           | Cloud Functions              | in-process          | `/functions`          |
| Entity store         | `datastore.py`           | Cloud Datastore              | SQLite / RAM        | `/datastore`          |
| Wide-column DB       | `bigtable.py`            | Cloud Bigtable               | in-process / RAM    | `/bigtable`           |
| Analytical DB        | `bigquery.py`            | BigQuery                     | SQLite / RAM        | `/bigquery`           |
| Task queues          | `tasks.py`               | Cloud Tasks                  | in-process          | `/tasks`              |
| Cron jobs            | `scheduler.py`           | Cloud Scheduler              | in-process          | `/scheduler`          |
| Service runner       | `cloudrun.py`            | Cloud Run                    | in-process          | `/cloudrun`           |
| IAM                  | `iam.py`                 | Cloud IAM                    | SQLite / RAM        | `/iam`                |
| Secret Manager       | `secretmanager.py`       | Secret Manager               | SQLite / RAM        | `/secretmanager`      |
| Key Management       | `kms.py`                 | Cloud KMS                    | in-memory           | `/kms`                |
| Structured Logging   | `logging_service.py`     | Cloud Logging                | SQLite / RAM        | `/logging`            |
| Metrics              | `monitoring.py`          | Cloud Monitoring             | SQLite / RAM        | `/monitoring`         |
| Auth / Identity      | `identityplatform.py`    | Identity Platform / Firebase | SQLite / RAM        | `/identityplatform`   |

What each implements (a compatible **subset**):

- **Object storage** — create/get/delete buckets (with versioning enable/disable
  and lifecycle rules stub); upload/download/stat/list/delete objects; custom
  user metadata; server-side copy (`copy_object`); compose (concatenate up to 32
  sources); `list_objects` with `prefix` and `delimiter` for simulated directory
  listing; object versioning (soft-delete to noncurrent, versioned download by
  generation, `delete_version`). Path-traversal-safe keys.
- **Document DB** — collections of JSON documents; create (auto or explicit
  id), get, set (replace), update (merge), delete, list, list collections, and
  field queries with `== != < <= > >=` plus an optional `limit`.
- **Pub/Sub** — topics and subscriptions; fan-out (each subscription gets an
  independent copy); `pull` with ack-ids, `ack`, `nack` (immediate redelivery),
  ack-deadline expiry + automatic redelivery, delivery-attempt counting;
  **ordering keys** (per-key serial delivery — next message in key is blocked
  until the current one is acked); **dead-letter policy** (`maxDeliveryAttempts`
  threshold forwards the message to a dead-letter topic); `modify_ack_deadline`
  to extend or shrink individual in-flight deadlines; **push delivery** (register
  a Python callable as a push handler — messages are auto-delivered and acked/nacked
  in a daemon thread); `update_subscription` to change ack-deadline and dead-letter
  policy after creation.
- **Function runner** — register a Python callable against an `object.finalize`,
  `pubsub.publish`, `http`, or `firestore.write` trigger (optionally scoped to a
  bucket / topic / collection); events dispatch synchronously, capture results and
  errors, and are recorded in an invocation log. Auto-fired by real storage writes
  and publishes. HTTP-triggered functions can be invoked via
  `POST /functions/<name>/invoke`. `firestore.write` events carry operation type
  (CREATE / UPDATE / DELETE), new data, and old data.
- **Entity store (Datastore-lite)** — entities with (kind, id) keys (integer or
  string ids; auto-assigned integer ids on `put`); put/get/delete; list all
  entities of a kind; programmatic `query()` with `== != < <= > >=`
  conditions, `ORDER BY`, and `LIMIT`; GQL-lite `gql()` supporting
  `SELECT * FROM Kind [WHERE ... [AND ...]] [ORDER BY ... [ASC|DESC]] [LIMIT n]`
  with string and numeric literals. Backed by SQLite for persistence.
- **Bigtable-lite** — instances, tables, and column families (with optional
  `max_versions` GC rule — older cells pruned on each write); `mutate_row`
  applies a list of mutations (SetCell, DeleteCell, DeleteFromFamily,
  DeleteFromRow) atomically; `read_row` with optional family filter; `scan_rows`
  by key prefix or `[start, end)` range with limit; `read_column` for
  projection. In-memory only (no disk persistence).
- **BigQuery-lite** — datasets and tables with declared schema (advisory);
  `insert_all` streaming inserts (list of `{insertId, json}` rows); full
  SQL-lite `query()` supporting `SELECT *` or projected columns,
  `FROM dataset.table`, `WHERE col op val [AND ...]` (ops: `= != < <= > >= LIKE`
  with `%` wildcard), `GROUP BY col` with `COUNT(*)`, `SUM(col)`, `AVG(col)`,
  `MIN(col)`, `MAX(col)` aggregates, `ORDER BY col [ASC|DESC]`, `LIMIT n`.
  Backed by SQLite for persistence.
- **Cloud Tasks-lite** — named queues with configurable `RetryConfig`
  (max_attempts, exponential backoff: min_backoff, max_backoff, max_doublings)
  and `RateLimits`; create tasks with optional `schedule_time` (deferred
  dispatch); background dispatcher thread polls every 50 ms; register a Python
  handler per queue or supply a `url` for real HTTP dispatch; exponential-backoff
  retry on failure; tasks reach SUCCEEDED or FAILED terminal states; pause/resume
  and purge queues.
- **Cloud Scheduler-lite** — named cron jobs with full 5-field cron expression
  parser (`*/N` steps, ranges, lists, `@hourly`/`@daily`/`@weekly`/`@monthly`/
  `@yearly` aliases); background evaluator fires at the matching wall-clock
  minute; `run_now` for manual dispatch; per-job execution history with success/
  failure status and error capture; pause/resume jobs; register or replace
  handlers after creation.
- **Cloud Run-lite** — deploy any Python callable as a named service;
  `invoke(name, ...)` passes an HTTP-shaped request dict (method, path, headers,
  body, queryParams) to the handler and returns a response dict (status, headers,
  body); configurable `ServiceConfig` (max_concurrency enforced via semaphore,
  timeout); per-service invocation log with latency and error capture; redeploy
  replaces the handler in-place; services exposed at
  `POST /cloudrun/services/<name>/invoke` over the HTTP server.
- **Cloud IAM** — role registry (5 built-in predefined roles: viewer/editor/owner
  and two service-specific; unlimited custom roles with arbitrary permission sets);
  `getIamPolicy` / `setIamPolicy` per named resource (replaces full binding list);
  `testIamPermissions` — returns the subset of requested permissions that a given
  principal holds, resolving through all granted roles including `allUsers` /
  `allAuthenticatedUsers` wildcards; create/update/soft-delete custom roles.
  Backed by SQLite for persistence.
- **Secret Manager** — create/get/list/delete named secrets with optional labels;
  add secret versions (arbitrary binary payloads); access version payload by
  number or `latest` alias; per-version state machine `ENABLED → DISABLED →
  DESTROYED` (destroyed versions have their payload wiped); `latest` skips
  destroyed versions. Backed by SQLite for persistence.
- **Cloud KMS** — key rings and symmetric crypto keys; each key carries a
  randomly-generated 32-byte master key; encrypt / decrypt via a stdlib
  CTR-mode cipher (HMAC-SHA256 counter stream + integrity tag — opengcp-local
  cipher, not AES; provides the same interface); optional
  `additionalAuthenticatedData` bound to the ciphertext; `generateDataKey`
  produces a random 32-byte DEK returned plaintext + wrapped (encrypted with the
  KMS key). In-memory only.
- **Cloud Logging** — write structured log entries (JSON or text payload) with
  severity, labels, resource, and a log name; list/filter entries by log name,
  minimum severity (DEFAULT/DEBUG/INFO/NOTICE/WARNING/ERROR/CRITICAL/ALERT/
  EMERGENCY), and a simple AND-of-predicate `filter` expression (supports
  `logName`, `severity`, and `labels.<key>`); `tail` returns the N most-recently
  inserted entries; `delete_log` purges all entries for a log name. Backed by
  SQLite for persistence.
- **Cloud Monitoring** — metric descriptor registry (create/get/list/delete with
  `valueType`, `metricKind`, `unit`, and label definitions); write time-series
  data points (DOUBLE, INT64, or STRING values) for any metric type with resource
  and metric labels; list time-series filtered by metric type and time range;
  alignment reducers (ALIGN_MEAN / ALIGN_SUM / ALIGN_MIN / ALIGN_MAX) over a
  configurable `alignment_period` in seconds. Backed by SQLite for persistence.
- **Identity Platform Auth** — email+password sign-up (PBKDF2-HMAC-SHA256 with
  random salt, 100 000 iterations) and sign-in returning a short-lived opengcp ID
  token (HMAC-SHA256 signed header.payload.sig, 1-hour TTL); `verify_id_token`
  checks the signature and expiry; `create_custom_token` for server-to-server
  flows with optional extra claims; full user management — get / get-by-email /
  list / update (display name, password, disabled) / delete; token expiry enforced.
  Backed by SQLite for persistence.

## Quickstart

Start the all-in-one local server:

```bash
opengcp serve --port 8085            # in-memory
opengcp serve --port 8085 --data-dir ./opengcp-data   # persistent
# or without installing:
python -m opengcp serve --port 8085
```

Talk to it over plain HTTP (no SDK, no credentials):

```bash
# object storage
curl -X POST  localhost:8085/storage/b/mybucket
curl -X POST  localhost:8085/storage/b/mybucket/o/hello.txt --data-binary "hi"
curl          localhost:8085/storage/b/mybucket/o/hello.txt        # -> hi

# document database
curl -X POST  localhost:8085/firestore/users -d '{"name":"ada","age":36}'
curl 'localhost:8085/firestore/users?field=name&op===&value="ada"'

# pub/sub
curl -X POST  localhost:8085/pubsub/topics/events
curl -X POST 'localhost:8085/pubsub/subscriptions/sub1?topic=events'
curl -X POST  localhost:8085/pubsub/topics/events/publish -d '{"data":"hello"}'
curl -X POST  localhost:8085/pubsub/subscriptions/sub1/pull
```

Or use the library directly:

```python
from opengcp import ObjectStorage, DocumentStore, PubSub, FunctionRunner
from opengcp import DatastoreDB, DSKey, BigtableAdmin, BigQueryDB
from opengcp.bigtable import SetCell

storage = ObjectStorage()                 # in-memory
storage.create_bucket("uploads")
storage.upload("uploads", "a.txt", b"hello")
assert storage.download("uploads", "a.txt") == b"hello"

# versioning
storage.create_bucket("versioned", versioning_enabled=True)
storage.upload("versioned", "k", b"v1")
storage.upload("versioned", "k", b"v2")
assert storage.download("versioned", "k", generation=1) == b"v1"

# copy + compose
storage.copy_object("uploads", "a.txt", "uploads", "copy.txt")
storage.compose("uploads", "joined.txt", ["a.txt", "copy.txt"])

db = DocumentStore()                       # in-memory SQLite
db.set("users", "u1", {"name": "ada"})
assert db.get("users", "u1")["name"] == "ada"

ps = PubSub()
fns = FunctionRunner(pubsub=ps)            # auto-dispatch on publish
ps.create_topic("orders"); ps.create_subscription("w", "orders")
fns.register("on_order", "pubsub.publish", lambda e: print("got", e["data"]),
             resource="orders")
ps.publish("orders", b"new-order")         # prints: got b'new-order'

# Datastore
ds = DatastoreDB()
key = ds.put(DSKey("Person"), {"name": "ada", "age": 36})
print(ds.get(key))                         # -> {"name": "ada", "age": 36}
results = ds.gql("SELECT * FROM Person WHERE age > 30")

# Bigtable
bt = BigtableAdmin()
inst = bt.create_instance("prod")
tbl = inst.create_table("users")
tbl.create_column_family("info", max_versions=3)
tbl.mutate_row("user#1", [SetCell("info", "name", b"ada")])
print(tbl.read_row("user#1"))
print(tbl.scan_rows(prefix="user#"))

# BigQuery
bq = BigQueryDB()
bq.create_dataset("analytics")
bq.create_table("analytics", "events", [{"name": "ts", "type": "INTEGER"},
                                          {"name": "event", "type": "STRING"}])
bq.insert_all("analytics", "events", [
    {"json": {"ts": 1, "event": "click"}},
    {"json": {"ts": 2, "event": "view"}},
])
rows = bq.query("SELECT COUNT(*), event FROM analytics.events GROUP BY event")
```

Convenience CLI subcommands:

```bash
opengcp storage mb mybucket
opengcp storage cp ./photo.jpg mybucket/photo.jpg
opengcp storage ls mybucket
opengcp storage cat mybucket/photo.jpg > out.jpg
opengcp fs set users u1 '{"name":"ada"}'
opengcp fs get users u1
opengcp pubsub publish events "hello"
opengcp datastore put Person '{"name":"ada","age":36}'
opengcp datastore query Person --gql "SELECT * FROM Person WHERE age > 30"
opengcp bq create-dataset analytics
opengcp bq create-table analytics.events '[{"name":"ts","type":"INTEGER"},{"name":"event","type":"STRING"}]'
opengcp bq insert analytics.events '{"ts":1,"event":"click"}'
opengcp bq query "SELECT COUNT(*) FROM analytics.events"
```

<!-- cognis:domains:start -->
## Domains

**Primary domain:** Cloud & DevTools  ·  **JTF MERIDIAN division:** ATHENA-PRIME · COGNI-2

**Topics:** `cognis` `devtools` `cloud` `developer-tools` `python` `cloud-emulator`

Part of the **Cognis Neural Suite** — 300+ source-available tools organized across 12 domains under the JTF MERIDIAN command structure. See the [suite on GitHub](https://github.com/cognis-digital) and [jtf-meridian](https://github.com/cognis-digital/jtf-meridian) for how the pieces fit together.
<!-- cognis:domains:end -->

## Install

opengcp is **source-available** and is **not published to PyPI**. Install it
straight from the git repository.

### One-line installers

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/cognis-digital/opengcp/main/install.sh | bash
```

```powershell
# Windows PowerShell
iwr -useb https://raw.githubusercontent.com/cognis-digital/opengcp/main/install.ps1 | iex
```

### pipx (recommended — isolated, puts `opengcp` on your PATH)

```bash
pipx install git+https://github.com/cognis-digital/opengcp.git
```

### uv

```bash
uv tool install git+https://github.com/cognis-digital/opengcp.git
```

### pip (from git)

```bash
pip install "git+https://github.com/cognis-digital/opengcp.git"
```

### From source

```bash
git clone https://github.com/cognis-digital/opengcp.git
cd opengcp
pip install .          # or: pip install -e ".[dev]" for development
```

### No install at all

The core has **no third-party runtime dependencies**, so you can also just run
it from a checkout:

```bash
python -m opengcp serve
```

Requires **Python 3.10+**. Works on Linux, macOS, and Windows.

## Verification

This repository ships a real, end-to-end pytest suite that round-trips data
through every service — both by calling the service classes directly and by
driving the actual HTTP server in-process.

- **407 tests, all passing** (`python -m pytest -q` → `407 passed`).
- Coverage by area: object storage (11 original + 20 extended), document DB (14),
  pub/sub (11 original + 16 extended), function runner (10 original + 16 extended),
  HTTP server end-to-end (10 original + 28 extended), CLI (4),
  Datastore (17), Bigtable (19), BigQuery (33),
  Cloud Tasks (19), Cloud Scheduler (20), Cloud Run (20),
  IAM (18), Secret Manager (16), Cloud KMS (16),
  Cloud Logging (15), Cloud Monitoring (16), Identity Platform Auth (23),
  HTTP server identity+security+ops end-to-end (38).
- CI runs the same suite on **ubuntu / macos / windows × Python 3.10–3.13**
  (see `.github/workflows/ci.yml`).

Run it yourself:

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Topics / Domains

`local-cloud-emulator` · `gcp-compatible` · `object-storage` ·
`document-database` · `pubsub` · `serverless-functions` · `offline-development`
· `integration-testing` · `developer-tooling` · `pure-python` · `stdlib-only`

## Roadmap

Not yet implemented (clearly out of scope for the current subset):

- **Cloud Storage:** Signed URLs, resumable/multipart uploads, object lock /
  retention policies, object ACLs, bucket notifications, HMAC keys.
- **Firestore:** composite indexes, transactions, `array-contains` / `in`
  operators, and sub-collections.
- **Pub/Sub:** message retention policies, snapshot/seek, filter expressions on
  subscriptions.
- **Cloud Functions:** remote function deployment model (load from file/module
  path); per-function concurrency / scaling config.
- **Datastore:** ancestor queries / entity groups, projections, multi-property
  ORDER BY, cursor-based pagination.
- **Bigtable:** disk persistence; server-side filters (row-range,
  column-qualifier, value-range, condition); read-modify-write
  (CheckAndMutate); replication.
- **BigQuery:** DML (INSERT/UPDATE/DELETE), streaming buffer flush, table
  partitioning, views, table export, job API, external tables.
- **Cloud Tasks:** real-time rate limiting (token bucket), task deduplication
  window, IAP-authenticated HTTP dispatch.
- **Cloud Scheduler:** time-zone aware scheduling, retry config per job,
  Pub/Sub and HTTP target types (current: Python callable only).
- **Cloud Run:** traffic-split / revision management, volume mounts, secrets,
  VPC connector emulation.
- **IAM:** IAM conditions, organization/folder hierarchy, audit log, workload
  identity federation, service account impersonation; IAM enforcement wired
  into individual service requests (currently IAM is a standalone service —
  not enforced at the storage/firestore/etc. layer).
- **Secret Manager:** automatic secret rotation, customer-managed encryption
  keys (CMEK), replication policies (per-region), secret annotations.
- **Cloud KMS:** real AES-256 encryption (stdlib has no AES; current cipher is
  opengcp-local HMAC-CTR), asymmetric key pairs, key import, key rotation,
  audit logging, customer-supplied encryption keys (CSEK).
- **Cloud Logging:** log sinks (export to storage/pubsub), log-based metrics,
  `protoPayload`, exclusion filters, log buckets with retention policies.
- **Cloud Monitoring:** alerting policies, notification channels, uptime checks,
  dashboards, cross-project aggregation, MQL queries.
- **Identity Platform:** OAuth/OIDC provider federation, multi-factor
  authentication, phone number auth, anonymous sign-in, refresh tokens,
  session cookies, tenant management.

## Interoperability

`opengcp` composes with the 300+ tool Cognis suite — JSON in/out and a shared
OpenAI-compatible `/v1` backbone. See **[INTEROP.md](INTEROP.md)** for the
suite map, composition patterns, and reference stacks.

## Integrations

Forward `opengcp`'s findings to STIX/MISP/Sigma/Splunk/Elastic/Slack/webhooks via
[`cognis-connect`](https://github.com/cognis-digital/cognis-connect). See **[INTEGRATIONS.md](INTEGRATIONS.md)**.

## License

Released under the **Cognis Open Collaboration License (COCL) 1.0** — see
[`LICENSE`](LICENSE). Non-commercial use is granted; commercial use requires a
separate license.
