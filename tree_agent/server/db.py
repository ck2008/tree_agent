"""SQLite connection factory, writer queue and migration runner.

SQLite allows many concurrent readers but only one writer. Rather than let a
20 MiB attachment commit and a chatty streaming turn fight over the write lock
and hand `database is locked` back to users, every write in the process goes
through one FIFO queue served by a single thread that owns the only write
connection. Reads use per-thread connections and never queue.
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .errors import ServiceError, StorageBusy

MIGRATION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# Applied to every connection, read or write. `synchronous = FULL` costs a
# little throughput and is the reason a power cut cannot lose a committed
# message; `busy_timeout` is the last-resort backstop under the writer queue.
CONNECTION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA busy_timeout = 10000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA wal_autocheckpoint = 1000",
)

# FTS5 and RETURNING are both load-bearing; a build without them would fail far
# from the cause, so the check happens once at startup.
MIN_SQLITE_VERSION = (3, 35, 0)


class DatabaseError(ServiceError):
    code = "database_error"


def _is_network_path(path: str) -> bool:
    """True for a UNC path or a mapped network drive.

    The whole point of this architecture is that the database lives on the
    service host's own disk. SQLite's locking is not reliable over SMB, and a
    NAS-hosted file shared between desktops is exactly the failure mode the
    spec forbids.
    """
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\") or absolute.startswith("//"):
        return True
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(absolute)[0]
    if not drive:
        return False
    try:
        import ctypes

        # 4 == DRIVE_REMOTE
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4
    except Exception:  # pragma: no cover - non-Windows or restricted host
        return False


def connect(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open one connection with the standard pragmas applied."""
    conn = sqlite3.connect(
        path,
        timeout=10.0,
        # The writer thread owns its connection for the process lifetime; reader
        # connections are thread-local. Neither is shared across threads, but
        # sqlite3's own check is too coarse to express that.
        check_same_thread=False,
        isolation_level=None,  # transactions are explicit, never implicit
    )
    conn.row_factory = sqlite3.Row
    for pragma in CONNECTION_PRAGMAS:
        if read_only and pragma.startswith("PRAGMA journal_mode"):
            continue
        conn.execute(pragma)
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


