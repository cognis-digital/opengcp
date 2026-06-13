"""Command-line interface for opengcp.

Subcommands:
  serve                                   start the local HTTP server (all services)
  storage mb <bucket>                     make a bucket
  storage cp <file> <b/name>              upload a file to <bucket>/<name>
  storage cat <b/name>                    print an object to stdout
  storage ls <bucket> [prefix]            list objects
  fs set <coll> <id> <json>               set a document
  fs get <coll> <id>                      get a document
  pubsub publish <topic> <data>           publish a message
  datastore put <kind> <json>             put an entity (auto-assigns id)
  datastore get <kind> <id>               get an entity by id
  datastore query <kind> [--gql <stmt>]   list a kind or run GQL
  bq create-dataset <dataset>             create a BigQuery dataset
  bq create-table <dataset>.<table> <schema_json>  create a table
  bq insert <dataset>.<table> <json>      insert a row
  bq query <sql>                          run a SELECT query
  version                                 print version
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .server import OpenGCPServer, Services


def _split_path(spec: str):
    if "/" not in spec:
        raise SystemExit(f"expected <bucket>/<name>, got: {spec}")
    bucket, _, name = spec.partition("/")
    return bucket, name


def cmd_serve(args):
    server = OpenGCPServer(host=args.host, port=args.port, data_dir=args.data_dir)
    print(f"opengcp {__version__} listening on {server.base_url}")
    print(f"data dir: {args.data_dir or '(in-memory)'}")
    print("services: storage, firestore, pubsub, functions, datastore, bigtable, bigquery")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.stop()
    return 0


def _services(args):
    return Services(data_dir=args.data_dir)


def cmd_storage(args):
    svc = _services(args)
    s = svc.storage
    if args.action == "mb":
        s.create_bucket(args.target)
        print(f"created bucket {args.target}")
    elif args.action == "cp":
        # storage cp <source_file> <bucket/name>
        source = args.target
        dest = args.source
        if not source or not dest:
            raise SystemExit("usage: opengcp storage cp <file> <bucket/name>")
        bucket, name = _split_path(dest)
        with open(source, "rb") as fh:
            data = fh.read()
        try:
            s.get_bucket(bucket)
        except Exception:
            s.create_bucket(bucket)
        meta = s.upload(bucket, name, data)
        print(f"uploaded {meta.size} bytes to {bucket}/{name}")
    elif args.action == "cat":
        bucket, name = _split_path(args.target)
        sys.stdout.buffer.write(s.download(bucket, name))
    elif args.action == "ls":
        items, _prefixes = s.list_objects(args.target, args.prefix or "")
        for m in items:
            print(f"{m.size:>10}  {m.name}")
    else:
        raise SystemExit(f"unknown storage action: {args.action}")
    return 0


def cmd_fs(args):
    svc = _services(args)
    db = svc.firestore
    if args.action == "set":
        data = json.loads(args.data)
        db.set(args.collection, args.doc_id, data)
        print(f"set {args.collection}/{args.doc_id}")
    elif args.action == "get":
        print(json.dumps(db.get(args.collection, args.doc_id), indent=2))
    else:
        raise SystemExit(f"unknown fs action: {args.action}")
    return 0


def cmd_pubsub(args):
    svc = _services(args)
    ps = svc.pubsub
    if args.action == "publish":
        try:
            ps.create_topic(args.topic)
        except Exception:
            pass
        mid = ps.publish(args.topic, args.data)
        print(f"published {mid}")
    else:
        raise SystemExit(f"unknown pubsub action: {args.action}")
    return 0


def cmd_datastore(args):
    from .datastore import DatastoreDB, Key
    svc = _services(args)
    ds = svc.datastore
    if args.action == "put":
        data = json.loads(args.data)
        key = ds.put(Key(args.kind), data)
        print(f"put {args.kind}/{key.id}")
    elif args.action == "get":
        try:
            eid = int(args.id)
        except ValueError:
            eid = args.id
        data = ds.get(Key(args.kind, eid))
        print(json.dumps(data, indent=2))
    elif args.action == "query":
        if args.gql:
            rows = ds.gql(args.gql)
        else:
            rows = ds.list_kind(args.kind)
        for key, data in rows:
            print(f"{key}: {json.dumps(data)}")
    else:
        raise SystemExit(f"unknown datastore action: {args.action}")
    return 0


def cmd_bq(args):
    from .bigquery import BigQueryDB
    svc = _services(args)
    bq = svc.bigquery
    if args.action == "create-dataset":
        bq.create_dataset(args.dataset)
        print(f"created dataset {args.dataset}")
    elif args.action == "create-table":
        ref = args.table_ref
        if "." not in ref:
            raise SystemExit("table_ref must be <dataset>.<table>")
        ds_id, tbl_id = ref.split(".", 1)
        schema = json.loads(args.schema)
        bq.create_table(ds_id, tbl_id, schema)
        print(f"created table {ds_id}.{tbl_id}")
    elif args.action == "insert":
        ref = args.table_ref
        if "." not in ref:
            raise SystemExit("table_ref must be <dataset>.<table>")
        ds_id, tbl_id = ref.split(".", 1)
        row = json.loads(args.row_json)
        bq.insert_all(ds_id, tbl_id, [{"json": row}])
        print(f"inserted 1 row into {ds_id}.{tbl_id}")
    elif args.action == "query":
        rows = bq.query(args.sql)
        for r in rows:
            print(json.dumps(r))
    else:
        raise SystemExit(f"unknown bq action: {args.action}")
    return 0


def cmd_version(args):
    print(f"opengcp {__version__}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="opengcp",
                                description="Local reimplementation of core GCP primitives.")
    p.add_argument("--data-dir", default=None,
                   help="persistence directory (default: in-memory)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="start the local HTTP server")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8085)
    sp.set_defaults(func=cmd_serve)

    st = sub.add_parser("storage", help="object storage commands")
    st.add_argument("action", choices=["mb", "cp", "cat", "ls"])
    st.add_argument("target", nargs="?")
    st.add_argument("source", nargs="?")
    st.add_argument("dest", nargs="?")
    st.add_argument("prefix", nargs="?")
    st.set_defaults(func=cmd_storage)

    fs = sub.add_parser("fs", help="firestore document commands")
    fs.add_argument("action", choices=["set", "get"])
    fs.add_argument("collection")
    fs.add_argument("doc_id")
    fs.add_argument("data", nargs="?")
    fs.set_defaults(func=cmd_fs)

    pb = sub.add_parser("pubsub", help="pub/sub commands")
    pb.add_argument("action", choices=["publish"])
    pb.add_argument("topic")
    pb.add_argument("data")
    pb.set_defaults(func=cmd_pubsub)

    dst = sub.add_parser("datastore", help="Cloud Datastore-style entity commands")
    dst.add_argument("action", choices=["put", "get", "query"])
    dst.add_argument("kind")
    dst.add_argument("id", nargs="?")
    dst.add_argument("data", nargs="?")
    dst.add_argument("--gql", default=None, help="GQL query string for 'query' action")
    dst.set_defaults(func=cmd_datastore)

    bq = sub.add_parser("bq", help="BigQuery-lite commands")
    bq.add_argument("action", choices=["create-dataset", "create-table", "insert", "query"])
    bq.add_argument("dataset", nargs="?")
    bq.add_argument("table_ref", nargs="?", metavar="dataset.table")
    bq.add_argument("schema", nargs="?", help="JSON schema array for create-table")
    bq.add_argument("row_json", nargs="?", metavar="row_json", help="JSON row for insert")
    bq.add_argument("sql", nargs="?", help="SQL for query action")
    bq.set_defaults(func=cmd_bq)

    ver = sub.add_parser("version", help="print version")
    ver.set_defaults(func=cmd_version)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
