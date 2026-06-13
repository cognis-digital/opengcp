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
  storage.py     # GCS-style object storage  (local-FS or in-memory)
  firestore.py   # document database          (SQLite or in-memory)
  pubsub.py      # publish/subscribe broker   (in-process)
  functions.py   # event-driven function runner
  server.py      # one http.server exposing all services under path prefixes
  cli.py         # console entry point: `opengcp serve` + convenience commands
  __main__.py    # `python -m opengcp`
```

Each service is a self-contained, thread-safe Python class you can import and
use directly (`from opengcp import ObjectStorage, DocumentStore, PubSub,
FunctionRunner`). The server simply wires the four together and maps HTTP
routes onto them. Storage and Firestore persist under a local **data dir**
(`--data-dir`); with no data dir everything runs **in-memory** (ideal for
tests). The function runner auto-wires to storage and pub/sub so that writing
an object or publishing a message can trigger your registered handlers.

## Services

| Service          | Module          | Models a la            | Backend            | Path prefix   |
|------------------|-----------------|------------------------|--------------------|---------------|
| Object storage   | `storage.py`    | Cloud Storage (GCS)    | local files / RAM  | `/storage`    |
| Document DB      | `firestore.py`  | Firestore              | SQLite / RAM       | `/firestore`  |
| Pub/Sub broker   | `pubsub.py`     | Cloud Pub/Sub          | in-process queues  | `/pubsub`     |
| Function runner  | `functions.py`  | Cloud Functions        | in-process         | `/functions`  |

What each implements (a compatible **subset**):

- **Object storage** — create/get/delete buckets; upload/download/stat/list/
  delete objects; object names may contain `/`; per-object md5, size,
  content-type, and generation (bumped on overwrite). Path-traversal-safe keys.
- **Document DB** — collections of JSON documents; create (auto or explicit
  id), get, set (replace), update (merge), delete, list, list collections, and
  field queries with `== != < <= > >=` plus an optional `limit`.
- **Pub/Sub** — topics and subscriptions; fan-out (each subscription gets an
  independent copy); `pull` with ack-ids, `ack`, `nack` (immediate redelivery),
  ack-deadline expiry + automatic redelivery, delivery-attempt counting.
- **Function runner** — register a Python callable against an
  `object.finalize` or `pubsub.publish` trigger (optionally scoped to a bucket
  / topic); events dispatch synchronously, capture results and errors, and are
  recorded in an invocation log. Auto-fired by real storage writes and
  publishes.

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

storage = ObjectStorage()                 # in-memory
storage.create_bucket("uploads")
storage.upload("uploads", "a.txt", b"hello")
assert storage.download("uploads", "a.txt") == b"hello"

db = DocumentStore()                       # in-memory SQLite
db.set("users", "u1", {"name": "ada"})
assert db.get("users", "u1")["name"] == "ada"

ps = PubSub()
fns = FunctionRunner(pubsub=ps)            # auto-dispatch on publish
ps.create_topic("orders"); ps.create_subscription("w", "orders")
fns.register("on_order", "pubsub.publish", lambda e: print("got", e["data"]),
             resource="orders")
ps.publish("orders", b"new-order")         # prints: got b'new-order'
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

- **59 tests, all passing** (`python -m pytest -q` → `59 passed`).
- Coverage by area: object storage (11), document DB (14), pub/sub (11),
  function runner (10), HTTP server end-to-end (10), CLI (4).
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

- Signed URLs, resumable/multipart uploads, and object versioning history.
- Firestore composite indexes, transactions, `array-contains` / `in`
  operators, and sub-collections.
- Pub/Sub push delivery, ordering keys, dead-letter topics, and message
  retention policies.
- HTTP-triggered Cloud Functions and a remote function deployment model
  (current runner handles `object.finalize` and `pubsub.publish` in-process).
- Authentication / IAM emulation.

## License

Released under the **Cognis Open Collaboration License (COCL) 1.0** — see
[`LICENSE`](LICENSE). Non-commercial use is granted; commercial use requires a
separate license.