class WriterQueue:
    """One thread, one write connection, strict FIFO.

    A job is a callable taking the connection. It runs inside
    `BEGIN IMMEDIATE` … `COMMIT`; any exception rolls the whole job back. A job
    that loses the write lock to another *process* is retried from the start,
    so jobs must not have side effects outside the database.
    """

    LOCK_RETRIES = 5
    LOCK_BACKOFF = 0.05

    def __init__(self, path: str) -> None:
        self._path = path
        self._jobs: queue.Queue = queue.Queue()
        self._conn: sqlite3.Connection | None = None
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="tree-agent-writer", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------ internals

    def _serve(self) -> None:
        self._conn = connect(self._path)
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, result, label, wrap = job
            try:
                result["value"] = self._run(fn) if wrap else fn(self._conn)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                result["error"] = exc
                result["label"] = label
            finally:
                result["done"].set()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        assert self._conn is not None
        for attempt in range(self.LOCK_RETRIES):
            try:
                # IMMEDIATE takes the write lock up front. A deferred
                # transaction that upgrades halfway through can fail with
                # SQLITE_BUSY after doing work, which is far harder to retry.
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                time.sleep(self.LOCK_BACKOFF * (attempt + 1))
                continue
            try:
                value = fn(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            try:
                self._conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                self._conn.execute("ROLLBACK")
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                time.sleep(self.LOCK_BACKOFF * (attempt + 1))
                continue
            return value
        raise StorageBusy("資料庫忙碌，請稍後再試")

    # --------------------------------------------------------------- public

    def submit(
        self,
        fn: Callable[[sqlite3.Connection], Any],
        label: str = "",
        *,
        wrap: bool = True,
    ) -> Any:
        """Run `fn` in the writer thread and return its result, or re-raise.

        `wrap=False` skips the surrounding transaction for the one caller that
        has to drive its own — the migration runner, whose `executescript` ends
        any transaction it finds open.
        """
        if self._closed.is_set():
            raise DatabaseError("寫入佇列已關閉")
        result: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((fn, result, label, wrap))
        result["done"].wait()
        if "error" in result:
            raise result["error"]
        return result["value"]

    @property
    def depth(self) -> int:
        """Pending jobs — worth exporting as a health metric (spec §10)."""
        return self._jobs.qsize()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._jobs.put(None)
        self._thread.join(timeout=15)


class Database:
    """The process-wide handle: readers, the writer queue, and migrations."""

    def __init__(self, path: str, *, allow_network_path: bool = False) -> None:
        if path != ":memory:":
            path = os.path.abspath(path)
            if _is_network_path(path) and not allow_network_path:
                raise DatabaseError(
                    f"資料庫不可放在網路磁碟或 UNC 路徑上：{path}。"
                    "請改用服務主機的本機磁碟。"
                )
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._check_sqlite_build()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._writer = WriterQueue(path)

    @staticmethod
    def _check_sqlite_build() -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise DatabaseError(
                f"需要 SQLite {'.'.join(map(str, MIN_SQLITE_VERSION))} 以上，"
                f"目前為 {sqlite3.sqlite_version}"
            )
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            probe.execute("CREATE TABLE r (x)")
            probe.execute("INSERT INTO r VALUES (1) RETURNING x").fetchone()
        except sqlite3.OperationalError as exc:
            raise DatabaseError(f"此 SQLite 版本缺少必要功能（FTS5 / RETURNING）：{exc}")
        finally:
            probe.close()

    # ---------------------------------------------------------------- reads

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot for the duration of the block.

        Keep these short: an open read transaction stops WAL checkpointing, and
        a 100 GiB database with a runaway `-wal` file is how this design fails.
        """
        conn = self._reader()
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")  # read-only; nothing to commit

    # --------------------------------------------------------------- writes

    def write(self, fn: Callable[[sqlite3.Connection], Any], label: str = "") -> Any:
        """Queue `fn` as one transaction. Blocks until it commits or fails."""
        return self._writer.submit(fn, label)

    @property
    def writer_depth(self) -> int:
        return self._writer.depth

    # ----------------------------------------------------------- migrations

    def migrate(self) -> list[str]:
        """Apply pending `NNNN_name.sql` files in order. Returns what ran."""
        files = sorted(
            name
            for name in os.listdir(MIGRATION_DIR)
            if name.endswith(".sql") and name[:4].isdigit()
        )
        applied: list[str] = []

        def run(conn: sqlite3.Connection) -> list[str]:
            bootstrapped = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='schema_migrations'"
            ).fetchone()
            done = (
                {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
                if bootstrapped
                else set()
            )
            for name in files:
                version = int(name[:4])
                if version in done:
                    continue
                with open(os.path.join(MIGRATION_DIR, name), encoding="utf-8") as fh:
                    script = fh.read()
                # `executescript` commits whatever is open before it starts, so
                # the transaction has to be opened *inside* the script for the
                # file to land all-or-nothing.
                try:
                    conn.executescript("BEGIN IMMEDIATE;\n" + script)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at_ms)"
                        " VALUES (?, ?, ?)",
                        (version, name, int(time.time() * 1000)),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                applied.append(name)
            return applied

        # Each file is its own transaction (see above), so this job drives its
        # own commits rather than running inside the queue's.
        return self._writer.submit(run, label="migrate", wrap=False)

    # ------------------------------------------------------------ operations

    def integrity_check(self) -> str:
        with self.read() as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0]

    def backup(self, destination: str) -> str:
        """A consistent copy via SQLite's online backup API (spec §10).

        Runs on a reader, so it does not block writes; the copy is a single
        point-in-time snapshot rather than a file that changed while it copied.
        """
        destination = os.path.abspath(destination)
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self._reader().backup(target)
        finally:
            target.close()
        return destination

    def close(self) -> None:
        self._writer.close()
        with self._readers_lock:
            for conn in self._readers:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._readers.clear()
        self._local = threading.local()
