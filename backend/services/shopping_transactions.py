"""Run complete SQLite operations off the shopping request loop."""
import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event
from time import perf_counter
from weakref import WeakKeyDictionary, WeakValueDictionary

from anyio import CapacityLimiter, to_thread
from sqlalchemy.exc import OperationalError

from backend.database import Session
from backend.services import shopping_metrics as metrics

_capacity = WeakKeyDictionary()
_turn_locks = WeakValueDictionary()
_pending_reads = {}


class ShoppingConflict(Exception):
    """A logical turn can be retried against the current conversation."""


class ShoppingBusy(ShoppingConflict):
    """The catalog is temporarily busy."""


@dataclass
class _Lane:
    workers: CapacityLimiter
    max_pending: int
    pending: int = 0


async def _run(name, operation, *args, **kwargs):
    loop = asyncio.get_running_loop()
    limits = _capacity.get(loop)
    if limits is None:
        limits = _capacity[loop] = {
            'sql_read': _Lane(CapacityLimiter(2), 128),
            'sql_write': _Lane(CapacityLimiter(1), 64),
        }
    lane = limits[name]
    if lane.pending >= lane.max_pending:
        raise ShoppingBusy("Shopping data is busy. Retry this turn.")
    lane.pending += 1
    queued = perf_counter()

    def work():
        metrics.record_stage('queue_wait', metrics.elapsed(queued))
        with metrics.stage(name):
            return operation(*args, **kwargs)

    try:
        return await to_thread.run_sync(work, limiter=lane.workers)
    finally:
        lane.pending -= 1


async def read(operation, *args, **kwargs):
    return await _run('sql_read', operation, *args, **kwargs)


@dataclass
class _PendingRead:
    started: Event = field(default_factory=Event)
    task: asyncio.Task | None = None
    waiters: int = 0


async def queued_public_read(key, operation, *args):
    """Coalesce equal reads while queued, before they take a database snapshot."""
    index = (asyncio.get_running_loop(), key)
    pending = _pending_reads.get(index)
    if pending is None or pending.started.is_set() or pending.task.done():
        pending = _pending_reads[index] = _PendingRead()

        def work():
            pending.started.set()
            return operation(*args)

        pending.task = asyncio.create_task(read(work))

        def finish(task):
            if _pending_reads.get(index) is pending:
                _pending_reads.pop(index)
            if not task.cancelled():
                task.exception()

        pending.task.add_done_callback(finish)
    pending.waiters += 1
    try:
        return await asyncio.shield(pending.task)
    finally:
        pending.waiters -= 1
        if not pending.waiters and not pending.started.is_set() and not pending.task.done():
            pending.task.cancel()


async def write(operation, *args):
    return await _run('sql_write', operation, *args)


def turn_lock(owner, key):
    index = (asyncio.get_running_loop(), owner, key)
    lock = _turn_locks.get(index)
    if lock is None:
        lock = _turn_locks[index] = asyncio.Lock()
    return lock


@contextmanager
def transaction():
    """Acquire the SQLite writer before reading mutable state."""
    try:
        with Session() as db:
            connection = db.connection()
            driver = connection.connection.driver_connection
            wait = driver.execute("PRAGMA busy_timeout").fetchone()[0]
            driver.execute("PRAGMA busy_timeout=1500")
            try:
                with metrics.stage("sql_lock"):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield db
                db.flush()
            finally:
                driver.execute(f"PRAGMA busy_timeout={wait}")
            with metrics.stage("commit"):
                db.commit()
    except OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", None)
        if code is not None and (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise ShoppingBusy("Shopping data is busy. Retry this turn.") from exc
        raise
