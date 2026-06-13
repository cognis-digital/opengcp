"""Tests for Cloud Monitoring emulator (opengcp.monitoring)."""

import time

import pytest

from opengcp.monitoring import MonitoringService, MonitoringError, MetricNotFound


@pytest.fixture()
def mon():
    return MonitoringService()


# ----- metric descriptors -----

def test_create_and_get_descriptor(mon):
    desc = mon.create_metric_descriptor(
        "custom.googleapis.com/cpu_usage",
        display_name="CPU Usage",
        description="CPU utilization percentage",
        value_type="DOUBLE",
        unit="%",
    )
    assert desc["type"] == "custom.googleapis.com/cpu_usage"
    assert desc["unit"] == "%"
    fetched = mon.get_metric_descriptor("custom.googleapis.com/cpu_usage")
    assert fetched["type"] == "custom.googleapis.com/cpu_usage"


def test_create_duplicate_descriptor_fails(mon):
    mon.create_metric_descriptor("custom/dup")
    with pytest.raises(MonitoringError):
        mon.create_metric_descriptor("custom/dup")


def test_list_metric_descriptors(mon):
    mon.create_metric_descriptor("custom/a")
    mon.create_metric_descriptor("custom/b")
    descs = mon.list_metric_descriptors()
    types = [d["type"] for d in descs]
    assert "custom/a" in types
    assert "custom/b" in types


def test_get_nonexistent_descriptor(mon):
    with pytest.raises(MetricNotFound):
        mon.get_metric_descriptor("custom/nope")


def test_delete_descriptor(mon):
    mon.create_metric_descriptor("custom/todel")
    mon.delete_metric_descriptor("custom/todel")
    with pytest.raises(MetricNotFound):
        mon.get_metric_descriptor("custom/todel")


def test_delete_nonexistent_descriptor(mon):
    with pytest.raises(MetricNotFound):
        mon.delete_metric_descriptor("custom/ghost")


def test_descriptor_with_labels(mon):
    desc = mon.create_metric_descriptor(
        "custom/with_labels",
        labels=[{"key": "host", "valueType": "STRING"}],
    )
    assert desc["labels"][0]["key"] == "host"


# ----- write time-series -----

def test_write_double_time_series(mon):
    now = time.time()
    n = mon.write_time_series([{
        "metric": {"type": "custom/cpu", "labels": {"host": "server1"}},
        "resource": {"type": "global"},
        "points": [{
            "interval": {"startTime": now - 60, "endTime": now},
            "value": {"doubleValue": 42.5},
        }],
    }])
    assert n == 1


def test_write_int64_time_series(mon):
    now = time.time()
    n = mon.write_time_series([{
        "metric": {"type": "custom/requests"},
        "points": [{
            "interval": {"startTime": now, "endTime": now},
            "value": {"int64Value": 100},
        }],
    }])
    assert n == 1


def test_write_multiple_points(mon):
    now = time.time()
    n = mon.write_time_series([{
        "metric": {"type": "custom/multi"},
        "points": [
            {"interval": {"startTime": now - i, "endTime": now - i + 1},
             "value": {"doubleValue": float(i)}}
            for i in range(5)
        ],
    }])
    assert n == 5


# ----- list time-series -----

def test_list_time_series_by_metric_type(mon):
    now = time.time()
    mon.write_time_series([
        {
            "metric": {"type": "custom/filtered"},
            "points": [{"interval": {"startTime": now, "endTime": now},
                        "value": {"doubleValue": 1.0}}],
        },
        {
            "metric": {"type": "custom/other"},
            "points": [{"interval": {"startTime": now, "endTime": now},
                        "value": {"doubleValue": 2.0}}],
        },
    ])
    ts = mon.list_time_series(metric_type="custom/filtered")
    assert all(t["metric"]["type"] == "custom/filtered" for t in ts)


def test_list_time_series_time_range(mon):
    now = time.time()
    mon.write_time_series([{
        "metric": {"type": "custom/range"},
        "points": [{"interval": {"startTime": now - 300, "endTime": now - 300},
                    "value": {"doubleValue": 1.0}}],
    }])
    mon.write_time_series([{
        "metric": {"type": "custom/range"},
        "points": [{"interval": {"startTime": now, "endTime": now},
                    "value": {"doubleValue": 2.0}}],
    }])
    ts = mon.list_time_series(
        metric_type="custom/range",
        start_time=now - 10,
        end_time=now + 10,
    )
    # Only the recent point should be in range
    assert len(ts) == 1


# ----- alignment -----

def test_align_mean(mon):
    now = time.time()
    base = float(int(now // 60) * 60)  # align to minute boundary
    points = []
    for i in range(4):
        points.append({
            "interval": {"startTime": base + i, "endTime": base + i + 1},
            "value": {"doubleValue": float(i + 1)},  # 1,2,3,4 → mean 2.5
        })
    mon.write_time_series([{"metric": {"type": "custom/align_mean"}, "points": points}])
    ts = mon.list_time_series(
        metric_type="custom/align_mean",
        aligner="ALIGN_MEAN",
        alignment_period=60.0,
    )
    assert len(ts) >= 1
    val = ts[0]["points"][0]["value"]["doubleValue"]
    assert abs(val - 2.5) < 0.01


def test_align_sum(mon):
    now = time.time()
    base = float(int(now // 60) * 60)
    mon.write_time_series([{"metric": {"type": "custom/align_sum"}, "points": [
        {"interval": {"startTime": base + i, "endTime": base + i + 1},
         "value": {"doubleValue": 10.0}}
        for i in range(3)
    ]}])
    ts = mon.list_time_series(
        metric_type="custom/align_sum",
        aligner="ALIGN_SUM",
        alignment_period=60.0,
    )
    val = ts[0]["points"][0]["value"]["doubleValue"]
    assert abs(val - 30.0) < 0.01
