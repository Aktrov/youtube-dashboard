from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the table's first release. SQLite's ADD COLUMN is a
# cheap metadata-only change and safe on a populated table, so a full
# migration tool would be overkill here — just add anything missing on boot.
_ADDITIVE_COLUMNS = {
    "channels": {
        "last_poll_ok": "BOOLEAN",
        "last_poll_error": "TEXT",
    },
}


def run_lightweight_migrations():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it fresh with every column
            have = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name not in have:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))
                    print(f"[Migration] Added {table}.{name} ({ddl_type}).")
