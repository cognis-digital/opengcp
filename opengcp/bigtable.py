"""Cloud Bigtable-lite: instances, tables, column families, row read/write/scan.

Implements a compatible SUBSET of the Cloud Bigtable data model:

  - **Instances** contain tables.
  - **Tables** contain column families (each is a namespace for column
    qualifiers) and rows (addressed by a row key string).
  - **Column families** have an optional max-versions setting (default
    unlimited).  Older cell versions are pruned to ``max_versions`` on every
    write — identical to a MaxVersionsGCRule.
  - **Cells** have a value (bytes) and a timestamp (int microseconds since
    epoch; auto-assigned when not provided).
  - **Read operations**
      - ``read_row``  — return all or a filtered set of cells for one row key.
      - ``scan_rows`` — iterate rows in lexicographic key range
        ``[start_key, end_key)`` with optional prefix matching.
      - ``read_column`` — return all rows that have a value in a given column.
  - **Write operations**
      - ``mutate_row``  — apply a list of mutations (set-cell, delete-cell,
        delete-from-family, delete-from-row) to a single row atomically.
  - No server-side filters beyond what is listed above; no replication; no
    read-modify-write (CheckAndMutate). In-memory only for now (no SQLite
    persistence — matching the spirit of Bigtable's very different on-disk
    format).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class BigtableError(Exception):
    """Base class for Bigtable errors."""


class InstanceNotFound(BigtableError):
    pass


class TableNotFound(BigtableError):
    pass


class ColumnFamilyNotFound(BigtableError):
    pass


# ---------------------------------------------------------------------------
# Mutation types
# ---------------------------------------------------------------------------

@dataclass
class SetCell:
    family: str
    qualifier: str
    value: bytes
    timestamp_micros: Optional[int] = None  # None → auto


@dataclass
class DeleteCell:
    family: str
    qualifier: str
    timestamp_micros: Optional[int] = None  # None → delete all versions


@dataclass
class DeleteFromFamily:
    family: str


@dataclass
class DeleteFromRow:
    pass


Mutation = Any  # Union[SetCell, DeleteCell, DeleteFromFamily, DeleteFromRow]


# ---------------------------------------------------------------------------
# Internal cell storage
# ---------------------------------------------------------------------------

# A Cell is (timestamp_micros, value).
Cell = Tuple[int, bytes]

# RowData: family -> qualifier -> sorted list of Cell (newest first)
RowData = Dict[str, Dict[str, List[Cell]]]


def _now_micros() -> int:
    return int(time.time() * 1_000_000)


def _row_to_dict(row_data: RowData) -> dict:
    out: dict = {}
    for fam, cols in row_data.items():
        out[fam] = {}
        for qual, cells in cols.items():
            out[fam][qual] = [{"timestampMicros": ts, "value": v.decode("utf-8", "replace")}
                              for ts, v in cells]
    return out


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

@dataclass
class ColumnFamily:
    name: str
    max_versions: int = 0  # 0 = unlimited


class Table:
    """A single Bigtable table with column families and in-memory row store."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.RLock()
        self._families: Dict[str, ColumnFamily] = {}
        # row_key -> RowData
        self._rows: Dict[str, RowData] = {}

    # ----- column family operations -----

    def create_column_family(self, name: str, max_versions: int = 0) -> ColumnFamily:
        with self._lock:
            if name in self._families:
                raise BigtableError(f"column family already exists: {name}")
            cf = ColumnFamily(name=name, max_versions=max_versions)
            self._families[name] = cf
            return cf

    def get_column_family(self, name: str) -> ColumnFamily:
        with self._lock:
            if name not in self._families:
                raise ColumnFamilyNotFound(name)
            return self._families[name]

    def delete_column_family(self, name: str) -> None:
        with self._lock:
            if name not in self._families:
                raise ColumnFamilyNotFound(name)
            del self._families[name]
            # also remove all data for this family from every row
            for row_data in self._rows.values():
                row_data.pop(name, None)

    def list_column_families(self) -> List[ColumnFamily]:
        with self._lock:
            return list(self._families.values())

    # ----- mutation helpers -----

    def _prune_versions(self, fam: str, qual: str, cells: List[Cell]) -> None:
        """In-place prune oldest cells if max_versions is set."""
        cf = self._families.get(fam)
        if cf and cf.max_versions > 0:
            del cells[cf.max_versions:]

    # ----- write operations -----

    def mutate_row(self, row_key: str, mutations: List[Mutation]) -> None:
        """Apply mutations atomically to a single row."""
        with self._lock:
            row_data: RowData = self._rows.setdefault(row_key, {})
            for mut in mutations:
                if isinstance(mut, SetCell):
                    if mut.family not in self._families:
                        raise ColumnFamilyNotFound(mut.family)
                    ts = mut.timestamp_micros if mut.timestamp_micros is not None else _now_micros()
                    fam_data = row_data.setdefault(mut.family, {})
                    cells = fam_data.setdefault(mut.qualifier, [])
                    # insert in descending timestamp order
                    cells.append((ts, mut.value if isinstance(mut.value, bytes)
                                  else str(mut.value).encode("utf-8")))
                    cells.sort(key=lambda c: c[0], reverse=True)
                    self._prune_versions(mut.family, mut.qualifier, cells)

                elif isinstance(mut, DeleteCell):
                    fam_data = row_data.get(mut.family, {})
                    if mut.qualifier in fam_data:
                        if mut.timestamp_micros is None:
                            del fam_data[mut.qualifier]
                        else:
                            remaining = [
                                c for c in fam_data[mut.qualifier]
                                if c[0] != mut.timestamp_micros
                            ]
                            if remaining:
                                fam_data[mut.qualifier] = remaining
                            else:
                                del fam_data[mut.qualifier]
                    # prune empty family dict
                    if mut.family in row_data and not row_data[mut.family]:
                        del row_data[mut.family]

                elif isinstance(mut, DeleteFromFamily):
                    row_data.pop(mut.family, None)

                elif isinstance(mut, DeleteFromRow):
                    row_data.clear()

            # remove empty row
            if not any(row_data.values()):
                self._rows.pop(row_key, None)

    # ----- read operations -----

    def read_row(self, row_key: str,
                 families: Optional[List[str]] = None) -> Optional[dict]:
        """Return row as a dict or None if the row does not exist.

        ``families`` limits which column families are returned.
        """
        with self._lock:
            row_data = self._rows.get(row_key)
            if row_data is None:
                return None
            if families:
                filtered = {f: cols for f, cols in row_data.items() if f in families}
            else:
                filtered = dict(row_data)
            return {
                "rowKey": row_key,
                "families": _row_to_dict(filtered),
            }

    def scan_rows(self, start_key: str = "", end_key: str = "",
                  prefix: str = "",
                  limit: int = 0,
                  families: Optional[List[str]] = None) -> List[dict]:
        """Return rows in lexicographic key order.

        If ``prefix`` is set, only rows whose key starts with it are returned
        (equivalent to a prefix scan). Otherwise ``[start_key, end_key)`` is
        used (empty end_key means unbounded end).
        """
        with self._lock:
            keys = sorted(self._rows.keys())
        results = []
        for key in keys:
            if prefix:
                if not key.startswith(prefix):
                    continue
            else:
                if start_key and key < start_key:
                    continue
                if end_key and key >= end_key:
                    continue
            row = self.read_row(key, families=families)
            if row:
                results.append(row)
            if limit and len(results) >= limit:
                break
        return results

    def read_column(self, family: str, qualifier: str) -> List[dict]:
        """Return all rows that have at least one cell in family:qualifier."""
        with self._lock:
            keys = sorted(self._rows.keys())
        results = []
        for key in keys:
            row = self.read_row(key, families=[family])
            if row and family in row["families"] and qualifier in row["families"][family]:
                results.append(row)
        return results

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "columnFamilies": [
                    {"name": cf.name, "maxVersions": cf.max_versions}
                    for cf in self._families.values()
                ],
            }


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

