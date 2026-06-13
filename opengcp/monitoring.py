"""Cloud Monitoring-style time-series metrics store.

Implements a compatible SUBSET of the Cloud Monitoring API:
  * Metric descriptor registry (create / get / list / delete).
  * Write time-series data points (numeric: INT64 or DOUBLE; or STRING).
  * List time-series data — filter by metric type and time range; aggregate
    with ALIGN_MEAN / ALIGN_SUM / ALIGN_MIN / ALIGN_MAX over a period.

All data is stored in SQLite (in-memory by default).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class MonitoringError(Exception):
    pass


class MetricNotFound(MonitoringError):
    pass


class MonitoringService:
    """Thread-safe Cloud Monitoring emulator.

    ``path`` of None (default) uses an in-memory SQLite database.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        db_path = path or ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS metric_descriptors (
                    metric_type  TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    description  TEXT NOT NULL DEFAULT '',
                    value_type   TEXT NOT NULL DEFAULT 'DOUBLE',
                    metric_kind  TEXT NOT NULL DEFAULT 'GAUGE',
                    unit         TEXT NOT NULL DEFAULT '1',
                    labels       TEXT NOT NULL DEFAULT '[]',
                    created      REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS time_series (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_type  TEXT NOT NULL,
                    resource     TEXT NOT NULL DEFAULT '{}',
                    metric_labels TEXT NOT NULL DEFAULT '{}',
                    value_type   TEXT NOT NULL DEFAULT 'DOUBLE',
                    point_value  REAL,
                    point_str    TEXT,
                    start_time   REAL NOT NULL,
                    end_time     REAL NOT NULL,
                    insert_time  REAL NOT NULL
                );
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ts_metric ON time_series (metric_type)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ts_end ON time_series (end_time)")
            self._conn.commit()

    # ---- metric descriptors ----

    def create_metric_descriptor(self, metric_type: str, *,
                                  display_name: str = "",
                                  description: str = "",
                                  value_type: str = "DOUBLE",
                                  metric_kind: str = "GAUGE",
                                  unit: str = "1",
                                  labels: Optional[List[Dict[str, str]]] = None
                                  ) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM metric_descriptors WHERE metric_type=?",
                    (metric_type,)).fetchone():
                raise MonitoringError(
                    f"metric descriptor already exists: {metric_type}")
            self._conn.execute(
                "INSERT INTO metric_descriptors VALUES (?,?,?,?,?,?,?,?)",
                (metric_type, display_name, description,
                 value_type.upper(), metric_kind.upper(),
                 unit, json.dumps(labels or []), now))
            self._conn.commit()
        return self._descriptor_dict(metric_type, display_name, description,
                                     value_type, metric_kind, unit,
                                     labels or [])

    def get_metric_descriptor(self, metric_type: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT metric_type, display_name, description, value_type, "
                "metric_kind, unit, labels FROM metric_descriptors "
                "WHERE metric_type=?",
                (metric_type,)).fetchone()
        if row is None:
            raise MetricNotFound(metric_type)
        return self._descriptor_dict(row[0], row[1], row[2], row[3],
                                     row[4], row[5], json.loads(row[6]))

    def list_metric_descriptors(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT metric_type, display_name, description, value_type, "
                "metric_kind, unit, labels FROM metric_descriptors "
                "ORDER BY metric_type").fetchall()
        return [self._descriptor_dict(r[0], r[1], r[2], r[3], r[4], r[5],
                                       json.loads(r[6]))
                for r in rows]

    def delete_metric_descriptor(self, metric_type: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM metric_descriptors WHERE metric_type=?",
                (metric_type,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise MetricNotFound(metric_type)

    # ---- time-series write ----

    def write_time_series(self, time_series: List[Dict[str, Any]]) -> int:
        """Write a list of time-series objects.

        Each element:
          {
            "metric": {"type": "custom.googleapis.com/cpu", "labels": {}},
            "resource": {"type": "global", ...},
            "points": [
              {"interval": {"startTime": <float>, "endTime": <float>},
               "value": {"doubleValue": 1.5}}  # or int64Value / stringValue
            ]
          }
        """
        now = time.time()
        count = 0
        with self._lock:
            for ts in time_series:
                metric = ts.get("metric", {})
                metric_type = metric.get("type", "")
                metric_labels = metric.get("labels", {})
                resource = ts.get("resource", {})
                for point in ts.get("points", []):
                    interval = point.get("interval", {})
                    start = float(interval.get("startTime", now))
                    end = float(interval.get("endTime", now))
                    val = point.get("value", {})
                    if "doubleValue" in val:
                        pval = float(val["doubleValue"])
                        vtype = "DOUBLE"
                        pstr = None
                    elif "int64Value" in val:
                        pval = float(val["int64Value"])
                        vtype = "INT64"
                        pstr = None
                    elif "stringValue" in val:
                        pval = None
                        vtype = "STRING"
                        pstr = str(val["stringValue"])
                    else:
                        pval = 0.0
                        vtype = "DOUBLE"
                        pstr = None
                    self._conn.execute(
                        "INSERT INTO time_series "
                        "(metric_type, resource, metric_labels, value_type, "
                        "point_value, point_str, start_time, end_time, insert_time) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (metric_type, json.dumps(resource),
                         json.dumps(metric_labels), vtype,
                         pval, pstr, start, end, now))
                    count += 1
            self._conn.commit()
        return count

    # ---- time-series list ----

    def list_time_series(self, metric_type: Optional[str] = None,
                         start_time: Optional[float] = None,
                         end_time: Optional[float] = None,
                         aligner: str = "ALIGN_NONE",
                         alignment_period: Optional[float] = None,
                         page_size: int = 1000) -> List[Dict[str, Any]]:
        """List time-series data, optionally aligned.

        ``aligner``: ALIGN_NONE | ALIGN_MEAN | ALIGN_SUM | ALIGN_MIN | ALIGN_MAX
        ``alignment_period``: bucket width in seconds (required if aligner != NONE)
        """
        clauses = ["1=1"]
        params: List[Any] = []
        if metric_type:
            clauses.append("metric_type = ?")
            params.append(metric_type)
        if start_time is not None:
            clauses.append("end_time >= ?")
            params.append(float(start_time))
        if end_time is not None:
            clauses.append("end_time <= ?")
            params.append(float(end_time))

        sql = (f"SELECT metric_type, resource, metric_labels, value_type, "
               f"point_value, point_str, start_time, end_time "
               f"FROM time_series "
               f"WHERE {' AND '.join(clauses)} "
               f"ORDER BY end_time ASC LIMIT ?")
        params.append(page_size)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        raw = [_ts_row_to_dict(r) for r in rows]

        if aligner.upper() == "ALIGN_NONE" or not alignment_period:
            return raw

        return _align_series(raw, aligner.upper(), float(alignment_period))

    # ---- helpers ----

    @staticmethod
    def _descriptor_dict(metric_type: str, display_name: str,
                         description: str, value_type: str,
                         metric_kind: str, unit: str,
                         labels: list) -> Dict[str, Any]:
        return {
            "name": f"projects/local/metricDescriptors/{metric_type}",
            "type": metric_type,
            "displayName": display_name,
            "description": description,
            "valueType": value_type.upper(),
            "metricKind": metric_kind.upper(),
            "unit": unit,
            "labels": labels,
        }


def _ts_row_to_dict(row) -> Dict[str, Any]:
    vtype = row[3]
    if vtype == "STRING":
        val = {"stringValue": row[5]}
    elif vtype == "INT64":
        val = {"int64Value": int(row[4]) if row[4] is not None else 0}
    else:
        val = {"doubleValue": row[4] if row[4] is not None else 0.0}
    return {
        "metric": {"type": row[0], "labels": json.loads(row[2])},
        "resource": json.loads(row[1]),
        "valueType": vtype,
        "points": [{
            "interval": {"startTime": row[6], "endTime": row[7]},
            "value": val,
        }],
    }


def _align_series(raw: List[Dict[str, Any]], aligner: str,
                  period: float) -> List[Dict[str, Any]]:
    """Bucket numeric time-series points into ``period``-second buckets."""
    from collections import defaultdict

    # key: (metric_type, labels_json, resource_json, bucket_index)
    buckets: dict = defaultdict(list)
    for ts in raw:
        vtype = ts.get("valueType", "DOUBLE")
        if vtype == "STRING":
            continue
        for pt in ts.get("points", []):
            end = pt["interval"]["endTime"]
            bucket_idx = int(end // period)
            k = (ts["metric"]["type"],
                 json.dumps(ts["metric"].get("labels", {}), sort_keys=True),
                 json.dumps(ts["resource"], sort_keys=True))
            val = pt["value"].get("doubleValue") or float(
                pt["value"].get("int64Value", 0))
            buckets[(k, bucket_idx)].append((end, val, vtype))

    agg_map = {"ALIGN_MEAN": lambda vals: sum(vals) / len(vals),
               "ALIGN_SUM": sum,
               "ALIGN_MIN": min,
               "ALIGN_MAX": max}
    agg_fn = agg_map.get(aligner, lambda v: v[-1])

    results: List[Dict[str, Any]] = []
    for (k, bucket_idx), pts in sorted(buckets.items()):
        metric_type, labels_json, resource_json = k
        vals = [p[1] for p in pts]
        agg_val = agg_fn(vals)
        bucket_end = (bucket_idx + 1) * period
        bucket_start = bucket_idx * period
        vtype = pts[0][2]
        if vtype == "INT64":
            val_dict = {"int64Value": int(agg_val)}
        else:
            val_dict = {"doubleValue": agg_val}
        results.append({
            "metric": {"type": metric_type,
                       "labels": json.loads(labels_json)},
            "resource": json.loads(resource_json),
            "valueType": vtype,
            "points": [{
                "interval": {"startTime": bucket_start, "endTime": bucket_end},
                "value": val_dict,
            }],
        })
    return results
