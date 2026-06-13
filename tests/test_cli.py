"""Exercise the CLI plumbing without starting a long-lived server."""

import json

from opengcp import cli


def test_version(capsys):
    rc = cli.main(["version"])
    out = capsys.readouterr().out
    assert rc == 0 and "opengcp" in out


def test_storage_roundtrip_cli(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    src = tmp_path / "in.txt"
    src.write_bytes(b"cli-bytes")
    # make bucket
    cli.main(["--data-dir", data_dir, "storage", "mb", "bkt"])
    # cp file -> bkt/key
    cli.main(["--data-dir", data_dir, "storage", "cp", str(src), "bkt/key"])
    capsys.readouterr()
    # ls
    cli.main(["--data-dir", data_dir, "storage", "ls", "bkt"])
    out = capsys.readouterr().out
    assert "key" in out
    # cat
    cli.main(["--data-dir", data_dir, "storage", "cat", "bkt/key"])
    captured = capsys.readouterr().out
    assert "cli-bytes" in captured


def test_firestore_cli(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    cli.main(["--data-dir", data_dir, "fs", "set", "c", "d", json.dumps({"a": 1})])
    capsys.readouterr()
    cli.main(["--data-dir", data_dir, "fs", "get", "c", "d"])
    out = capsys.readouterr().out
    assert json.loads(out) == {"a": 1}


def test_pubsub_cli(capsys):
    rc = cli.main(["pubsub", "publish", "t", "hello"])
    out = capsys.readouterr().out
    assert rc == 0 and "published" in out
