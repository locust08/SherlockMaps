"""Create a consistent SQLite backup, including committed WAL data.

Run from the repository: .venv/Scripts/python.exe automation/backup_database.py
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "data" / "malaysia_qualified_companies.sqlite"
    folder = root / "data" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / ("before-optimization-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite")
    # Reserve an exclusive destination: never overwrite a previous backup.
    destination.touch(exist_ok=False)
    reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer, pages=2048, sleep=0.1)
        check = writer.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Backup quick_check failed: {check}")
        count = writer.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        print(json.dumps({"backup": str(destination), "quick_check": check, "companies": count,
                          "bytes": destination.stat().st_size}), flush=True)
    finally:
        writer.close()
        reader.close()


if __name__ == "__main__":
    main()
