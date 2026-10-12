"""V4 collection and sales-operations dashboard for LOCUS-T Malaysia leads."""

from __future__ import annotations

import html
import csv
import io
import json
import re
import secrets
import sqlite3
import threading
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as xml_escape

from lead_intelligence_v4 import INTELLIGENCE_VERSION, SALES_STATUSES, update_sales_lead
from collector_control import CollectorController
from collection_selection import catalog, preview_selection
from system_memory import available_memory
from sales_updates import save_sales_update, StaleSalesEdit

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "malaysia_qualified_companies.sqlite"
STATUS_PATH = ROOT / "data" / "malaysia_batch_status.json"
PORT = 8765
DASHBOARD_CACHE_SECONDS = 120
COLLECTOR_TOKEN = secrets.token_urlsafe(32)
_collector_controller: CollectorController | None = None
_collector_controller_lock = threading.Lock()


def collector_controller() -> CollectorController:
    global _collector_controller
    with _collector_controller_lock:
        if (_collector_controller is None or _collector_controller.database != DB_PATH.resolve()
                or _collector_controller.status_file != STATUS_PATH.resolve()):
            _collector_controller = CollectorController(DB_PATH, STATUS_PATH)
        return _collector_controller

_dashboard_cache_lock = threading.Lock()
_dashboard_cache: dict | None = None
_dashboard_cache_at = 0.0
_dashboard_refresh_running = False


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def table(headers: list[str], rows: list[tuple]) -> str:
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    if not rows:
        body = f"<tr><td colspan='{len(headers)}'>No data yet</td></tr>"
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


NAV_ITEMS = (
    ("dashboard", "/", "Collect leads"),
    ("leads", "/leads", "Review leads"),
    ("pipeline", "/pipeline", "Sales pipeline"),
    ("coverage", "/coverage", "Coverage & Yield"),
    ("searches", "/searches", "Search Queries"),
    ("report", "/export/report.html", "Summary Report"),
)


def navigation(active: str = "") -> str:
    links = "".join(
        f"<a href='{url}' class='{'active' if key == active else ''}'>{esc(label)}</a>"
        for key, url, label in NAV_ITEMS
    )
    return f"<nav class='topnav' aria-label='Dashboard pages'>{links}</nav>"


def environment_notice() -> str:
    temporary = any(root in DB_PATH.resolve().parents for root in
                    (Path('/private/tmp'), Path('/tmp'), Path(tempfile.gettempdir()).resolve()))
    label = 'Temporary database' if temporary else 'Project database' if DB_PATH == ROOT / 'data' / 'malaysia_qualified_companies.sqlite' else 'Custom database'
    explanation = 'This is not the production database. It may contain live collected leads; export or migrate them before clearing temporary files.' if temporary else 'Collection and sales updates are stored in this database.'
    return f"<aside id='environment-notice' class='notice' aria-label='Data environment'><strong>{label}</strong> · {explanation}<details><summary>Database location</summary><code>{esc(DB_PATH)}</code></details></aside>"


def memory_label(status: dict) -> str:
    value = status.get('available_ram_gb')
    return 'Unavailable' if value is None else f'{value:.2f} GiB available ({status.get("ram_operating_state", "unknown")})'


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-32768")
    return conn


def write_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


COMPANY_EXPORT_COLUMNS = [
    ("Company ID", "c.id"),
    ("Organization ID", "co.organization_id"),
    ("Business name", "c.business_name"),
    ("Industry", "COALESCE(ic.industry_label,c.sector)"),
    ("Lead tier", "COALESCE(c.lead_tier,'LEGACY')"),
    ("Primary offer", "li.primary_offer"),
    ("Sales score", "li.sales_readiness_score"),
    ("Sales rank", "li.sales_rank"),
    ("Sales status", "sl.status"),
    ("Assigned PIC", "sl.assigned_pic"),
    ("Address", "c.address"),
    ("Locality", "COALESCE(c.locality,c.city_state)"),
    ("State", "COALESCE(c.state_name,c.city_state)"),
    ("Phone", "c.phone"),
    ("Website", "c.website"),
    ("Maps category", "c.maps_category"),
    ("Rating", "c.rating"),
    ("Review count", "c.reviews_count"),
    ("Public email", "wi.public_email"),
    ("WhatsApp", "wi.whatsapp_url"),
    ("Maps place ID", "c.maps_place_id"),
    ("Source URL", "c.source_url"),
    ("First seen", "c.first_seen_at"),
    ("Last seen", "c.last_seen_at"),
]


def company_export_query() -> str:
    fields = ",".join(expr for _, expr in COMPANY_EXPORT_COLUMNS)
    return f"""SELECT {fields}
        FROM companies c
        LEFT JOIN company_industry_classification ic
          ON ic.company_id=c.id AND ic.taxonomy_version=4
        LEFT JOIN company_organization co ON co.company_id=c.id
        LEFT JOIN lead_intelligence li
          ON li.company_id=c.id AND li.intelligence_version=4
        LEFT JOIN sales_leads sl ON sl.organization_id=co.organization_id
        LEFT JOIN website_intelligence wi ON wi.company_id=c.id
        ORDER BY c.id"""


def company_export_csv() -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([label for label, _ in COMPANY_EXPORT_COLUMNS])
    conn = connection()
    for row in conn.execute(company_export_query()):
        writer.writerow(["" if value is None else value for value in row])
    conn.close()
    return output.getvalue().encode("utf-8-sig")


def stream_company_csv(handler: BaseHTTPRequestHandler) -> None:
    """Stream the CSV so large exports do not require a giant response buffer."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="sherlockmaps-companies.csv"')
    handler.send_header("Connection", "close")
    handler.end_headers()
    conn = connection()
    try:
        batch = io.StringIO(newline="")
        writer = csv.writer(batch)
        writer.writerow([label for label, _ in COMPANY_EXPORT_COLUMNS])
        handler.wfile.write(("\ufeff" + batch.getvalue()).encode("utf-8"))
        for row_number, row in enumerate(conn.execute(company_export_query()), 1):
            batch.seek(0)
            batch.truncate(0)
            writer.writerow(["" if value is None else value for value in row])
            if row_number % 500 == 0:
                handler.wfile.write(batch.getvalue().encode("utf-8"))
                handler.wfile.flush()
            else:
                # Keep small batches so a slow client does not retain all rows in memory.
                handler.wfile.write(batch.getvalue().encode("utf-8"))
        handler.wfile.flush()
    finally:
        conn.close()


def xlsx_cell(value: object, ref: str) -> str:
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t>{xml_escape(text)}</t></is></c>'


def xlsx_column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def company_export_xlsx(destination: Path | None = None) -> bytes | None:
    # Build a standards-compliant XLSX using only the Python standard library.
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Companies" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'''
    output = destination if destination is not None else io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        # Keep only one row in Python memory, including for disk-backed exports.
        with archive.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as sheet:
            sheet.write(b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
            headers = [label for label, _ in COMPANY_EXPORT_COLUMNS]
            def write_row(number: int, values) -> None:
                cells = "".join(xlsx_cell(value, f"{xlsx_column(index)}{number}")
                                for index, value in enumerate(values, 1))
                sheet.write(f'<row r="{number}">{cells}</row>'.encode("utf-8"))
            write_row(1, headers)
            conn = connection()
            try:
                for number, row in enumerate(conn.execute(company_export_query()), 2):
                    write_row(number, row)
            finally:
                conn.close()
            sheet.write(b'</sheetData></worksheet>')
    return output.getvalue() if isinstance(output, io.BytesIO) else None


def report_page(data: dict) -> str:
    status = data["status"]
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    qualified = int(status.get("qualified_companies", 0) or 0)
    target = int(status.get("target_qualified_companies", 0) or 0)
    sections = [
        ("Qualified locations", f"{qualified:,}"),
        ("Target", f"{target:,}"),
        ("24-hour query rate", f"{float(status.get('query_rate_per_hour_24h', 0) or 0):.2f} queries/hour"),
        ("24-hour qualified rate", f"{float(status.get('qualified_rate_per_hour_24h', 0) or 0):.2f} companies/hour"),
        ("Estimated remaining time", f"{status.get('eta_hours', 'calculating')} hours"),
        ("Browsers", f"{status.get('actual_browser_processes', 0)} active / {status.get('scheduler_worker_limit', 0)} allowed"),
    ]
    summary_rows = [(label, value) for label, value in sections]
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>SherlockMaps Malaysia Report</title>
    <style>body{{font-family:Segoe UI,Arial,sans-serif;margin:36px;color:#142535}}h1{{color:#102f46}}table{{border-collapse:collapse;min-width:520px}}td,th{{border:1px solid #cbd8df;padding:9px;text-align:left}}th{{background:#1f6f8b;color:#fff}}h2{{margin-top:30px}}small{{color:#607585}}.topnav{{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 24px}}.topnav a{{padding:9px 12px;border-radius:7px;background:#e8f0f4;color:#15566d;text-decoration:none}}.topnav a.active{{background:#1f6f8b;color:white}}</style></head><body>
    {navigation('report')}<h1>LOCUS-T Malaysia Collection Report</h1><small>Generated {esc(generated)} · source database: malaysia_qualified_companies.sqlite</small>
    <h2>Collection summary</h2>{table(['Metric','Value'], summary_rows)}
    <h2>By industry</h2>{table(['Industry','Locations'], data['sectors'])}
    <h2>By lead tier</h2>{table(['Lead tier','Locations'], data['tiers'])}
    <h2>By state</h2>{table(['State','Locations'], data['states'])}
    </body></html>"""


def load_status() -> dict:
    status = json.loads(STATUS_PATH.read_text(encoding='utf-8')) if STATUS_PATH.exists() else {}
    # Dashboard always measures this host, even while an older collector runs.
    status['collector_memory_legacy'] = 'memory_source' not in status and bool(status)
    if status['collector_memory_legacy']:
        status['lowest_available_ram_gb'] = None
    status.update(available_memory())
    return status


def collector_state(status: dict) -> tuple[str, str]:
    if status.get("halt_reason"):
        return "Halted", "bad"
    updated = status.get("updated_at")
    if updated:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()
            if age > 300:
                return "Not updating", "warn"
        except ValueError:
            pass
    return "Running", "good"


