"""Lightweight, separate website-opportunity audit queue for V2 leads."""

from __future__ import annotations

import concurrent.futures
import re
import sqlite3
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from batch_collect_malaysia_v2 import DB_PATH, open_db, utc_now

MAX_BYTES = 750_000
TIMEOUT_SECONDS = 12
WORKERS = 5


@dataclass(frozen=True)
class AuditResult:
    company_id: int
    https_ok: int
    homepage_ok: int
    has_title: int
    has_meta_description: int
    has_viewport: int
    has_local_business_schema: int
    error: str = ""


def audit(company_id: int, website: str) -> AuditResult:
    request = urllib.request.Request(
        website,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SherlockMapsOpportunityAudit/2.0)"},
    )
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
            body = response.read(MAX_BYTES).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
            final_url = response.geturl().lower()
            lowered = body.lower()
            return AuditResult(
                company_id=company_id,
                https_ok=int(final_url.startswith("https://")),
                homepage_ok=int(200 <= int(response.status) < 400),
                has_title=int(bool(re.search(r"<title[^>]*>\s*[^<]{2,}", body, re.I))),
                has_meta_description=int(bool(re.search(r"<meta[^>]+name=[\"']description[\"']", body, re.I))),
                has_viewport=int(bool(re.search(r"<meta[^>]+name=[\"']viewport[\"']", body, re.I))),
                has_local_business_schema=int("localbusiness" in lowered or "local business" in lowered),
            )
    except Exception as exc:
        return AuditResult(company_id, 0, 0, 0, 0, 0, 0, f"{type(exc).__name__}: {exc}"[:500])


def save_result(conn: sqlite3.Connection, result: AuditResult) -> None:
    status = "completed" if result.homepage_ok else "failed"
    conn.execute(
        """UPDATE website_audits SET status=?,https_ok=?,homepage_ok=?,has_title=?,
           has_meta_description=?,has_viewport=?,has_local_business_schema=?,checked_at=?,error=?
           WHERE company_id=?""",
        (status, result.https_ok, result.homepage_ok, result.has_title,
         result.has_meta_description, result.has_viewport, result.has_local_business_schema,
         utc_now(), result.error, result.company_id),
    )
    conn.execute("UPDATE companies SET website_audit_status=? WHERE id=?", (status, result.company_id))
    conn.commit()


def run(limit: int = 500) -> int:
    conn = open_db(Path(DB_PATH))
    rows = conn.execute(
        """SELECT c.id,c.website FROM companies c JOIN website_audits a ON a.company_id=c.id
           WHERE a.status='pending' AND c.website<>'' ORDER BY c.id LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        conn.close()
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(audit, int(company_id), website) for company_id, website in rows]
        for future in concurrent.futures.as_completed(futures):
            save_result(conn, future.result())
    conn.close()
    return len(rows)


if __name__ == "__main__":
    raise SystemExit(0 if run() >= 0 else 1)
