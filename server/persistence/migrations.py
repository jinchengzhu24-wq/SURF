"""Standalone CLI (`python -m server.persistence.migrations`) for data migrations."""

from __future__ import annotations

import argparse
import json
from typing import Any

from ._cocreation_migrate import migrate_cocreation_db
from ._study_import import import_study_logs
from .database import DEFAULT_DATABASE_PATH, SQLiteDatabase

__all__ = ["import_study_logs", "main", "migrate_cocreation_db"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import legacy study JSONL logs and/or migrate 8010 SQLite DB."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DATABASE_PATH),
        help="Target unified SQLite database path.",
    )
    parser.add_argument(
        "--import-study-logs",
        default=None,
        help="Directory containing legacy Backend/study_logs/*.jsonl files.",
    )
    parser.add_argument(
        "--migrate-cocreation-db",
        default=None,
        help="Path to legacy CoCreationPrototype SQLite database to migrate.",
    )
    args = parser.parse_args(argv)

    db = SQLiteDatabase(args.db)
    db.initialize()
    output: dict[str, Any] = {"db": str(args.db)}
    if args.import_study_logs:
        output["study_import"] = import_study_logs(db, args.import_study_logs)
    if args.migrate_cocreation_db:
        output["cocreation_migration"] = migrate_cocreation_db(db, args.migrate_cocreation_db)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
