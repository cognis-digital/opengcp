"""Tests for Cloud Logging emulator (opengcp.logging_service)."""

import time

import pytest

from opengcp.logging_service import LoggingService, SEVERITY_MAP


@pytest.fixture()
def lg():
    return LoggingService()


# ----- write -----

def test_write_single_entry(lg):
    ids = lg.write_entries([{
        "logName": "projects/local/logs/app",
        "severity": "INFO",
        "jsonPayload": {"msg": "hello"},
    }])
    assert len(ids) == 1


def test_write_multiple_entries(lg):
    entries = [
        {"logName": "projects/local/logs/app", "severity": "DEBUG",
         "jsonPayload": {"n": i}}
        for i in range(5)
    ]
    ids = lg.write_entries(entries)
    assert len(ids) == 5


def test_write_with_log_name_override(lg):
    ids = lg.write_entries(
        [{"severity": "INFO", "jsonPayload": {"x": 1}}],
        log_name="projects/local/logs/override",
    )
    entries = lg.list_entries(log_name="projects/local/logs/override")
    assert len(entries) == 1


def test_text_payload(lg):
    lg.write_entries([{
        "logName": "projects/local/logs/text",
        "severity": "WARNING",
        "textPayload": "plain text log",
    }])
    entries = lg.list_entries(log_name="projects/local/logs/text")
    assert entries[0]["jsonPayload"] == "plain text log"


# ----- list / filter -----

def test_list_all_entries(lg):
    lg.write_entries([{"logName": "logs/a", "severity": "INFO",
                       "jsonPayload": {}}] * 3)
    entries = lg.list_entries()
    assert len(entries) >= 3


def test_filter_by_log_name(lg):
    lg.write_entries([{"logName": "logs/app1", "jsonPayload": {}}])
    lg.write_entries([{"logName": "logs/app2", "jsonPayload": {}}])
    entries = lg.list_entries(log_name="logs/app1")
    assert all(e["logName"] == "logs/app1" for e in entries)


def test_filter_by_severity_min(lg):
    lg.write_entries([
        {"logName": "logs/sev", "severity": "DEBUG", "jsonPayload": {}},
        {"logName": "logs/sev", "severity": "ERROR", "jsonPayload": {}},
    ])
    entries = lg.list_entries(log_name="logs/sev", severity_min="WARNING")
    for e in entries:
        assert e["severity"] >= SEVERITY_MAP["WARNING"]


def test_filter_by_log_name_in_filter_expr(lg):
    lg.write_entries([
        {"logName": "logs/foo", "severity": "INFO", "jsonPayload": {"v": 1}},
        {"logName": "logs/bar", "severity": "INFO", "jsonPayload": {"v": 2}},
    ])
    entries = lg.list_entries(filter_expr='logName = "logs/foo"')
    assert all(e["logName"] == "logs/foo" for e in entries)


def test_filter_severity_in_filter_expr(lg):
    lg.write_entries([
        {"logName": "logs/x", "severity": "INFO", "jsonPayload": {}},
        {"logName": "logs/x", "severity": "CRITICAL", "jsonPayload": {}},
    ])
    entries = lg.list_entries(filter_expr="severity >= ERROR")
    for e in entries:
        assert e["severity"] >= SEVERITY_MAP["ERROR"]


def test_labels_filter(lg):
    lg.write_entries([{
        "logName": "logs/labeled",
        "severity": "INFO",
        "labels": {"service": "api"},
        "jsonPayload": {},
    }])
    entries = lg.list_entries(filter_expr='labels.service = "api"')
    assert len(entries) >= 1


# ----- tail -----

def test_tail_returns_recent(lg):
    for i in range(5):
        lg.write_entries([{"logName": "logs/tail", "jsonPayload": {"i": i}}])
    tail = lg.tail(3)
    assert len(tail) == 3


# ----- log names -----

def test_list_log_names(lg):
    lg.write_entries([{"logName": "logs/alpha", "jsonPayload": {}}])
    lg.write_entries([{"logName": "logs/beta", "jsonPayload": {}}])
    names = lg.list_log_names()
    assert "logs/alpha" in names
    assert "logs/beta" in names


# ----- delete -----

def test_delete_log(lg):
    lg.write_entries([{"logName": "logs/todel", "jsonPayload": {}}] * 3)
    n = lg.delete_log("logs/todel")
    assert n == 3
    entries = lg.list_entries(log_name="logs/todel")
    assert entries == []


def test_delete_nonexistent_log(lg):
    n = lg.delete_log("logs/ghost")
    assert n == 0
