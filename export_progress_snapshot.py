"""Emit the current crawler ledger as JSON for the spreadsheet exporter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "malaysia_qualified_companies.sqlite"
STATUS_PATH = ROOT / "data" / "malaysia_batch_status.json"

status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
companies = conn.execute(
    """SELECT sector, business_name, maps_category, address, city_state, phone,
              website, rating, opening_hours, plus_code, first_seen_at, last_seen_at
       FROM companies ORDER BY sector, city_state, business_name"""
).fetchall()
jobs = conn.execute(
    """SELECT prompt, sector, locality, state, term, status, source_count,
              qualified_new, started_at, completed_at, error
       FROM jobs ORDER BY COALESCE(completed_at, started_at), prompt"""
).fetchall()
sectors = conn.execute(
    "SELECT sector, COUNT(*) FROM companies GROUP BY sector ORDER BY sector"
).fetchall()
conn.close()
print(json.dumps({"status": status, "companies": companies, "jobs": jobs, "sectors": sectors}))
