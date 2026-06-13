"""Tests for Cloud Scheduler emulator (opengcp.scheduler)."""

import datetime
import time

import pytest

from opengcp.scheduler import (CloudScheduler, SchedulerError, JobNotFound,
                               parse_cron, _cron_matches_simple)


# ----- cron parser -----

def test_parse_every_minute():
    cron = parse_cron("* * * * *")
    assert len(cron) == 5
    assert 0 in cron[0]   # minutes includes 0
    assert 59 in cron[0]  # and 59


def test_parse_specific_fields():
    cron = parse_cron("30 9 * * 1")  # 09:30 on Mondays
    assert 30 in cron[0]
    assert 9 in cron[1]
    assert 1 in cron[4]  # Monday = 1 in cron (Sun=0)


def test_parse_step():
    cron = parse_cron("*/15 * * * *")
    assert {0, 15, 30, 45}.issubset(cron[0])
    assert 1 not in cron[0]


def test_parse_range():
    cron = parse_cron("0 9-17 * * *")
    assert set(range(9, 18)) == cron[1]


def test_parse_list():
    cron = parse_cron("0 0 1,15 * *")
    assert {1, 15} == cron[2]


def test_parse_alias_daily():
    cron = parse_cron("@daily")
    assert 0 in cron[0] and 0 in cron[1]  # minute=0, hour=0


def test_parse_alias_hourly():
    cron = parse_cron("@hourly")
    # minute=0, any hour
    assert cron[0] == {0}
    assert len(cron[1]) == 24


def test_parse_alias_weekly():
    cron = parse_cron("@weekly")
    assert cron[0] == {0} and cron[1] == {0}


def test_parse_alias_monthly():
    cron = parse_cron("@monthly")
    assert cron[2] == {1}  # day 1


def test_parse_invalid_raises():
    with pytest.raises(SchedulerError):
        parse_cron("* * * *")   # only 4 fields


def test_cron_matches_simple():
    dt = datetime.datetime(2026, 6, 13, 9, 30, 0)  # Saturday 09:30
    # every 30 minutes at 9:30 or 9:00
    cron = parse_cron("30 9 * * *")
    assert _cron_matches_simple(cron, dt) is True
    cron2 = parse_cron("0 9 * * *")
    assert _cron_matches_simple(cron2, dt) is False


# ----- scheduler CRUD -----

def test_create_and_list_jobs():
    sc = CloudScheduler()
    sc.create_job("job1", "* * * * *")
    sc.create_job("job2", "@hourly")
    jobs = sc.list_jobs()
    assert [j["name"] for j in jobs] == ["job1", "job2"]
    sc.stop()


def test_create_job_duplicate_raises():
    sc = CloudScheduler()
    sc.create_job("j", "* * * * *")
    with pytest.raises(SchedulerError):
        sc.create_job("j", "* * * * *")
    sc.stop()


def test_create_job_invalid_schedule():
    sc = CloudScheduler()
    with pytest.raises(SchedulerError):
        sc.create_job("j", "bad schedule here")
    sc.stop()


def test_get_job():
    sc = CloudScheduler()
    sc.create_job("j", "@daily", description="runs daily")
    job = sc.get_job("j")
    assert job.description == "runs daily"
    d = job.to_dict()
    assert d["schedule"] == "@daily"
    sc.stop()


def test_get_job_not_found():
    sc = CloudScheduler()
    with pytest.raises(JobNotFound):
        sc.get_job("nope")
    sc.stop()


def test_delete_job():
    sc = CloudScheduler()
    sc.create_job("j", "* * * * *")
    sc.delete_job("j")
    with pytest.raises(JobNotFound):
        sc.get_job("j")
    sc.stop()


def test_pause_and_resume_job():
    sc = CloudScheduler()
    sc.create_job("j", "* * * * *")
    sc.pause_job("j")
    assert sc.get_job("j").state == "PAUSED"
    sc.resume_job("j")
    assert sc.get_job("j").state == "ENABLED"
    sc.stop()


# ----- run_now and history -----

def test_run_now_fires_handler():
    sc = CloudScheduler()
    fired = []
    sc.create_job("j", "* * * * *", handler=lambda d: fired.append(d["name"]))
    sc.run_now("j")
    # run_now is synchronous in spirit but dispatches in a thread; wait briefly
    deadline = time.time() + 2.0
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    assert fired == ["j"]
    sc.stop()


def test_run_now_records_history():
    sc = CloudScheduler()
    sc.create_job("j", "* * * * *", handler=lambda d: None)
    sc.run_now("j")
    deadline = time.time() + 2.0
    while not sc.job_history("j") and time.time() < deadline:
        time.sleep(0.05)
    history = sc.job_history("j")
    assert len(history) >= 1
    assert history[0]["ok"] is True
    sc.stop()


def test_run_now_captures_error():
    sc = CloudScheduler()

    def boom(d):
        raise ValueError("oops")

    sc.create_job("j", "@daily", handler=boom)
    sc.run_now("j")

    deadline = time.time() + 2.0
    while not sc.job_history("j") and time.time() < deadline:
        time.sleep(0.05)

    history = sc.job_history("j")
    assert history[0]["ok"] is False
    assert "oops" in history[0]["error"]
    sc.stop()


def test_register_handler_after_create():
    sc = CloudScheduler()
    sc.create_job("j", "* * * * *")
    fired = []
    sc.register_handler("j", lambda d: fired.append(True))
    sc.run_now("j")
    deadline = time.time() + 2.0
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    assert fired
    sc.stop()


def test_run_now_updates_last_attempt():
    sc = CloudScheduler()
    sc.create_job("j", "@hourly", handler=lambda d: None)
    sc.run_now("j")
    deadline = time.time() + 2.0
    while sc.get_job("j").last_attempt_time is None and time.time() < deadline:
        time.sleep(0.05)
    job = sc.get_job("j")
    assert job.last_attempt_time is not None
    assert job.last_attempt_status == "SUCCESS"
    sc.stop()


def test_paused_job_skipped_by_run_now():
    """run_now still fires even if paused (manual override)."""
    sc = CloudScheduler()
    fired = []
    sc.create_job("j", "* * * * *", handler=lambda d: fired.append(True))
    sc.pause_job("j")
    sc.run_now("j")   # manual trigger should still work
    deadline = time.time() + 2.0
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    assert fired
    sc.stop()
