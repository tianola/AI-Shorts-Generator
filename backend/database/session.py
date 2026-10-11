"""
Database session management.
SQLAlchemy session factory with schema sync driven by the ORM models themselves.
"""
from sqlalchemy import create_engine, event, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings
from database.base import Base
import os


def get_engine():
    db_url = os.getenv("TEST_DATABASE_URL")
    if not db_url:
        db_path = os.path.abspath(os.path.join(settings.STORAGE_ROOT, "app.db"))
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db_url = f"sqlite:///{db_path}"
        return create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            echo=False
        )
    else:
        # Use StaticPool for in-memory tests to share connection
        return create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False
        )


engine = get_engine()


@event.listens_for(engine, "connect")
def _apply_sqlite_pragmas(dbapi_conn, _conn_record):
    """busy_timeout is per-connection, so it must be set on every new one.
    Setting it once inside init_database() only protected that single pooled
    connection, leaving the rest of the pool able to fail with 'database is
    locked' while the API and worker write concurrently."""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


# Session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def _literal_default(column):
    """Render a Python-side scalar column default as a SQLite literal.

    Callable defaults (datetime.utcnow) and missing defaults return None: the
    column is added nullable and existing rows get NULL, which is what the ORM
    already assumes for every optional column here.
    """
    default = column.default
    if default is None or not default.is_scalar:
        return None
    value = default.arg
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _sync_columns(conn):
    """Add columns that the models declare but an existing database lacks.

    derive_from: Base.metadata, so adding a Column to models.py is enough. The
    previous hand-maintained list silently fell behind, and a missing column
    made every ORM query fail with OperationalError - which the worker polling
    loop swallowed and retried at ~10Hz, writing 1.2GB of logs while processing
    nothing.
    """
    import database.models  # noqa: F401  (register mappers on Base)

    inspector = sa_inspect(conn)
    known_tables = set(inspector.get_table_names())
    added = []

    for table_name, table in Base.metadata.tables.items():
        if table_name not in known_tables:
            continue  # create_all() owns whole-table creation
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}'
            literal = _literal_default(column)
            if literal is not None:
                ddl += f" DEFAULT {literal}"
            conn.execute(text(ddl))
            added.append((table_name, column.name))

    return added


def init_database():
    """Create missing tables, then add missing columns from the models."""
    import database.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        added = _sync_columns(conn)

    for table_name, column_name in added:
        print(f"[db] added missing column {table_name}.{column_name}")

    # A schema the models do not match is not survivable: every ORM query would
    # raise, and the caller would look healthy while doing nothing.
    remaining = _missing_columns_report()
    if remaining:
        raise RuntimeError(
            "Database schema is still missing columns after sync: "
            + ", ".join(f"{t}.{c}" for t, c in remaining)
        )

    print("Database tables created/verified with migrations")


def _missing_columns_report():
    import database.models  # noqa: F401

    missing = []
    with engine.connect() as conn:
        inspector = sa_inspect(conn)
        known = set(inspector.get_table_names())
        for table_name, table in Base.metadata.tables.items():
            if table_name not in known:
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name not in existing:
                    missing.append((table_name, column.name))
    return missing


def get_database():
    """Dependency to get DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
