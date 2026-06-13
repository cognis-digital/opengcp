"""Command-line interface for opengcp.

Subcommands:
  serve                          start the local HTTP server (all services)
  storage mb <bucket>            make a bucket
  storage cp <file> <b/name>     upload a file to <bucket>/<name>
  storage cat <b/name>           print an object to stdout
  storage ls <bucket> [prefix]   list objects
  fs set <coll> <id> <json>      set a document
  fs get <coll> <id>             get a document
  pubsub publish <topic> <data>  publish a message
  version                        print version
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
    print("services: storage, firestore, pubsub, functions")
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
        items = s.list_objects(args.target, args.prefix or "")
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

    ver = sub.add_parser("version", help="print version")
    ver.set_defaults(func=cmd_version)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