class Instance:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.RLock()
        self._tables: Dict[str, Table] = {}

    def create_table(self, table_id: str) -> Table:
        with self._lock:
            if table_id in self._tables:
                raise BigtableError(f"table already exists: {table_id}")
            t = Table(table_id)
            self._tables[table_id] = t
            return t

    def get_table(self, table_id: str) -> Table:
        with self._lock:
            if table_id not in self._tables:
                raise TableNotFound(table_id)
            return self._tables[table_id]

    def delete_table(self, table_id: str) -> None:
        with self._lock:
            if table_id not in self._tables:
                raise TableNotFound(table_id)
            del self._tables[table_id]

    def list_tables(self) -> List[str]:
        with self._lock:
            return sorted(self._tables.keys())

    def to_dict(self) -> dict:
        with self._lock:
            return {"name": self.name, "tables": self.list_tables()}


# ---------------------------------------------------------------------------
# BigtableAdmin
# ---------------------------------------------------------------------------

class BigtableAdmin:
    """Top-level Bigtable admin: manages instances."""

    def __init__(self):
        self._lock = threading.RLock()
        self._instances: Dict[str, Instance] = {}

    def create_instance(self, instance_id: str) -> Instance:
        with self._lock:
            if instance_id in self._instances:
                raise BigtableError(f"instance already exists: {instance_id}")
            inst = Instance(instance_id)
            self._instances[instance_id] = inst
            return inst

    def get_instance(self, instance_id: str) -> Instance:
        with self._lock:
            if instance_id not in self._instances:
                raise InstanceNotFound(instance_id)
            return self._instances[instance_id]

    def delete_instance(self, instance_id: str) -> None:
        with self._lock:
            if instance_id not in self._instances:
                raise InstanceNotFound(instance_id)
            del self._instances[instance_id]

    def list_instances(self) -> List[str]:
        with self._lock:
            return sorted(self._instances.keys())