def read_dashboard_data() -> dict:
    status = load_status()
    if not DB_PATH.exists():
        return _empty_dashboard_data()
    conn = connection()
    sectors = conn.execute(
        """SELECT industry_label,COUNT(*) FROM company_industry_classification
           WHERE taxonomy_version=4 GROUP BY industry_label ORDER BY COUNT(*) DESC"""
    ).fetchall()
    tiers = conn.execute("SELECT COALESCE(lead_tier,'LEGACY'),COUNT(*) FROM companies GROUP BY lead_tier ORDER BY COUNT(*) DESC").fetchall()
    states = conn.execute("SELECT COALESCE(state_name,city_state),COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()
    recent = conn.execute(
        """SELECT c.business_name,COALESCE(ic.industry_label,c.sector),COALESCE(c.locality,c.city_state),c.maps_category,
                  COALESCE(lead_tier,'LEGACY'),phone,website,rating
           FROM companies c LEFT JOIN company_industry_classification ic
             ON ic.company_id=c.id AND ic.taxonomy_version=4
           ORDER BY c.last_seen_at DESC LIMIT 30"""
    ).fetchall()
    jobs = conn.execute(
        """SELECT prompt,status,links_discovered,processed_count,qualified_new,
                  duplicate_count,rejected_count,worker_id,completed_at,error
           FROM search_jobs WHERE taxonomy_version IN (3,4)
           ORDER BY COALESCE(completed_at,started_at) DESC LIMIT 30"""
    ).fetchall()
    events = conn.execute(
        """SELECT event_type,details,worker_count,available_ram_gb,created_at
           FROM worker_events ORDER BY id DESC LIMIT 15"""
    ).fetchall()
    totals = conn.execute(
        """WITH all_jobs AS (
               SELECT links_discovered,processed_count,duplicate_count,rejected_count
                 FROM jobs WHERE collector_version=2
               UNION ALL
               SELECT links_discovered,processed_count,duplicate_count,rejected_count
                 FROM search_jobs WHERE taxonomy_version IN (3,4)
           ) SELECT COALESCE(SUM(links_discovered),0),COALESCE(SUM(processed_count),0),
                    COALESCE(SUM(duplicate_count),0),COALESCE(SUM(rejected_count),0)
               FROM all_jobs"""
    ).fetchone()
    ranks = conn.execute(
        """SELECT sales_rank,COUNT(*) FROM lead_intelligence
           WHERE intelligence_version=4 GROUP BY sales_rank ORDER BY sales_rank"""
    ).fetchall()
    offers = conn.execute(
        """SELECT primary_offer,COUNT(*) FROM lead_intelligence
           WHERE intelligence_version=4 GROUP BY primary_offer ORDER BY COUNT(*) DESC"""
    ).fetchall()
    pipeline = conn.execute(
        """SELECT status,COUNT(*) FROM sales_leads GROUP BY status
           ORDER BY CASE status WHEN 'NEW' THEN 0 WHEN 'REVIEWED' THEN 1 WHEN 'CONTACTED' THEN 2
           WHEN 'QUALIFIED' THEN 3 WHEN 'DISCOVERY' THEN 4 WHEN 'PROPOSAL_SENT' THEN 5
           WHEN 'WON' THEN 6 WHEN 'LOST' THEN 7 ELSE 8 END"""
    ).fetchall()
    markets = conn.execute(
        """SELECT CASE WHEN c.state_name IN ('Selangor','Federal Territory') THEN 'Klang Valley'
                    WHEN c.state_name='Johor' THEN 'Johor'
                    WHEN c.state_name='Penang' THEN 'Penang'
                    WHEN c.state_name IN ('Perak','Pahang','Kedah','Negeri Sembilan','Melaka')
                      THEN 'Peninsular Expansion' ELSE 'Other (legacy)' END market,
                  COUNT(*),SUM(CASE WHEN li.sales_rank IN ('A','B') THEN 1 ELSE 0 END)
           FROM companies c LEFT JOIN lead_intelligence li
             ON li.company_id=c.id AND li.intelligence_version=4
           GROUP BY market ORDER BY COUNT(*) DESC"""
    ).fetchall()
    company_count = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
    ready_count = conn.execute('SELECT COUNT(*) FROM (' + call_list_query() + ')').fetchone()[0]
    conn.close()
    return {"status": status, "sectors": sectors, "tiers": tiers, "states": states,
            "recent": recent, "jobs": jobs, "events": events, "totals": totals,
            "ranks": ranks, "offers": offers, "pipeline": pipeline, "markets": markets,
            "company_count": company_count, "ready_count": ready_count}


def _empty_dashboard_data() -> dict:
    """Return an immediately renderable shell while analytics warm in the background."""
    return {
        "status": load_status(), "sectors": [], "tiers": [], "states": [],
        "recent": [], "jobs": [], "events": [], "totals": (0, 0, 0, 0),
        "ranks": [], "offers": [], "pipeline": [], "markets": [], "analytics_loading": True,
    }


def _refresh_dashboard_cache() -> None:
    global _dashboard_cache, _dashboard_cache_at, _dashboard_refresh_running
    try:
        fresh = read_dashboard_data()
        with _dashboard_cache_lock:
            _dashboard_cache = fresh
            _dashboard_cache_at = time.monotonic()
    finally:
        with _dashboard_cache_lock:
            _dashboard_refresh_running = False


def dashboard_data() -> dict:
    """Serve cached analytics and ensure only one expensive refresh runs at a time."""
    global _dashboard_cache, _dashboard_refresh_running
    now = time.monotonic()
    with _dashboard_cache_lock:
        if _dashboard_cache is None:
            _dashboard_cache = _empty_dashboard_data()
        expired = now - _dashboard_cache_at >= DASHBOARD_CACHE_SECONDS
        if expired and not _dashboard_refresh_running:
            _dashboard_refresh_running = True
            threading.Thread(
                target=_refresh_dashboard_cache,
                name="dashboard-analytics-refresh",
                daemon=True,
            ).start()
        snapshot = dict(_dashboard_cache)
    snapshot["status"] = load_status()
    return snapshot


CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f6f8;color:#142535}
header{background:#102f46;color:white;padding:26px 5vw}h1{margin:0;font-size:28px}
header p{margin:7px 0 0;color:#c9e3f0}a{color:#167398}header a{color:#d5edf7}
.topnav{display:flex;flex-wrap:wrap;gap:7px;margin:16px 0 8px}.topnav a{padding:9px 12px;border:1px solid #46758c;border-radius:7px;background:#163f56;color:#dceef6;text-decoration:none;font-size:14px}.topnav a:hover{background:#245d76}.topnav a.active{background:#e7f4f8;color:#123047;border-color:#e7f4f8;font-weight:700}
.status-strip{display:flex;flex-wrap:wrap;gap:10px 20px;margin-top:13px;padding:10px 12px;background:#0b2639;border-radius:8px;font-size:13px;color:#d7ebf4}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.dot.good{background:#37c978}.dot.warn{background:#f4a340}.dot.bad{background:#ef5b5b}
main{max-width:1600px;margin:24px auto;padding:0 22px}.cards{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:14px}
.card{background:white;border:1px solid #d8e2e8;border-radius:10px;padding:16px}.label{color:#607585;font-size:13px}
.value{font-size:28px;font-weight:700;color:#123047;margin-top:5px}.small{font-size:14px}.good{color:#157347}.warn{color:#a85a00}.bad{color:#b42318}
.bar{height:16px;background:#dbe7ed;border-radius:9px;overflow:hidden;margin:16px 0 7px}.bar span{display:block;height:100%;background:#198a70}
h2{font-size:19px;margin:30px 0 10px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
table{width:100%;border-collapse:collapse;background:white;font-size:12.5px}th{background:#1f6f8b;color:white;text-align:left;padding:9px;position:sticky;top:0}
td{padding:8px;border-bottom:1px solid #e4ebef;vertical-align:top;max-width:380px;word-break:break-word}tr:nth-child(even){background:#f8fbfc}
.table-wrap{overflow:auto;max-height:470px;border:1px solid #d8e2e8;border-radius:8px}
input,select,textarea{padding:9px;border:1px solid #b9c8d1;border-radius:6px;margin:0 8px 12px 0;background:white}input[type=text]{width:min(480px,90%)}textarea{width:min(900px,95%);height:180px}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}.button{display:inline-block;padding:10px 14px;border-radius:7px;background:#1f6f8b;color:white;text-decoration:none;font-size:14px}.button:hover{background:#15566d}
button:disabled{opacity:.45;cursor:not-allowed}
[hidden]{display:none!important}.scope-fields label{display:block}.scope-fields select[multiple]{width:100%;max-width:520px}.scope-fields textarea{height:70px;width:95%}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
.notice{padding:14px 18px;border:1px solid #c4d9e5;background:#eaf3f8;border-radius:8px;margin-bottom:20px;line-height:1.5}.notice code{overflow-wrap:anywhere}
.journey{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 24px}.journey a{padding:10px 14px;background:white;border:1px solid #b9c8d1;border-radius:7px;text-decoration:none}
summary{cursor:pointer;font-weight:600;padding:12px 0}details{margin:12px 0}h3{font-size:17px;margin:18px 0 12px}
.filter-fields{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.filter-fields label{display:flex;flex-direction:column;gap:6px}.filter-fields input,.filter-fields select{margin:0;max-width:100%;box-sizing:border-box}
.badge{display:inline-block;border-radius:5px;background:#eaf3f8;color:#123047;padding:4px 7px}.nowrap{white-space:nowrap!important;word-break:normal}.muted{color:#526875}
.mobile-leads{display:none}.lead-card{margin:12px 0}.lead-card h3{margin-top:0}.lead-card p{overflow-wrap:anywhere}
.desktop-leads table{font-size:14px}.desktop-leads td{padding:12px}.form-grid label{display:flex;flex-direction:column;gap:6px}.form-grid input,.form-grid select,.form-grid textarea{width:100%;box-sizing:border-box;margin:0}.form-grid textarea{height:110px}
:focus-visible{outline:3px solid #af5700;outline-offset:3px}.primary-cards{grid-template-columns:repeat(4,minmax(0,1fr))}.help{line-height:1.6;max-width:900px}
@media(max-width:650px){main{padding:0 16px}header{padding:22px 20px}h1{font-size:24px}.desktop-leads{display:none}.mobile-leads{display:block}.primary-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.filter-fields label{width:100%}.filter-fields input,.filter-fields select{width:100%}.button{min-height:24px}.journey{gap:8px}.journey a{flex:1 1 130px}.value{font-size:24px}}
"""


def dashboard_page(data: dict) -> str:
    status = data["status"]
    qualified = int(status.get("qualified_companies", data.get('company_count', 0)))
    target = int(status.get("target_qualified_companies", 400_000))
    progress = min(100.0, qualified / target * 100) if target else 0
    control = collector_controller().snapshot()
    state = control['state'].title()
    state_class = 'good' if control['state'] in ('running', 'completed') else 'bad' if control['state'] == 'failed' else 'warn'
    totals = data["totals"]
    eta = status.get("eta_hours")
    query_rate = float(status.get("query_rate_per_hour_24h", 0) or 0)
    query_window = float(status.get("query_rate_window_hours", 0) or 0)
    query_count = int(status.get("queries_completed_24h", 0) or 0)
    initial_updated_at = str(status.get("updated_at", ""))
    actual_workers = int(status.get("actual_browser_processes", status.get("active_workers", 0)) or 0)
    worker_limit = int(status.get("scheduler_worker_limit", actual_workers) or 0)
    maximum_workers = int(status.get("maximum_workers", 4) or 4)
    ram_state = str(status.get("ram_operating_state", "unknown"))
    ram_class = "good" if ram_state == "healthy" else "bad" if ram_state == "critical" else "warn"
    progress_text = '<0.1%' if 0 < progress < .1 else f'{progress:.1f}%'
    scope_canary = status.get('scope_canary') or {}
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>LOCUS-T Lead Intelligence V4</title><style>{CSS}</style></head><body>
    <header><h1>Collect and review leads</h1>{navigation('dashboard')}<p>Build a lead list, review sales opportunities, then record your team's outcomes.</p>
    <div class='status-strip'>
      <span>Dashboard refreshed: <strong id='dashboard-refresh'>--</strong></span>
      <span><i id='heartbeat-dot' class='dot {state_class}'></i>Collector updated: <strong id='collector-update'>--</strong> (<span id='heartbeat-age'>--</span>)</span>
      <span>Next full refresh: <strong id='refresh-countdown'>30s</strong></span>
      <span>Browsers: <strong id='header-workers'>{actual_workers} active / {worker_limit} allowed / {maximum_workers} max</strong></span>
      <span>Device memory: <strong id='header-ram' class='{ram_class}'>{esc(memory_label(status))}</strong></span>
      <span>24h query rate: <strong id='header-query-rate'>{query_rate:.1f}/hour</strong></span>
    </div></header>
    <main>
    {environment_notice()}
    <nav class='journey' aria-label='Lead workflow'><a href='#collection-setup'>1. Collect leads</a><a href='/leads'>2. Review leads</a><a href='/pipeline'>3. Assign and track sales</a></nav>
    <p class='help'>New here? Choose one industry and a small area, preview the searches, then start collection. Review the results before handing them to sales. No messages are sent by this dashboard.</p>
    <section id='collection-setup' class='card' aria-label='Collector controls'>
      <h2 style='margin-top:0'>Set up your collection</h2>
      <h3>1. Choose businesses and search areas</h3>
      <p><label>Search plan <select id="collector-mode"><option value="default">Default recommended plan</option>
        <option value="selected">Choose industries and locations</option></select></label></p>
      <div id="collector-scope" class="scope-fields" hidden>
        <div class='grid2'>
          <div><label for="collector-industries">Industries</label><select id="collector-industries" multiple size="6"></select>
            <p class='small'>Select one or more industries. Hold Ctrl / Command to select multiple options.</p>
            <label for="collector-keywords">Custom search keywords (optional, one per line)</label>
            <textarea id="collector-keywords" placeholder="commercial refrigeration contractor"></textarea>
            <label for="collector-custom-industry">Classify custom keywords as</label><select id="collector-custom-industry"></select>
            <p class='small'>Custom keywords replace preset industry keywords and use the classification above.</p></div>
          <div><label for="collector-states">Malaysian states</label><select id="collector-states" multiple size="6"></select>
            <label for="collector-cities">Cities / areas (optional)</label><select id="collector-cities" multiple size="6"></select>
            <label for="collector-custom-state">State for custom areas</label><select id="collector-custom-state"></select>
            <label for="collector-areas">Custom neighbourhoods, postcodes or industrial parks (one per line)</label>
            <textarea id="collector-areas" placeholder="Seksyen 15"></textarea>
            <p class='small'>Leave cities and custom areas empty to search all listed cities in selected states.
              For areas in different states, enter State | Area on each line.
              Search areas are not geographic boundaries: Google Maps may return nearby businesses.</p></div>
        </div>
      </div>
      <p id='campaign-summary' class='notice' aria-live='polite'></p>
      <p id='scope-lock-notice' class='small' hidden>Collection is running. Pause and wait for active searches to finish before changing the search plan.</p>
      <h3>2. Set your total lead goal</h3>
      <label>Total qualified-location target <input id="collector-target" type="number" min="1" max="10000000" value="{int(control.get('target', target))}"></label>
      <p class='small'>{qualified:,} locations currently collected. This goal is the database total, not additional leads.</p>
      <details><summary>Advanced collection settings</summary><label>Maximum browser workers <input id="collector-workers" type="number" min="1" max="6" value="{int(control.get('workers', 5))}"></label><p class='small'>More workers use more device memory. The collector adapts within this limit.</p></details>
      <h3>3. Preview, then start</h3>
      <p class='small'>Preview confirms pending searches without collecting or saving leads. Preview again after changing your settings.</p>
      <div class='actions'><button id="collector-start" class='button'>Start / Resume</button>
        <button id="collector-pause" class='button'>Pause</button>
        <button id="collector-preview" class='button'>Preview searches</button></div>
      <pre id="collector-query-preview" aria-live="polite" style="white-space:pre-wrap;max-height:260px;overflow:auto"></pre>
      <p id="collector-control-state" role='status' aria-live='polite'>{esc(state)}</p>
      <p class='small'>Pause stops new searches and lets active searches finish. Saved leads and search progress are retained.
        Closing this dashboard does not stop collection. Website enrichment runs separately.</p>
      <p class='small'>The lead target is the total number of qualified locations in this database, not an additional-lead count.</p>
    </section>
    <script>
    const collectorToken = {json.dumps(COLLECTOR_TOKEN)};
    const collectionCatalog = {json.dumps(catalog()).replace('<', chr(92) + 'u003c')};
    let collectorBusy = false;
    let collectorActive = false;
    let collectionDirty = false;
    let previewReady = false;
    let settingsRevision = 0;
    const selectedValues = id => Array.from(document.getElementById(id).selectedOptions).map(option => option.value);
    const lines = id => document.getElementById(id).value.split(/\\r?\\n/).map(value => value.trim()).filter(Boolean);
    function fillOptions(id, values, selected = []) {{
      const node = document.getElementById(id);
      node.replaceChildren();
      values.forEach(item => {{
        const option = new Option(item.label ?? item, item.value ?? item);
        option.selected = selected.includes(option.value);
        node.add(option);
      }});
    }}
    function updateCities() {{
      const states = selectedValues('collector-states');
      const selected = selectedValues('collector-cities');
      fillOptions('collector-cities', collectionCatalog.cities.filter(item => states.includes(item.state)).map(item =>
        ({{label:item.locality + ' — ' + item.state, value:JSON.stringify(item)}})), selected);
      const customState = document.getElementById('collector-custom-state').value;
      fillOptions('collector-custom-state', states, [customState]);
    }}
    function scopeControls() {{
      const choosing = document.getElementById('collector-mode').value === 'selected';
      document.getElementById('collector-scope').hidden = !choosing;
      document.getElementById('collector-mode').disabled = collectorActive || collectorBusy;
      document.querySelectorAll('#collector-scope input, #collector-scope select, #collector-scope textarea').forEach(node => {{
        node.disabled = !choosing || collectorActive || collectorBusy;
      }});
      if (lines('collector-keywords').length) document.getElementById('collector-industries').disabled = true;
      document.getElementById('collector-preview').disabled = collectorBusy || collectorActive;
      document.getElementById('collector-start').disabled = collectorBusy || collectorActive || !previewReady;
      document.getElementById('scope-lock-notice').hidden = !collectorActive;
      const summary = document.getElementById('campaign-summary');
      if (!choosing) summary.textContent = 'Broad built-in plan: 26 industries; 60% core markets (Klang Valley, Johor and Penang), 40% expansion in Perak, Pahang, Kedah, Negeri Sembilan and Melaka. A 60-query trial checks expansion quality.';
      else {{
        const terms = lines('collector-keywords');
        const industries = terms.length ? terms.join(', ') : selectedValues('collector-industries').join(', ');
        const states = selectedValues('collector-states').join(', ');
        const cityCount = selectedValues('collector-cities').length + lines('collector-areas').length;
        summary.textContent = (industries || 'Choose an industry') + ' · ' + (states || 'Choose a state') + ' · ' + (cityCount ? cityCount + ' selected areas' : 'All listed cities in selected states') + '. This plan will not automatically expand to other areas.';
      }}
    }}
    function currentSelection() {{
      if (document.getElementById('collector-mode').value === 'default') return {{mode:'default'}};
      return {{mode:'selected', industries:selectedValues('collector-industries'), states:selectedValues('collector-states'),
        cities:selectedValues('collector-cities').map(value => JSON.parse(value)),
        custom_keywords:lines('collector-keywords'), custom_industry:document.getElementById('collector-custom-industry').value,
        custom_locations:lines('collector-areas').map(value => {{
          const divider = value.indexOf(' | ');
          return divider < 0 ? {{state:document.getElementById('collector-custom-state').value, locality:value}}
            : {{state:value.slice(0, divider).trim(), locality:value.slice(divider + 3).trim()}};
        }})}};
    }}
    function restoreSelection(selection) {{
      document.getElementById('collector-mode').value = selection.mode || 'default';
      fillOptions('collector-industries', collectionCatalog.industries, selection.industries || []);
      fillOptions('collector-states', collectionCatalog.states, selection.states || []);
      fillOptions('collector-custom-industry', [''].concat(collectionCatalog.industries), [selection.custom_industry || '']);
      updateCities();
      const cities = (selection.cities || []).map(item => JSON.stringify(item));
      Array.from(document.getElementById('collector-cities').options).forEach(option => option.selected = cities.includes(option.value));
      const custom = selection.custom_locations || [];
      document.getElementById('collector-keywords').value = (selection.custom_keywords || []).join('\\n');
      const multistate = new Set(custom.map(item => item.state)).size > 1;
      document.getElementById('collector-areas').value = custom.map(item =>
        multistate ? item.state + ' | ' + item.locality : item.locality).join('\\n');
      if (custom.length) document.getElementById('collector-custom-state').value = custom[0].state;
      scopeControls();
    }}
    function showCollector(control) {{
      const active = ['starting','running','pausing'].includes(control.state);
      collectorActive = active;
      scopeControls();
      document.getElementById('collector-start').disabled = active || collectorBusy || !previewReady;
      document.getElementById('collector-pause').disabled = !active || control.state === 'pausing' || collectorBusy;
      document.getElementById('collector-target').disabled = active;
      document.getElementById('collector-workers').disabled = active;
      document.getElementById('collector-control-state').textContent = control.state + (control.error ? ': ' + control.error : '');
      const stateCard = document.getElementById('collector-state');
      stateCard.textContent = control.state.charAt(0).toUpperCase() + control.state.slice(1);
      stateCard.className = 'value ' + (control.state === 'failed' ? 'bad' : ['running','completed'].includes(control.state) ? 'good' : 'warn');
    }}
    async function refreshCollector() {{
      try {{
        const response = await fetch('/api/collector', {{cache:'no-store'}});
        if (!response.ok) throw new Error('Collector status unavailable');
        showCollector(await response.json());
      }} catch (error) {{ document.getElementById('collector-control-state').textContent = error.message; }}
    }}
    async function controlCollector(action) {{
      collectorBusy = true;
      scopeControls();
      document.getElementById('collector-start').disabled = true;
      document.getElementById('collector-pause').disabled = true;
      try {{
        const response = await fetch('/api/collector/' + action, {{method:'POST',
          headers:{{'Content-Type':'application/json','X-Collector-Token':collectorToken}},
          body:JSON.stringify({{target:Number(document.getElementById('collector-target').value),
                               workers:Number(document.getElementById('collector-workers').value), selection:currentSelection()}})}});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Collector control failed');
        collectorBusy = false;
        collectionDirty = false;
        previewReady = false;
        showCollector(result);
      }} catch (error) {{
        collectorBusy = false;
        await refreshCollector();
        document.getElementById('collector-control-state').textContent = error.message;
      }}
    }}
    document.getElementById('collector-start').onclick = () => controlCollector('start');
    document.getElementById('collector-pause').onclick = () => controlCollector('pause');
    document.getElementById('collector-preview').onclick = async () => {{
      const node = document.getElementById('collector-query-preview');
      const revision = settingsRevision;
      previewReady = false;
      scopeControls();
      node.textContent = 'Generating query preview…';
      try {{
        const response = await fetch('/api/collector/preview', {{method:'POST',
          headers:{{'Content-Type':'application/json','X-Collector-Token':collectorToken}},
          body:JSON.stringify({{selection:currentSelection()}})}});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Preview failed');
        if (revision !== settingsRevision) throw new Error('Settings changed during preview. Preview again before starting.');
        previewReady = true;
        scopeControls();
        node.textContent = result.query_count + ' pending searches (completed searches are skipped).\\n'
          + result.sample.map(item => item.query).join('\\n') + '\\n' + result.location_notice;
      }} catch (error) {{ node.textContent = error.message; }}
    }};
    document.querySelectorAll('#collector-mode, #collector-scope select, #collector-scope textarea, #collector-target, #collector-workers').forEach(node => {{
      node.addEventListener('input', () => {{
        collectionDirty = true;
        settingsRevision += 1;
        previewReady = false;
        if (node.id === 'collector-states') updateCities();
        scopeControls();
        document.getElementById('collector-query-preview').textContent = '';
      }});
    }});
    document.addEventListener('DOMContentLoaded', () => {{
      restoreSelection({json.dumps(control['selection']).replace('<', chr(92) + 'u003c')});
      showCollector({json.dumps(control).replace('<', chr(92) + 'u003c')});
      setInterval(refreshCollector, 5000);
    }});
    </script>
    <div class='cards primary-cards' style='margin-top:18px'>
      <div class='card'><div class='label'>Collected locations</div><div class='value'>{'Loading…' if data.get('analytics_loading') and 'qualified_companies' not in status else f'{qualified:,}'}</div><div class='small'>Passed collection checks; not necessarily sales-ready.</div></div>
      <div class='card'><div class='label'>Total lead goal</div><div class='value'>{target:,}</div><div class='small'>{progress_text} collected</div></div>
      <div class='card'><div class='label'>Collector</div><div id='collector-state' class='value {state_class}'>{state}</div><div class='small'>Pilot: {esc(status.get('pilot_status','pending'))}</div></div>
      <div class='card'><div class='label'>Sales-ready organizations</div><div class='value'>{'Loading…' if data.get('analytics_loading') else f"{int(data.get('ready_count', 0)):,}"}</div><div class='small'>A/B rank, deduplicated, excluding lost and do-not-contact.</div><a href='/leads'>Review leads →</a></div>
    </div>
    <div class='bar' role='progressbar' aria-label='Total lead goal' aria-valuemin='0' aria-valuemax='100' aria-valuenow='{progress:.3f}'><span style='width:{progress:.2f}%'></span></div>
    <p class='small'>Next: <a href='/leads'>review collected leads</a>, then <a href='/pipeline'>assign a salesperson and record outcomes</a>.</p>
    <details id='advanced-diagnostics'><summary>Advanced diagnostics · workers, memory and collection speed</summary><div class='cards'>
      <div class='card'><div class='label'>Browser control</div><div class='value'>{actual_workers} / {worker_limit}</div><div class='small'>Active / allowed · maximum {maximum_workers}</div></div>
      <div class='card'><div class='label'>Last hour / ETA</div><div class='value'>{int(status.get('qualified_last_hour',0)):,}</div><div class='small'>ETA: {esc(eta if eta is not None else 'calculating')} hours</div></div>
      <div class='card'><div class='label'>Estimated queries/hour (24h)</div><div class='value'>{query_rate:.1f}</div><div class='small'>{query_count:,} queries over {query_window:.2f} observed hours</div></div>
      <div class='card'><div class='label'>Available memory on this device</div><div class='value {ram_class}'>{esc(memory_label(status))}</div><div class='small'>{esc(status.get('memory_source','unavailable'))}. Includes reclaimable memory, not just unused RAM.</div></div>
      <div class='card'><div class='label'>Memory recoveries</div><div class='value'>{int(status.get('reclaimed_jobs',0)):,}</div><div class='small'>{int(status.get('memory_pressure_pauses',0)):,} pauses · historical minimum {esc(status.get('lowest_available_ram_gb') if status.get('lowest_available_ram_gb') is not None else 'unavailable')} GiB</div></div>
      <div class='card'><div class='label'>Renderer recycling</div><div class='value'>{int(status.get('page_recycle_count',0)):,}</div><div class='small'>{int(status.get('memory_cleanup_count',0)):,} in-feed cleanups · context every 10 details</div></div>
      <div class='card'><div class='label'>Fresh-location canary</div><div class='value'>{esc(scope_canary.get('status','pending'))}</div><div class='small'>{int(scope_canary.get('completed',0))} / 60 completed · {int(scope_canary.get('qualified_new',0))} new leads · {int(scope_canary.get('failures',0))} technical failures</div></div>
    </div><p class='small'>{'An older collector is running without verified cross-platform memory detection. Restart collection when convenient to apply the memory safety fix.' if status.get('collector_memory_legacy') and control['state'] in ('starting','running','pausing') else 'Memory protection uses the measured capacity; unavailable measurements block new browser submissions.'}</p></details>
    <div class='label'>Updated: {esc(status.get('updated_at'))} · {esc(status.get('halt_reason') or 'No halt reason')}</div>
    <h2>Reports and exports</h2>
    <div class='actions'>
      <a class='button' href='/export/call-list.csv'>Download A/B call list</a>
      <a class='button' href='/export/companies.csv'>Download companies CSV</a>
      <a class='button' href='/export/companies.xlsx'>Download companies Excel</a>
      <a class='button' href='/export/report.html'>Open summary report</a>
    </div>
    <div class='small'>The call list is organization-deduplicated and excludes suppressed or do-not-contact businesses. Full exports retain every physical location and its provenance.</div>
    <div class='cards' style='margin-top:18px'>
      <div class='card'><div class='label'>Links discovered</div><div class='value'>{int(totals[0]):,}</div></div>
      <div class='card'><div class='label'>Listings processed</div><div class='value'>{int(totals[1]):,}</div></div>
      <div class='card'><div class='label'>Duplicates</div><div class='value'>{int(totals[2]):,}</div></div>
      <div class='card'><div class='label'>Rejected</div><div class='value'>{int(totals[3]):,}</div></div>
    </div>
    <div class='grid2'><div><h2>Sales-readiness ranks</h2>{table(['Rank','Organizations / locations'],data.get('ranks',[]))}</div>
    <div><h2>Suggested LOCUS-T offer</h2>{table(['Offer','Locations'],[(friendly(offer),count) for offer,count in data.get('offers',[])])}</div></div>
    <div class='grid2'><div><h2>Sales pipeline</h2>{table(['Status','Organizations'],data.get('pipeline',[]))}</div>
    <div><h2>Priority-market coverage</h2>{table(['Market','Locations','A/B leads'],data.get('markets',[]))}</div></div>
    <div class='grid2'><div><h2>Lead tiers</h2>{table(['Tier','Locations'],[(friendly(tier),count) for tier,count in data['tiers']])}</div>
    <div><h2>States</h2>{table(['State','Locations'],data['states'])}</div></div>
    <h2>Industries</h2>{table(['Industry','Locations'],data['sectors'])}
    <details><summary>Recent collected locations</summary>{table(['Business','Industry','Area','Maps category','Lead tier','Phone','Website','Rating'],data['recent'])}</details>
    <details><summary>Search execution and worker events</summary><p><a href='/searches'>View the complete search manifest →</a></p>
    {table(['Prompt','Status','Links','Processed','New','Duplicates','Rejected','Worker','Completed','Error'],data['jobs'])}
    <h2>Worker and throttle events</h2>{table(['Event','Details','Workers','Recorded RAM GiB','Time'],data['events'])}</details>
    </main>
    <script>
    const refreshStarted = new Date();
    let collectorUpdate = {json.dumps(initial_updated_at)} ? new Date({json.dumps(initial_updated_at)}) : null;
    let countdown = 30;
    const fmtMYT = value => new Intl.DateTimeFormat('en-MY',{{timeZone:'Asia/Kuala_Lumpur',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}}).format(value);
    document.getElementById('dashboard-refresh').textContent = fmtMYT(refreshStarted) + ' MYT';
    function updateClock() {{
      const now = new Date();
      const age = collectorUpdate ? Math.max(0, Math.floor((now-collectorUpdate)/1000)) : 9999;
      document.getElementById('collector-update').textContent = collectorUpdate ? fmtMYT(collectorUpdate)+' MYT' : 'unknown';
      document.getElementById('heartbeat-age').textContent = age < 60 ? age+'s ago' : Math.floor(age/60)+'m '+(age%60)+'s ago';
      const dot = document.getElementById('heartbeat-dot');
      dot.className = 'dot ' + (age < 60 ? 'good' : age <= 180 ? 'warn' : 'bad');
      document.getElementById('refresh-countdown').textContent = countdown+'s';
      countdown -= 1;
      if (countdown < 0) {{ if (collectionDirty) countdown = 30; else location.reload(); }}
    }}
    async function refreshStatus() {{
      try {{
        const response = await fetch('/api/status',{{cache:'no-store'}});
        const live = await response.json();
        collectorUpdate = live.updated_at ? new Date(live.updated_at) : collectorUpdate;
        const actual = live.actual_browser_processes ?? live.active_workers ?? 0;
        const allowed = live.scheduler_worker_limit ?? actual;
        document.getElementById('header-workers').textContent = `${{actual}} active / ${{allowed}} allowed / ${{live.maximum_workers ?? 4}} max`;
        const ramState = live.ram_operating_state ?? 'unknown';
        const ramNode = document.getElementById('header-ram');
        ramNode.textContent = live.available_ram_gb == null ? 'Unavailable' : `${{Number(live.available_ram_gb).toFixed(2)}} GiB available (${{ramState}})`;
        ramNode.className = ramState === 'healthy' ? 'good' : ramState === 'critical' ? 'bad' : 'warn';
        document.getElementById('header-query-rate').textContent = `${{Number(live.query_rate_per_hour_24h ?? 0).toFixed(1)}}/hour`;
      }} catch (_) {{}}
    }}
    updateClock();
    setInterval(updateClock,1000);
    setInterval(refreshStatus,10000);
    </script></body></html>"""


def searches_page(query: str = "", page_number: int = 1) -> str:
    conn = connection()
    base = """WITH all_searches AS (
        SELECT collector_version AS taxonomy_version,'Legacy V2' AS source,prompt,sector,locality,state,term,
               geo_level,status,attempts,links_discovered,processed_count,qualified_new,
               duplicate_count,rejected_count,started_at,completed_at,error
          FROM jobs
        UNION ALL
        SELECT taxonomy_version,CASE taxonomy_version WHEN 4 THEN 'LOCUS-T V4' ELSE 'Taxonomy V3' END,prompt,sector,locality,state,term,
               geo_level,status,attempts,links_discovered,processed_count,qualified_new,
               duplicate_count,rejected_count,started_at,completed_at,error
          FROM search_jobs
    ) """
    where = " WHERE 1=1"
    params: tuple = ()
    if query:
        where += " AND (prompt LIKE ? OR sector LIKE ? OR status LIKE ? OR source LIKE ?)"
        term = f"%{query}%"
        params = (term, term, term, term)
    total = int(conn.execute(base + "SELECT COUNT(*) FROM all_searches" + where, params).fetchone()[0])
    per_page = 500
    pages = max(1, (total + per_page - 1) // per_page)
    page_number = min(max(1, page_number), pages)
    offset = (page_number - 1) * per_page
    sql = base + """SELECT taxonomy_version,source,prompt,sector,locality,state,term,geo_level,status,attempts,
                    links_discovered,processed_count,qualified_new,duplicate_count,
                    rejected_count,started_at,completed_at,error
             FROM all_searches""" + where
    sql += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, COALESCE(completed_at,started_at) DESC, prompt LIMIT ? OFFSET ?"
    rows = conn.execute(sql, params + (per_page, offset)).fetchall()
    conn.close()
    encoded_query = esc(query)
    previous_link = f"<a href='/searches?q={encoded_query}&page={page_number-1}'>← Previous</a>" if page_number > 1 else ""
    next_link = f"<a href='/searches?q={encoded_query}&page={page_number+1}'>Next →</a>" if page_number < pages else ""
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta http-equiv='refresh' content='60'>
    <title>All Searches — LOCUS-T V4</title><style>{CSS}</style></head><body>
    <header><h1>All Search Queries</h1>{navigation('searches')}<p>{total:,} matching queries · page {page_number:,} of {pages:,}</p></header>
    <main><form method='get'><input name='q' value='{encoded_query}' placeholder='Filter by prompt, industry, or status'></form>
    <p>{previous_link} &nbsp; {next_link}</p>
    {table(['Taxonomy','Source','Prompt','Industry','Area','State','Term','Geo level','Status','Attempts','Links','Processed','New','Duplicates','Rejected','Started','Completed','Error'],rows)}
    </main></body></html>"""


def page(title: str, body: str, active: str = "") -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title>
    <style>{CSS}</style></head><body><header><h1>{esc(title)}</h1>{navigation(active)}
    </header><main>{environment_notice()}{body}</main></body></html>"""


def ranked_leads_page(params: dict[str, list[str]]) -> str:
    filters = {key: values[0] for key, values in params.items() if values}
    clauses = ["li.intelligence_version=4"]
    values: list[object] = []
    for key, column in (
        ("rank", "li.sales_rank"), ("offer", "li.primary_offer"),
        ("industry", "COALESCE(ic.industry_label,c.sector)"),
        ("state", "c.state_name"), ("status", "sl.status"),
    ):
        if filters.get(key):
            clauses.append(f"{column}=?")
            values.append(filters[key])
    if filters.get("q"):
        clauses.append("(c.business_name LIKE ? OR c.phone LIKE ? OR c.website LIKE ?)")
        values.extend([f"%{filters['q']}%"] * 3)
    try:
        page_number = max(1, int(filters.get("page", "1")))
    except ValueError:
        page_number = 1
    conn = connection()
    where = " AND ".join(clauses)
    total = int(conn.execute(
        f"""SELECT COUNT(*) FROM lead_intelligence li JOIN companies c ON c.id=li.company_id
              LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
              LEFT JOIN sales_leads sl ON sl.organization_id=li.organization_id WHERE {where}""", values
    ).fetchone()[0])
    per_page = 200
    rows = conn.execute(
        f"""SELECT li.organization_id,c.business_name,COALESCE(ic.industry_label,c.sector),
                   li.primary_offer,li.sales_readiness_score,li.sales_rank,COALESCE(sl.status,'NEW'),
                   COALESCE(sl.assigned_pic,''),c.phone,COALESCE(wi.public_email,''),c.website,
                   c.locality,c.state_name,c.rating,c.reviews_count
                   ,c.id
            FROM lead_intelligence li JOIN companies c ON c.id=li.company_id
            LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
            LEFT JOIN sales_leads sl ON sl.organization_id=li.organization_id
            LEFT JOIN website_intelligence wi ON wi.company_id=c.id
            WHERE {where} ORDER BY li.sales_readiness_score DESC,c.id LIMIT ? OFFSET ?""",
        values + [per_page, (page_number - 1) * per_page],
    ).fetchall()
    options: dict[str, list[str]] = {}
    for name, sql in {
        "offer": "SELECT DISTINCT primary_offer FROM lead_intelligence WHERE intelligence_version=4 ORDER BY 1",
        "industry": "SELECT DISTINCT industry_label FROM company_industry_classification WHERE taxonomy_version=4 ORDER BY 1",
        "state": "SELECT DISTINCT state_name FROM companies WHERE state_name IS NOT NULL ORDER BY 1",
    }.items():
        options[name] = [str(row[0]) for row in conn.execute(sql)]
    conn.close()
    def choices(name: str, items: list[str]) -> str:
        selected = filters.get(name, "")
        plural = {'industry':'industries','status':'statuses'}.get(name, name + 's')
        return "<option value=''>All " + esc(plural) + "</option>" + "".join(
            f"<option value='{esc(item)}'{' selected' if item == selected else ''}>{esc(friendly(item))}</option>" for item in items
        )
    pages = max(1, (total + per_page - 1) // per_page)
    nav_params = "&".join(f"{esc(k)}={esc(v)}" for k, v in filters.items() if k != "page")
    previous = f"<a href='/leads?{nav_params}&page={page_number-1}'>Previous</a>" if page_number > 1 else ""
    next_link = f"<a href='/leads?{nav_params}&page={page_number+1}'>Next</a>" if page_number < pages else ""
    body = f"""<p class='help'>Review the most promising leads first, then open a business to assign a salesperson or record an outcome. A/B ranks are higher-priority opportunities; they are not confirmed customer interest.</p>
      <details><summary>What do the ranks mean?</summary><p>A: score 75+ · B: 60–74 · C: 45–59 · D: below 45. Scores combine reachability, service need, capacity, demand and freshness. Open a lead for its score breakdown.</p></details>
      <form method='get'><div class='filter-fields'><label>Search businesses<input type='search' name='q' value='{esc(filters.get('q',''))}' placeholder='Business, phone, or website'></label>
      <label>Sales-readiness rank<select name='rank'>{choices('rank', ['A','B','C','D'])}</select></label>
      <label>Suggested offer<select name='offer'>{choices('offer', options['offer'])}</select></label>
      <label>Industry<select name='industry'>{choices('industry', options['industry'])}</select></label>
      <label>State<select name='state'>{choices('state', options['state'])}</select></label>
      <label>Sales status<select name='status'>{choices('status', list(SALES_STATUSES))}</select></label></div>
      <button class='button' type='submit'>Filter</button></form>
      <p>{total:,} matching locations · page {page_number:,}/{pages:,} · {previous} &nbsp; {next_link}</p>
      <div class='actions'><a class='button' href='/export/call-list.csv'>Download all eligible A/B organizations</a></div>
      <p class='small'>The call-list export covers all eligible organizations, not just the filters above. Phone/email blanks mean no value has been collected.</p>
      {compact_leads(rows)}"""
    return page("Review leads", body, "leads")


def friendly(value: object) -> str:
    labels = {'ENRICHMENT_REQUIRED':'More research needed', 'WEBSITE_BUILD':'Website opportunity',
              'SEO_UPGRADE':'SEO opportunity', 'DO_NOT_CONTACT':'Do not contact',
              'PROPOSAL_SENT':'Proposal sent', 'high_value':'High-value searches',
              'volume_gated':'Volume-limited searches', 'long_tail':'Specific searches',
              'commercial':'Commercial searches', 'experimental':'Exploratory searches',
              'selected':'Selected campaign', 'custom':'Custom keywords'}
    return labels.get(str(value), str(value or '').replace('_',' ').title())


def contact_links(phone, email='', website='') -> str:
    links = []
    digits = re.sub(r'[^+0-9]', '', str(phone or ''))
    if len(digits) >= 3:
        links.append(f"<a class='nowrap' href='tel:{esc(digits)}'>{esc(phone)}</a>")
    if email and re.fullmatch(r'[^\s<>"\x27]+@[^\s<>"\x27]+\.[^\s<>"\x27]+', str(email)):
        links.append(f"<a href='mailto:{esc(email)}'>{esc(email)}</a>")
    if website and urlparse(str(website)).scheme in ('http','https') and urlparse(str(website)).netloc:
        links.append(f"<a href='{esc(website)}' target='_blank' rel='noopener noreferrer'>Website</a>")
    return '<br>'.join(links) or '<span class="muted">Contact details unavailable</span>'


def compact_leads(rows) -> str:
    desktop, mobile = [], []
    for row in rows:
        org, name, industry, offer, score, rank, status, owner, phone, email, website, city, state, rating, reviews, company_id = row
        link = f"<a href='/lead?id={int(company_id)}' aria-label='Review {esc(name)}'>{esc(name)}</a>"
        action = f"<a class='button' href='/lead?id={int(company_id)}'>Review</a>"
        contact = contact_links(phone, email)
        status_owner = f"{esc(friendly(status))}<br><span class='muted'>{esc(owner or 'Unassigned')}</span>"
        location = ', '.join(str(part) for part in (city,state) if part)
        cells = [link + f"<br><span class='muted'>{esc(industry)}</span>", esc(location),
                 f"<span class='badge'>{esc(rank)} · {int(score)}/100</span>", esc(friendly(offer)), status_owner, contact, action]
        desktop.append('<tr>' + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
        mobile.append(f"<article class='card lead-card'><h3>{link}</h3><p>{esc(industry)} · {esc(location)}</p><p>{esc(rank)} rank · {int(score)}/100 · {esc(friendly(offer))}</p><p>{status_owner}</p><p>{contact}</p>{action}</article>")
    if not rows:
        return "<section class='notice'><h3>No matching leads</h3><p>Clear a filter or <a href='/'>start a collection</a> to find businesses.</p></section>"
    headers = ['Business','Location','Priority','Suggested offer','Sales / owner','Contact','Action']
    head = ''.join('<th scope="col">'+esc(h)+'</th>' for h in headers)
    return f"<div class='desktop-leads table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(desktop)}</tbody></table></div><div class='mobile-leads'>{''.join(mobile)}</div>"


def lead_detail_page(company_id: int) -> str | None:
    conn = connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute('''SELECT c.*,li.organization_id,li.sales_rank,li.sales_readiness_score,
            li.primary_offer,li.reachability_score,li.service_need_score,li.capacity_score,
            li.demand_score,li.freshness_score,li.evidence_json,
            sl.status,sl.assigned_pic,sl.notes,sl.call_outcome,sl.next_action_at,
            sl.proposal_value,sl.updated_at AS sales_updated_at,sl.contact_count,
            COALESCE(wi.public_email,'') AS public_email,
            CASE WHEN cs.organization_id IS NULL THEN 0 ELSE 1 END AS suppressed
            FROM companies c JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
            JOIN sales_leads sl ON sl.organization_id=li.organization_id
            LEFT JOIN website_intelligence wi ON wi.company_id=c.id
            LEFT JOIN contact_suppression cs ON cs.organization_id=li.organization_id WHERE c.id=?''', (company_id,)).fetchone()
        if row is None:
            return None
        history = [tuple(item) for item in conn.execute('''SELECT old_status,new_status,outcome,notes,created_by,created_at
            FROM sales_activities WHERE organization_id=? ORDER BY id DESC LIMIT 50''', (row['organization_id'],))]
    finally:
        conn.close()
    statuses = ['DO_NOT_CONTACT'] if row['suppressed'] else SALES_STATUSES
    status_options = ''.join(f"<option value='{s}'{' selected' if s == row['status'] else ''}>{esc(friendly(s))}</option>" for s in statuses)
    parts = [('Reachability',row['reachability_score']),('Service need',row['service_need_score']),
             ('Capacity',row['capacity_score']),('Demand',row['demand_score']),('Freshness',row['freshness_score'])]
    try:
        evidence = json.loads(row['evidence_json'])
    except (TypeError, ValueError):
        evidence = {}
    suppressed_notice = "<p class='notice bad'>Do not contact: this organization is suppressed. You can update notes but cannot reactivate it here.</p>" if row['suppressed'] else ''
    body = f"""<p><a href='/leads'>← Review leads</a> · Organization #{int(row['organization_id'])} · sales updates apply to all locations of this organization.</p>
      <section class='card'><h2 style='margin-top:0'>Business details</h2><p>{esc(row['sector'])} · {esc(row['address'])}</p>
      <p>{contact_links(row['phone'],row['public_email'],row['website'])}</p><p><strong>{esc(row['sales_rank'])} rank · {row['sales_readiness_score']}/100</strong> · {esc(friendly(row['primary_offer']))}</p>
      <details><summary>Why this rank and suggested offer?</summary><p>Automated prioritisation, not confirmed customer interest. A: 75+; B: 60–74; C: 45–59; D: below 45. Component points add up to the score.</p>{table(['Component','Points'],parts)}<pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{esc(json.dumps(evidence,ensure_ascii=False,indent=2))}</pre></details></section>
      {suppressed_notice}<section class='card' style='margin-top:18px'><h2 style='margin-top:0'>Assign and record sales outcomes</h2>
      <p>Contact happens outside this dashboard. Saving records your team's update; it sends no messages.</p>
      <form id='sales-form'><div class='grid2 form-grid'>
        <label>Assigned salesperson<input name='assigned_pic' maxlength='120' value='{esc(row['assigned_pic'])}' placeholder='Name of the person responsible'></label>
        <div><label for='sales-status'>Sales status</label><select id='sales-status' name='status'>{status_options}</select></div>
        <label>Contact outcome<input name='call_outcome' maxlength='240' value='{esc(row['call_outcome'])}' placeholder='Connected, no answer, interested…'></label>
        <label>Next follow-up (Malaysia time)<input name='next_action_at' type='datetime-local' value='{esc(myt_input(row['next_action_at']))}'></label>
        <label>Proposal value (MYR)<input name='proposal_value' type='number' min='0' max='1000000000000' step='0.01' value='{esc(row['proposal_value'])}'></label>
        <label>Contact notes<textarea name='notes' maxlength='2000'>{esc(row['notes'])}</textarea></label>
      </div><p><label><input type='checkbox' name='record_contact' {'disabled' if row['suppressed'] else ''}>Record a contact made outside this dashboard</label></p>
      <button class='button' type='submit'>Save sales update</button><p id='sales-result' role='status' aria-live='polite'></p>
      <p class='small'>{int(row['contact_count'])} recorded contacts. <a href='/lead?id={company_id}'>Reload lead and activity history</a>.</p></form></section>
      <section id='activity-history'><h2>Recent activity</h2>{table(['From','To','Outcome','Notes','Salesperson','Time'],history) if history else '<p class="notice">No sales activity yet. Assign a salesperson above, then record the first outcome.</p>'}</section>
      <script>
      let salesVersion = {json.dumps(row['sales_updated_at'])};
      const form = document.getElementById('sales-form');
      form.addEventListener('submit', async event => {{
        event.preventDefault();const resultNode = document.getElementById('sales-result');
        const button = form.querySelector('button');button.disabled = true;
        const values = Object.fromEntries(new FormData(form));
        values.organization_id = {int(row['organization_id'])};values.expected_updated_at = salesVersion;
        values.record_contact = form.elements.record_contact.checked;
        values.next_action_at = values.next_action_at ? values.next_action_at + ':00+08:00' : '';
        try {{
          const response = await fetch('/api/sales/update',{{method:'POST',headers:{{'Content-Type':'application/json','X-Collector-Token':{json.dumps(COLLECTOR_TOKEN)}}},body:JSON.stringify(values)}});
          const result = await response.json();if (!response.ok) throw new Error(result.error || 'Save failed');
          salesVersion = result.updated_at;form.elements.record_contact.checked = false;
          resultNode.textContent = 'Saved. Reload to see the updated activity history.';
          if (values.status === 'DO_NOT_CONTACT') {{form.elements.record_contact.disabled = true;Array.from(form.elements.status.options).forEach(option => option.disabled = option.value !== 'DO_NOT_CONTACT');}}
        }} catch (error) {{resultNode.textContent = error.message;}} finally {{button.disabled = false;}}
      }});
      </script>"""
    return page(row['business_name'], body, 'leads')


def myt_input(value) -> str:
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')
    except ValueError:
        return ''


CALL_LIST_COLUMNS = [
    "Organization ID", "Company ID", "Business name", "Industry", "Primary offer",
    "Sales score", "Sales rank", "Sales status", "Assigned PIC", "Phone", "Public email",
    "WhatsApp", "Website", "Address", "Locality", "State", "Rating", "Review count",
    "Maps URL", "Next action at", "Call outcome", "Requirement", "Budget range",
    "Proposal value", "Proposal URL", "Lost reason", "Notes",
]


def call_list_query() -> str:
    return """SELECT li.organization_id,c.id,c.business_name,COALESCE(ic.industry_label,c.sector),
              li.primary_offer,li.sales_readiness_score,li.sales_rank,sl.status,COALESCE(sl.assigned_pic,''),
              c.phone,COALESCE(wi.public_email,''),COALESCE(wi.whatsapp_url,''),c.website,c.address,
              c.locality,c.state_name,c.rating,c.reviews_count,c.source_url,COALESCE(sl.next_action_at,''),
              COALESCE(sl.call_outcome,''),COALESCE(sl.requirement,''),COALESCE(sl.budget_range,''),
              COALESCE(sl.proposal_value,''),COALESCE(sl.proposal_url,''),COALESCE(sl.lost_reason,''),COALESCE(sl.notes,'')
       FROM sales_leads sl JOIN lead_intelligence li
         ON li.company_id=sl.representative_company_id AND li.intelligence_version=4
       JOIN companies c ON c.id=sl.representative_company_id
       LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
       LEFT JOIN website_intelligence wi ON wi.company_id=c.id
       LEFT JOIN contact_suppression cs ON cs.organization_id=sl.organization_id
       WHERE li.sales_rank IN ('A','B') AND sl.status NOT IN ('DO_NOT_CONTACT','LOST')
         AND cs.organization_id IS NULL
       ORDER BY li.sales_readiness_score DESC,sl.organization_id"""


def stream_call_list_csv(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="locus-t-ab-call-list.csv"')
    handler.send_header("Connection", "close")
    handler.end_headers()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CALL_LIST_COLUMNS)
    handler.wfile.write(("\ufeff" + output.getvalue()).encode("utf-8"))
    conn = connection()
    try:
        for row in conn.execute(call_list_query()):
            output.seek(0); output.truncate(0)
            writer.writerow(["" if value is None else value for value in row])
            handler.wfile.write(output.getvalue().encode("utf-8"))
    finally:
        conn.close()


def pipeline_page(message: str = "") -> str:
    conn = connection()
    funnel = conn.execute(
        """SELECT status,COUNT(*),SUM(COALESCE(proposal_value,0)) FROM sales_leads
           GROUP BY status ORDER BY CASE status WHEN 'NEW' THEN 0 WHEN 'REVIEWED' THEN 1
           WHEN 'CONTACTED' THEN 2 WHEN 'QUALIFIED' THEN 3 WHEN 'DISCOVERY' THEN 4
           WHEN 'PROPOSAL_SENT' THEN 5 WHEN 'WON' THEN 6 WHEN 'LOST' THEN 7 ELSE 8 END"""
    ).fetchall()
    by_pic = conn.execute(
        """SELECT COALESCE(NULLIF(assigned_pic,''),'Unassigned'),COUNT(*),
                  SUM(CASE WHEN status IN ('QUALIFIED','DISCOVERY','PROPOSAL_SENT','WON') THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END),SUM(COALESCE(proposal_value,0))
           FROM sales_leads GROUP BY assigned_pic ORDER BY COUNT(*) DESC"""
    ).fetchall()
    metric_sql = """SELECT {dimension},COUNT(*) leads,
          ROUND(100.0*SUM(CASE WHEN sl.contact_count>0 OR sl.status NOT IN ('NEW','REVIEWED') THEN 1 ELSE 0 END)/COUNT(*),1),
          ROUND(100.0*SUM(CASE WHEN sl.status IN ('QUALIFIED','DISCOVERY','PROPOSAL_SENT','WON') THEN 1 ELSE 0 END)/COUNT(*),1),
          ROUND(100.0*SUM(CASE WHEN sl.status IN ('PROPOSAL_SENT','WON') THEN 1 ELSE 0 END)/COUNT(*),1),
          ROUND(100.0*SUM(CASE WHEN sl.status='WON' THEN 1 ELSE 0 END)/COUNT(*),1),
          ROUND(100.0*SUM(CASE WHEN sl.status='WON' THEN COALESCE(sl.proposal_value,0) ELSE 0 END)/COUNT(*),2)
       FROM sales_leads sl JOIN companies c ON c.id=sl.representative_company_id
       JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
       LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
       GROUP BY {dimension} ORDER BY leads DESC"""
    conversion_by_industry = conn.execute(metric_sql.format(
        dimension="COALESCE(ic.industry_label,c.sector)"
    )).fetchall()
    conversion_by_offer = conn.execute(metric_sql.format(dimension="li.primary_offer")).fetchall()
    conversion_by_market = conn.execute(metric_sql.format(
        dimension="CASE WHEN c.state_name IN ('Selangor','Federal Territory') THEN 'Klang Valley' ELSE c.state_name END"
    )).fetchall()
    by_query_term = conn.execute(
        """WITH first_source AS (
             SELECT p.company_id,MIN(p.prompt) prompt FROM provenance p GROUP BY p.company_id)
           SELECT COALESCE(sj.term,'Legacy/unmapped'),COUNT(DISTINCT sl.organization_id),
             ROUND(100.0*COUNT(DISTINCT CASE WHEN sl.contact_count>0 OR sl.status NOT IN ('NEW','REVIEWED') THEN sl.organization_id END)/COUNT(DISTINCT sl.organization_id),1),
             ROUND(100.0*COUNT(DISTINCT CASE WHEN sl.status IN ('QUALIFIED','DISCOVERY','PROPOSAL_SENT','WON') THEN sl.organization_id END)/COUNT(DISTINCT sl.organization_id),1),
             ROUND(100.0*COUNT(DISTINCT CASE WHEN sl.status IN ('PROPOSAL_SENT','WON') THEN sl.organization_id END)/COUNT(DISTINCT sl.organization_id),1),
             ROUND(100.0*COUNT(DISTINCT CASE WHEN sl.status='WON' THEN sl.organization_id END)/COUNT(DISTINCT sl.organization_id),1),
             ROUND(100.0*SUM(CASE WHEN sl.status='WON' THEN COALESCE(sl.proposal_value,0) ELSE 0 END)/COUNT(DISTINCT sl.organization_id),2)
           FROM sales_leads sl JOIN first_source fs ON fs.company_id=sl.representative_company_id
           LEFT JOIN (SELECT prompt,MAX(term) term FROM search_jobs GROUP BY prompt) sj ON sj.prompt=fs.prompt
           GROUP BY COALESCE(sj.term,'Legacy/unmapped') ORDER BY COUNT(DISTINCT sl.organization_id) DESC LIMIT 50"""
    ).fetchall()
    recent = conn.execute(
        """SELECT sa.organization_id,o.display_name,sa.old_status,sa.new_status,sa.outcome,
                  sa.notes,sa.created_by,sa.created_at
           FROM sales_activities sa LEFT JOIN organizations o ON o.id=sa.organization_id
           ORDER BY sa.id DESC LIMIT 100"""
    ).fetchall()
    actionable = conn.execute('''SELECT li.organization_id,c.business_name,COALESCE(ic.industry_label,c.sector),
        li.primary_offer,li.sales_readiness_score,li.sales_rank,sl.status,COALESCE(sl.assigned_pic,''),
        c.phone,COALESCE(wi.public_email,''),c.website,c.locality,c.state_name,c.rating,c.reviews_count,c.id
        FROM sales_leads sl JOIN companies c ON c.id=sl.representative_company_id
        JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
        LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
        LEFT JOIN website_intelligence wi ON wi.company_id=c.id
        WHERE sl.status NOT IN ('WON','LOST','DO_NOT_CONTACT')
        ORDER BY CASE WHEN COALESCE(sl.assigned_pic,'')='' THEN 0 ELSE 1 END,
                 li.sales_readiness_score DESC LIMIT 50''').fetchall()
    conn.close()
    notice = f"<p class='good'>{esc(message)}</p>" if message else ""
    sample = ",".join(CALL_LIST_COLUMNS) + "\n1,1,Example Business,Construction,SEO_UPGRADE,80,A,CONTACTED,PIC Name,0123456789,,,,,,,,,,,Connected,SEO proposal,RM5k-RM10k,,,Follow up next week"
    body = f"""{notice}<section class='notice'><strong>Next step: assign leads and record outcomes.</strong><p>Open Review below to assign a salesperson, change status, or log contact notes. Contact businesses outside this dashboard; email and WhatsApp sending are not enabled.</p></section>
      <div class='grid2'><div><h2>Sales funnel</h2>{table(['Status','Organizations','Proposal value (MYR)'],[(friendly(s),n,v) for s,n,v in funnel])}</div>
      <div><h2>By salesperson</h2>{table(['Salesperson','Leads','Qualified+','Won','Proposal value (MYR)'],by_pic)}</div></div>
      <section id='pipeline-leads'><h2>Assign and follow up</h2><p>First 50 open organizations, with unassigned leads first. <a href='/leads'>Search and filter all leads →</a></p>{compact_leads(actionable)}</section>
      <details><summary>CSV handoff · download or import team updates</summary><div class='actions'><a class='button' href='/export/call-list.csv'>Download all eligible A/B organizations</a></div>
      <p>CSV import/export remains available for offline team handoff. Valid statuses: {esc(', '.join(SALES_STATUSES))}. Re-importing records an append-only activity.</p>
      <form id='import-form'><input id='csv-file' type='file' accept='.csv,text/csv' required>
      <button class='button' type='submit'>Import team updates</button></form><pre style='display:none'>{esc(sample)}</pre></details>
      <details id='conversion-analytics'><summary>Conversion analytics · industry, offer, market and search term</summary>
      <p>Rates reflect recorded sales updates, not automatically tracked conversations. Zero means no conversion has been recorded. Proposal and revenue amounts are in MYR.</p>
      <h2>Conversion by industry</h2>{table(['Industry','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100 leads'],conversion_by_industry)}
      <div class='grid2'><div><h2>Conversion by primary offer</h2>{table(['Offer','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],conversion_by_offer)}</div>
      <div><h2>Conversion by market</h2>{table(['Market','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],conversion_by_market)}</div></div>
      <h2>Conversion by source query term</h2>{table(['Term','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],by_query_term)}
      </details><h2>Recent sales activity</h2>{table(['Org ID','Organization','From','To','Outcome','Notes','Salesperson','Time'],recent) if recent else '<p class="notice">No sales activity yet. Open a lead above, assign a salesperson, and record the first outcome.</p>'}
      <script>document.getElementById('import-form').addEventListener('submit',async event=>{{event.preventDefault();const file=document.getElementById('csv-file').files[0];
      const response=await fetch('/import/sales-updates',{{method:'POST',headers:{{'Content-Type':'text/csv; charset=utf-8'}},body:await file.text()}});
      document.open();document.write(await response.text());document.close();}});</script>"""
    return page("Assign and track sales", body, "pipeline")


def coverage_page(started_only: bool = False) -> str:
    conn = connection()
    having = " HAVING SUM(CASE WHEN status IN ('running','completed','failed') THEN 1 ELSE 0 END)>0" if started_only else ''
    market = conn.execute(
        """SELECT CASE WHEN state IN ('Selangor','Federal Territory') THEN 'Klang Valley'
                         WHEN state IN ('Perak','Pahang','Kedah','Negeri Sembilan','Melaka')
                           THEN 'Peninsular Expansion' ELSE state END,
                  COUNT(*),SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                  SUM(qualified_new),SUM(ab_leads_new),ROUND(AVG(expected_ab_yield),2)
           FROM search_jobs WHERE taxonomy_version=4 GROUP BY 1""" + having + " ORDER BY COUNT(*) DESC"
    ).fetchall()
    yield_rows = conn.execute(
        """SELECT sector,strategy_bucket,COUNT(*),SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                  SUM(qualified_new),SUM(ab_leads_new),ROUND(AVG(expected_ab_yield),2),
                  ROUND(100.0*SUM(duplicate_count)/NULLIF(SUM(processed_count),0),1)
           FROM search_jobs WHERE taxonomy_version=4 GROUP BY sector,strategy_bucket""" + having +
           " ORDER BY SUM(ab_leads_new) DESC,AVG(expected_ab_yield) DESC LIMIT 100"
    ).fetchall()
    coverage = conn.execute(
        """SELECT c.state_name,COALESCE(ic.industry_label,c.sector),COUNT(*),
                  SUM(CASE WHEN li.sales_rank IN ('A','B') THEN 1 ELSE 0 END)
           FROM companies c LEFT JOIN company_industry_classification ic
             ON ic.company_id=c.id AND ic.taxonomy_version=4
           LEFT JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
           GROUP BY c.state_name,2 ORDER BY c.state_name,COUNT(*) DESC"""
    ).fetchall()
    conn.close()
    body = f"""<p class='help'>Use this advanced view to understand where searches are planned and which searches produce sales-ready leads. A planned search is not collected coverage.</p>
      <form method='get'><label><input type='checkbox' name='started' value='1' {'checked' if started_only else ''}>Hide groups with no started searches</label><button class='button'>Apply</button></form>
      <details><summary>Definitions and default search allocation</summary><p>Completed = finished searches. Collected = new locations that passed collection checks. A/B leads = new higher-priority ranked locations, not confirmed prospects. Estimated A/B per search = average predicted yield, not a total or a guarantee. Duplicate rate = duplicate observations divided by processed listings.</p><p>The broad default plan rotates approximately 30% Klang Valley, 20% Johor, 10% Penang and 40% Peninsular expansion searches while work remains. Selected campaigns use your chosen areas instead.</p></details>
      <h2>Search plan versus collected results</h2>{table(['Market','Planned searches','Completed','Collected locations','New A/B locations','Estimated A/B per search'],market)}
      <details><summary>Industry and strategy yield</summary>{table(['Industry','Strategy','Planned searches','Completed','Collected locations','New A/B locations','Estimated A/B per search','Duplicate %'],[(sector,friendly(strategy),*values) for sector,strategy,*values in yield_rows])}</details>
      <h2>Collected lead coverage — all states</h2>{table(['State','Industry','Locations','A/B-ranked locations'],coverage)}"""
    return page("Coverage and query yield", body, "coverage")


def import_sales_updates(raw: bytes) -> tuple[int, list[str]]:
    text_value = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_value))
    normalized_headers = {str(name).strip().lower().replace(" ", "_"): name for name in (reader.fieldnames or [])}
    if "organization_id" not in normalized_headers or "sales_status" not in normalized_headers:
        raise ValueError("CSV must contain Organization ID and Sales status columns")
    conn = write_connection()
    updated = 0
    errors: list[str] = []
    try:
        for row_number, source in enumerate(reader, 2):
            row = {str(key).strip().lower().replace(" ", "_"): value for key, value in source.items()}
            status = (row.get("sales_status") or "").strip().upper()
            if not status:
                continue
            payload = {
                "organization_id": row.get("organization_id"), "status": status,
                "assigned_pic": row.get("assigned_pic"), "next_action_at": row.get("next_action_at"),
                "call_outcome": row.get("call_outcome"), "requirement": row.get("requirement"),
                "budget_range": row.get("budget_range"), "proposal_value": row.get("proposal_value"),
                "proposal_url": row.get("proposal_url"), "lost_reason": row.get("lost_reason"),
                "notes": row.get("notes"),
            }
            try:
                update_sales_lead(conn, payload, actor=row.get("assigned_pic") or "PIC CSV import")
                updated += 1
            except Exception as exc:
                errors.append(f"Row {row_number}: {exc}")
    finally:
        conn.close()
    return updated, errors


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not DB_PATH.exists() and parsed.path in ('/leads','/pipeline','/coverage','/searches','/lead'):
                if parsed.path == '/lead':
                    self.send_error(404, 'Lead not found')
                else:
                    active = {'/leads':'leads','/pipeline':'pipeline','/coverage':'coverage','/searches':'searches'}[parsed.path]
                    self.respond(page('No leads yet', "<section class='card'><h2>Start your first collection</h2><p>Choose industries and locations, preview the searches, then start collection. Your leads will appear here when collected.</p><a class='button' href='/'>Set up collection</a></section>",active), 'text/html; charset=utf-8')
                return
            if parsed.path in ("/", "/index.html"):
                self.respond(dashboard_page(dashboard_data()), "text/html; charset=utf-8")
            elif parsed.path == "/searches":
                query = parse_qs(parsed.query).get("q", [""])[0]
                try:
                    page_number = int(parse_qs(parsed.query).get("page", ["1"])[0])
                except ValueError:
                    page_number = 1
                self.respond(searches_page(query, page_number), "text/html; charset=utf-8")
            elif parsed.path == "/leads":
                self.respond(ranked_leads_page(parse_qs(parsed.query)), "text/html; charset=utf-8")
            elif parsed.path == '/lead':
                try:
                    company_id = int(parse_qs(parsed.query).get('id', ['0'])[0])
                except ValueError:
                    self.send_error(400, 'Invalid lead ID')
                    return
                detail = lead_detail_page(company_id)
                if detail is None:
                    self.send_error(404, 'Lead not found')
                    return
                self.respond(detail, 'text/html; charset=utf-8')
            elif parsed.path == "/pipeline":
                self.respond(pipeline_page(), "text/html; charset=utf-8")
            elif parsed.path == "/coverage":
                self.respond(coverage_page(parse_qs(parsed.query).get('started') == ['1']), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                self.respond(json.dumps(load_status()), "application/json; charset=utf-8")
            elif parsed.path == '/api/collector':
                self.respond(json.dumps(collector_controller().snapshot()), 'application/json; charset=utf-8')
            elif parsed.path == "/export/companies.csv":
                stream_company_csv(self)
            elif parsed.path == "/export/call-list.csv":
                stream_call_list_csv(self)
            elif parsed.path == "/export/companies.xlsx":
                # Each request owns its file: concurrent downloads cannot truncate
                # one another, and disconnects still remove the temporary export.
                with tempfile.TemporaryDirectory(prefix="sherlockmaps-export-") as folder:
                    export_path = Path(folder) / "sherlockmaps-companies.xlsx"
                    company_export_xlsx(export_path)
                    self.download_file(export_path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sherlockmaps-companies.xlsx")
            elif parsed.path == "/export/report.html":
                report = report_page(dashboard_data()).encode("utf-8")
                self.download(report, "text/html; charset=utf-8", "sherlockmaps-report.html", inline=True)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            self.send_error(500, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ('/api/collector/start', '/api/collector/pause', '/api/collector/preview', '/api/sales/update'):
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            hostname = urlparse('http://' + host).hostname
            token = self.headers.get('X-Collector-Token', '')
            if (hostname not in ('127.0.0.1', 'localhost', '::1')
                    or (origin and urlparse(origin).netloc != host)
                    or not secrets.compare_digest(token, COLLECTOR_TOKEN)):
                self.send_error(403, 'Collector controls require a local dashboard request')
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Expected a JSON request of at most 64 KiB')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object')
                control = collector_controller()
                if parsed.path == '/api/sales/update':
                    if not DB_PATH.exists():
                        raise ValueError('No leads yet. Start a collection before editing sales records.')
                    conn = write_connection()
                    try:
                        result = save_sales_update(conn, payload)
                    finally:
                        conn.close()
                    global _dashboard_cache_at
                    _dashboard_cache_at = 0.0
                elif parsed.path.endswith('/pause'):
                    result = control.pause()
                elif parsed.path.endswith('/preview'):
                    result = preview_selection(payload.get('selection'), DB_PATH)
                else:
                    result = control.start(payload.get('target', 400_000), payload.get('workers', 5),
                                           selection=payload.get('selection'))
                self.respond(json.dumps(result), 'application/json; charset=utf-8')
            except StaleSalesEdit as exc:
                self.send_response(409)
                self.respond_json_error(str(exc))
            except (ValueError, TypeError) as exc:
                self.send_response(400)
                self.respond_json_error(str(exc))
            except (OSError, sqlite3.Error) as exc:
                self.send_response(500)
                self.respond_json_error(str(exc))
            return
        if parsed.path != "/import/sales-updates":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 100 * 1024 * 1024:
                raise ValueError("CSV is empty or exceeds 100 MB")
            updated, errors = import_sales_updates(self.rfile.read(size))
            message = f"Imported {updated:,} sales updates."
            if errors:
                message += f" {len(errors):,} rows failed: " + "; ".join(errors[:10])
            self.respond(pipeline_page(message), "text/html; charset=utf-8")
        except Exception as exc:
            self.respond(page("Sales import failed", f"<p class='bad'>{esc(exc)}</p><p><a href='/pipeline'>Return to pipeline</a></p>", "pipeline"), "text/html; charset=utf-8")

    def respond(self, body: str, content_type: str) -> None:
        content = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def respond_json_error(self, message: str) -> None:
        content = json.dumps({'error': message}).encode('utf-8')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def download(self, content: bytes, content_type: str, filename: str, inline: bool = False) -> None:
        disposition = "inline" if inline else "attachment"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def download_file(self, path: Path, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Connection", "close")
        self.end_headers()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        self.wfile.flush()

    def log_message(self, *_: object) -> None:
        return


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, default=DB_PATH)
    parser.add_argument('--status-file', type=Path)
    args = parser.parse_args()
    DB_PATH = args.database.resolve()
    if args.status_file:
        STATUS_PATH = args.status_file.resolve()
    elif args.database != ROOT / 'data' / 'malaysia_qualified_companies.sqlite':
        STATUS_PATH = DB_PATH.with_suffix(DB_PATH.suffix + '.status.json')
    print(f"Dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
