"""Data-reading SQL functions that must never run under a read-only policy.

These functions read bytes from *outside* the database's own tables: local
files, object storage, URLs, or other database servers. Each is a distinct
escape from whatever access control the warehouse enforces:

* ``read_csv('/etc/passwd')``           -- arbitrary local file read
* ``read_parquet('s3://other-bucket')`` -- SSRF / cross-account access
* ``dblink(...)`` / ``postgres_scan``   -- lateral movement to another server
* ``url(...)``                          -- outbound fetch, i.e. exfiltration

They are blocked in **every** syntactic position, not just ``FROM``/``JOIN``.
A reader hidden in a projection (``SELECT read_csv('/etc/passwd')``), inside a
``WHERE`` subquery, or nested in another call (``UNNEST(read_csv(...))``) is
just as dangerous as one in the source slot, and a source-position-only check
is trivially bypassed by all three.

**This is a blocklist, and blocklists are not complete.** A reader that is not
named here will pass. Treat it as one layer:

1.  The database credential should be read-only with no filesystem privileges.
2.  Table references are separately allowlisted against the schema catalog when
    ``require_catalog_tables`` is enabled -- that check *is* fail-closed.
3.  This list is the backstop for the positions an allowlist cannot cover.

Deployments needing extra coverage should add names to
``SqlPolicy.denied_functions`` rather than editing this module, so the baseline
stays upgradeable.
"""

from __future__ import annotations

from typing import FrozenSet

#: Functions that read data from outside the database's own tables.
DATA_READER_FUNCTIONS: FrozenSet[str] = frozenset(
    {
        # -- DuckDB file readers ------------------------------------------
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_json_objects",
        "read_text",
        "read_blob",
        "read_xlsx",
        "parquet_scan",
        "glob",
        # -- DuckDB extension scanners (lakehouse / external databases) ----
        "iceberg_scan",
        "delta_scan",
        "postgres_scan",
        "postgres_query",
        "mysql_scan",
        "mysql_query",
        "sqlite_scan",
        "sqlite_query",
        # -- DuckDB spatial / sniffing / metadata over arbitrary paths -----
        "sniff_csv",
        "st_read",
        "st_readosm",
        "st_read_meta",
        "parquet_metadata",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "parquet_schema",
        "iceberg_metadata",
        "iceberg_snapshots",
        # -- PostgreSQL file / remote readers ------------------------------
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_ls_tmpdir",
        "pg_ls_archive_statusdir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_get",
        "dblink",
        "dblink_exec",
        # -- MySQL --------------------------------------------------------
        "load_file",
        # -- SQLite (fileio extension) -------------------------------------
        # Ships with the sqlite3 CLI and is loadable at runtime, so it must be
        # blocked even though a default library build lacks it.
        "readfile",
        "writefile",
        "fsdir",
        "lsmode",
        "load_extension",
        # -- ClickHouse table functions ------------------------------------
        # ClickHouse exposes remote reads as ordinary functions, making this
        # one of the widest surfaces of any engine.
        "file",
        "s3",
        "s3cluster",
        "hdfs",
        "hdfscluster",
        "remote",
        "remotesecure",
        "mysql",
        "postgresql",
        "mongodb",
        "jdbc",
        "odbc",
        "azureblobstorage",
        "deltalake",
        "hudi",
        "iceberg",
        # -- Generic outbound fetch ---------------------------------------
        "url",
        "urlcluster",
    }
)

#: Synthetic row generators. Not an exfiltration risk -- they read nothing --
#: but an unbounded range is a denial-of-service vector: ``generate_series(1,
#: 1e12)`` materialises a trillion rows. Blocked in source position by default;
#: an operator can opt individual names back in via
#: ``SqlPolicy.allowed_source_functions``.
GENERATOR_FUNCTIONS: FrozenSet[str] = frozenset(
    {"generate_series", "sequence", "range"}
)

#: Row-expansion operators. These restructure an array or struct that is
#: *already* in query scope (e.g. an ``orders.items`` column), so they never
#: reach outside the manifest and are always allowed as a source. Note that
#: ``UNNEST(read_csv(...))`` is still rejected -- the reader scan walks every
#: position and sees the inner call before this allowance is consulted.
ROW_EXPANSION_FUNCTIONS: FrozenSet[str] = frozenset(
    {"unnest", "explode", "flatten", "posexplode", "inline"}
)
