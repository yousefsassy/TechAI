from pathlib import Path
import sqlite3
from time import monotonic, sleep

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from backend.config import settings

class Base(DeclarativeBase):
    pass

if not settings.database_url.startswith('sqlite:///'):
    raise ValueError('Only sandbox SQLite is supported')
Path(settings.database_url.removeprefix('sqlite:///')).resolve().parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(settings.database_url, connect_args={'check_same_thread': False, 'timeout': 30})
@event.listens_for(engine, 'connect')
def configure(connection, _):
    connection.execute('PRAGMA foreign_keys=ON')
    # Concurrent first startup can hit BUSY while switching a new file to WAL,
    # before the normal SQLite busy timeout takes effect for that operation.
    deadline = monotonic() + 1
    while True:
        try:
            connection.execute('PRAGMA journal_mode=WAL').close()
            break
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or monotonic() >= deadline:
                raise
            sleep(.025)
Session = sessionmaker(engine, expire_on_commit=False)


def migrate(connection):
    """Apply additive schema changes in the database startup transaction."""
    connection.exec_driver_sql(
        'CREATE TABLE IF NOT EXISTS techstock_schema_migrations (version INTEGER PRIMARY KEY)'
    )
    applied = set(connection.exec_driver_sql('SELECT version FROM techstock_schema_migrations').scalars())
    if 1 not in applied:
        existing = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(retailer_request_clocks)')}
        for name, definition in (
            ('active_until', 'FLOAT NOT NULL DEFAULT 0'),
            ('owner_token', 'VARCHAR'),
            ('transport_failures', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if name not in existing:
                connection.exec_driver_sql(f'ALTER TABLE retailer_request_clocks ADD COLUMN {name} {definition}')
        connection.exec_driver_sql('INSERT INTO techstock_schema_migrations (version) VALUES (1)')


def init_db():
    from backend import models
    from backend.services.title_index import initialize
    with engine.begin() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        Base.metadata.create_all(connection)
        migrate(connection)
        initialize(connection)
