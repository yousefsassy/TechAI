"""Share active public discovery work and fence cross-process completion."""
import asyncio
import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete

from backend.models import DiscoveryOperation, now
from backend.services.shopping_transactions import read, transaction, write
from backend.services import shopping_metrics as metrics
from backend.database import Session

LEASE_SECONDS = 115
log = logging.getLogger(__name__)
_active = {}


@dataclass
class Lookup:
    cancelled: threading.Event = field(default_factory=threading.Event)
    urls: set[str] = field(default_factory=set)
    callbacks: set = field(default_factory=set)
    subscribers: int = 0
    task: asyncio.Task | None = None

    def publish(self, url):
        if url not in self.urls:
            self.urls.add(url)
            for callback in tuple(self.callbacks):
                callback(url)


def _claim(signature, token, force):
    with transaction() as db:
        timestamp = now()
        db.execute(delete(DiscoveryOperation).where(
            DiscoveryOperation.expires_at < timestamp - timedelta(minutes=5)
        ))
        row = db.get(DiscoveryOperation, signature)
        if row is not None and row.expires_at > timestamp:
            if row.completed_at is None:
                return row.token, 'active'
            if force and row.reports and all(r['status'] in ('checked', 'no_match') for r in row.reports):
                if any(r['urls'] for r in row.reports):
                    return row.token, 'recent'
        values = dict(token=token, expires_at=timestamp + timedelta(seconds=LEASE_SECONDS),
                      completed_at=None, reports=None)
        if row is None:
            db.add(DiscoveryOperation(signature=signature, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        return token, 'leader'


def _state(signature):
    with Session() as db:
        row = db.get(DiscoveryOperation, signature)
        if row is None:
            return None
        return row.token, row.expires_at, row.completed_at, row.reports


def _complete(signature, token, reports):
    with transaction() as db:
        row = db.get(DiscoveryOperation, signature)
        timestamp = now()
        if row and row.token == token and row.expires_at > timestamp:
            row.reports = reports
            row.completed_at = timestamp
            row.expires_at = timestamp + timedelta(seconds=30 if reports else 0)
            return True
    return False


async def _run(signature, operation, force, load):
    token = uuid.uuid4().hex
    pause = 0.1
    deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
    owner, state = await write(_claim, signature, token, force)
    if state == 'active':
        metrics.increment('shared_lookups')
    while state == 'active':
        if operation.cancelled.is_set():
            return []
        if asyncio.get_running_loop().time() >= deadline:
            return [dict(retailer='catalog', status='timeout', checked=0, urls=[])]
        await asyncio.sleep(pause)
        pause = min(0.5, pause * 1.5)
        row = await read(_state, signature)
        if row and row[0] == owner and row[2] is not None and row[3]:
            return row[3]
        if not row or row[0] != owner or row[1] <= now():
            owner, state = await write(_claim, signature, token, force)
    if state == 'recent':
        return [dict(retailer='catalog', status='recent_attempt', checked=0, urls=[])]
    reports = None
    try:
        if not operation.cancelled.is_set():
            reports = await load(operation)
    finally:
        valid = await write(_complete, signature, token, reports)
    if valid:
        return reports or []
    return [dict(retailer='catalog', status='timeout', checked=0, urls=[])]


async def join(criteria, force, load, on_verified=None):
    signature = hashlib.sha256(json.dumps([criteria, force], sort_keys=True, default=str).encode()).hexdigest()
    key = (asyncio.get_running_loop(), signature)
    operation = _active.get(key)
    if operation is None:
        operation = _active[key] = Lookup()

        async def run():
            try:
                return await _run(signature, operation, force, load)
            finally:
                if _active.get(key) is operation:
                    _active.pop(key)

        operation.task = asyncio.create_task(run())

        def check_background(task):
            if not task.cancelled() and task.exception() is not None and operation.subscribers == 0:
                log.warning("Shared lookup exited: %s", type(task.exception()).__name__)

        operation.task.add_done_callback(check_background)
    else:
        metrics.increment('shared_lookups')

    operation.subscribers += 1
    if on_verified:
        operation.callbacks.add(on_verified)
    try:
        if on_verified:
            for url in sorted(operation.urls):
                on_verified(url)
        return await asyncio.shield(operation.task)
    finally:
        if on_verified:
            operation.callbacks.discard(on_verified)
        operation.subscribers -= 1
        if not operation.subscribers and not operation.task.done():
            operation.cancelled.set()
            if _active.get(key) is operation:
                _active.pop(key)
