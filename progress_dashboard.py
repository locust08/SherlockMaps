"""V4 collection and sales-operations dashboard for LOCUS-T Malaysia leads."""

from __future__ import annotations

import html
import csv
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as xml_escape

from lead_intelligence_v4 import INTELLIGENCE_VERSION, SALES_STATUSES, update_sales_lead

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "malaysia_qualified_companies.sqlite"
STATUS_PATH = ROOT / "data" / "malaysia_batch_status.json"
PORT = 8765


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def table(headers: list[str], rows: list[tuple]) -> str:
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    if not rows:
        body = f"<tr><td colspan='{len(headers)}'>No data yet</td></tr>"
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


NAV_ITEMS = (
    ("dashboard", "/", "Dashboard"),
    ("leads", "/leads", "Ranked Leads"),
    ("pipeline", "/pipeline", "Sales Pipeline"),
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


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
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


def company_export_xlsx() -> bytes:
    # Build a standards-compliant XLSX using only the Python standard library.
    rows = io.StringIO()
    rows.write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    headers = [label for label, _ in COMPANY_EXPORT_COLUMNS]
    rows.write('<row r="1">')
    for index, value in enumerate(headers, 1):
        rows.write(xlsx_cell(value, f"{xlsx_column(index)}1"))
    rows.write('</row>')
    conn = connection()
    for row_number, row in enumerate(conn.execute(company_export_query()), 2):
        rows.write(f'<row r="{row_number}">')
        for index, value in enumerate(row, 1):
            rows.write(xlsx_cell(value, f"{xlsx_column(index)}{row_number}"))
        rows.write('</row>')
    conn.close()
    rows.write('</sheetData></worksheet>')
    worksheet = rows.getvalue().encode("utf-8")
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
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


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
    if not STATUS_PATH.exists():
        return {}
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


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
                    WHEN c.state_name='Penang' THEN 'Penang' ELSE 'Other (legacy)' END market,
                  COUNT(*),SUM(CASE WHEN li.sales_rank IN ('A','B') THEN 1 ELSE 0 END)
           FROM companies c LEFT JOIN lead_intelligence li
             ON li.company_id=c.id AND li.intelligence_version=4
           GROUP BY market ORDER BY COUNT(*) DESC"""
    ).fetchall()
    conn.close()
    return {"status": status, "sectors": sectors, "tiers": tiers, "states": states,
            "recent": recent, "jobs": jobs, "events": events, "totals": totals,
            "ranks": ranks, "offers": offers, "pipeline": pipeline, "markets": markets}


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
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
"""


def dashboard_page(data: dict) -> str:
    status = data["status"]
    qualified = int(status.get("qualified_companies", 0))
    target = int(status.get("target_qualified_companies", 400_000))
    progress = min(100.0, qualified / target * 100) if target else 0
    state, state_class = collector_state(status)
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
    return f"""<!doctype html><html><head><meta charset='utf-8'>
    <title>LOCUS-T Lead Intelligence V4</title><style>{CSS}</style></head><body>
    <header><h1>LOCUS-T Lead Intelligence V4</h1>{navigation('dashboard')}<p>400,000 sales-ready Malaysian locations · focus: Klang Valley 55%, Johor 25%, Penang 20%</p>
    <div class='status-strip'>
      <span>Dashboard refreshed: <strong id='dashboard-refresh'>--</strong></span>
      <span><i id='heartbeat-dot' class='dot {state_class}'></i>Collector updated: <strong id='collector-update'>--</strong> (<span id='heartbeat-age'>--</span>)</span>
      <span>Next full refresh: <strong id='refresh-countdown'>30s</strong></span>
      <span>Browsers: <strong id='header-workers'>{actual_workers} active / {worker_limit} allowed / {maximum_workers} max</strong></span>
      <span>Free RAM: <strong id='header-ram' class='{ram_class}'>{esc(status.get('available_ram_gb','?'))} GB ({esc(ram_state)})</strong></span>
      <span>24h query rate: <strong id='header-query-rate'>{query_rate:.1f}/hour</strong></span>
    </div></header>
    <main><div class='cards'>
      <div class='card'><div class='label'>Qualified locations</div><div class='value'>{qualified:,}</div></div>
      <div class='card'><div class='label'>Target / progress</div><div class='value'>{target:,}</div><div class='small'>{progress:.1f}%</div></div>
      <div class='card'><div class='label'>Collector</div><div class='value {state_class}'>{state}</div><div class='small'>Pilot: {esc(status.get('pilot_status','pending'))}</div></div>
      <div class='card'><div class='label'>Browser control</div><div class='value'>{actual_workers} / {worker_limit}</div><div class='small'>Active / allowed · maximum {maximum_workers}</div></div>
      <div class='card'><div class='label'>Last hour / ETA</div><div class='value'>{int(status.get('qualified_last_hour',0)):,}</div><div class='small'>ETA: {esc(eta if eta is not None else 'calculating')} hours</div></div>
      <div class='card'><div class='label'>Estimated queries/hour (24h)</div><div class='value'>{query_rate:.1f}</div><div class='small'>{query_count:,} queries over {query_window:.2f} observed hours</div></div>
      <div class='card'><div class='label'>RAM operating state</div><div class='value {ram_class}'>{esc(ram_state.title())}</div><div class='small'>{esc(status.get('available_ram_gb','?'))} GB free · {esc(status.get('reserved_ram_gb',2))} GB reserved · {esc(status.get('ram_policy_mode','balanced'))} mode</div></div>
      <div class='card'><div class='label'>Memory recoveries</div><div class='value'>{int(status.get('reclaimed_jobs',0)):,}</div><div class='small'>{int(status.get('memory_pressure_pauses',0)):,} pauses · lowest {esc(status.get('lowest_available_ram_gb','?'))} GB at {esc(status.get('lowest_available_ram_at') or 'n/a')}</div></div>
      <div class='card'><div class='label'>Renderer recycling</div><div class='value'>{int(status.get('page_recycle_count',0)):,}</div><div class='small'>{int(status.get('memory_cleanup_count',0)):,} in-feed cleanups · context every 10 details</div></div>
    </div>
    <div class='bar'><span style='width:{progress:.2f}%'></span></div>
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
    <div><h2>Primary LOCUS-T offer</h2>{table(['Offer','Leads'],data.get('offers',[]))}</div></div>
    <div class='grid2'><div><h2>Sales pipeline</h2>{table(['Status','Organizations'],data.get('pipeline',[]))}</div>
    <div><h2>Priority-market coverage</h2>{table(['Market','Locations','A/B leads'],data.get('markets',[]))}</div></div>
    <div class='grid2'><div><h2>Lead tiers</h2>{table(['Tier','Locations'],data['tiers'])}</div>
    <div><h2>States</h2>{table(['State','Locations'],data['states'])}</div></div>
    <h2>Industries</h2>{table(['Industry','Locations'],data['sectors'])}
    <h2>Latest qualified locations</h2>{table(['Business','Industry','Area','Maps category','Lead tier','Phone','Website','Rating'],data['recent'])}
    <h2>Latest searches</h2><p><a href='/searches'>View the complete search manifest →</a></p>
    {table(['Prompt','Status','Links','Processed','New','Duplicates','Rejected','Worker','Completed','Error'],data['jobs'])}
    <h2>Worker and throttle events</h2>{table(['Event','Details','Workers','Free RAM GB','Time'],data['events'])}
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
      if (countdown < 0) location.reload();
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
        ramNode.textContent = `${{live.available_ram_gb ?? '?'}} GB (${{ramState}})`;
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
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>
    <style>{CSS}</style></head><body><header><h1>{esc(title)}</h1>{navigation(active)}
    </header><main>{body}</main></body></html>"""


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
        return "<option value=''>All " + esc(name) + "s</option>" + "".join(
            f"<option value='{esc(item)}'{' selected' if item == selected else ''}>{esc(item)}</option>" for item in items
        )
    pages = max(1, (total + per_page - 1) // per_page)
    nav_params = "&".join(f"{esc(k)}={esc(v)}" for k, v in filters.items() if k != "page")
    previous = f"<a href='/leads?{nav_params}&page={page_number-1}'>Previous</a>" if page_number > 1 else ""
    next_link = f"<a href='/leads?{nav_params}&page={page_number+1}'>Next</a>" if page_number < pages else ""
    body = f"""<form method='get'><input type='text' name='q' value='{esc(filters.get('q',''))}' placeholder='Business, phone, or website'>
      <select name='rank'>{choices('rank', ['A','B','C','D'])}</select>
      <select name='offer'>{choices('offer', options['offer'])}</select>
      <select name='industry'>{choices('industry', options['industry'])}</select>
      <select name='state'>{choices('state', options['state'])}</select>
      <select name='status'>{choices('status', list(SALES_STATUSES))}</select>
      <button class='button' type='submit'>Filter</button></form>
      <p>{total:,} matching locations · page {page_number:,}/{pages:,} · {previous} &nbsp; {next_link}</p>
      <div class='actions'><a class='button' href='/export/call-list.csv'>Download brand-deduplicated A/B call list</a></div>
      {table(['Org ID','Business','Industry','Offer','Score','Rank','Status','PIC','Phone','Email','Website','Locality','State','Rating','Reviews'], rows)}"""
    return page("Ranked LOCUS-T leads", body, "leads")


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
        """SELECT COALESCE(assigned_pic,'Unassigned'),COUNT(*),
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
    conn.close()
    notice = f"<p class='good'>{esc(message)}</p>" if message else ""
    sample = ",".join(CALL_LIST_COLUMNS) + "\n1,1,Example Business,Construction,SEO_UPGRADE,80,A,CONTACTED,PIC Name,0123456789,,,,,,,,,,,Connected,SEO proposal,RM5k-RM10k,,,Follow up next week"
    body = f"""{notice}<div class='actions'><a class='button' href='/export/call-list.csv'>Download A/B call-list CSV</a></div>
      <p>Update the downloaded CSV using valid statuses: {esc(', '.join(SALES_STATUSES))}. Re-importing records an append-only activity. Email and WhatsApp outreach are not enabled.</p>
      <form id='import-form'><input id='csv-file' type='file' accept='.csv,text/csv' required>
      <button class='button' type='submit'>Import PIC updates</button></form><pre style='display:none'>{esc(sample)}</pre>
      <div class='grid2'><div><h2>Funnel</h2>{table(['Status','Organizations','Proposal value'],funnel)}</div>
      <div><h2>By PIC</h2>{table(['PIC','Leads','Qualified+','Won','Proposal value'],by_pic)}</div></div>
      <h2>Conversion by industry</h2>{table(['Industry','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100 leads'],conversion_by_industry)}
      <div class='grid2'><div><h2>Conversion by primary offer</h2>{table(['Offer','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],conversion_by_offer)}</div>
      <div><h2>Conversion by market</h2>{table(['Market','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],conversion_by_market)}</div></div>
      <h2>Conversion by source query term</h2>{table(['Term','Leads','Contact %','Qualification %','Proposal %','Close %','Won revenue / 100'],by_query_term)}
      <h2>Recent sales activity</h2>{table(['Org ID','Organization','From','To','Outcome','Notes','PIC','Time'],recent)}
      <script>document.getElementById('import-form').addEventListener('submit',async event=>{{event.preventDefault();const file=document.getElementById('csv-file').files[0];
      const response=await fetch('/import/sales-updates',{{method:'POST',headers:{{'Content-Type':'text/csv; charset=utf-8'}},body:await file.text()}});
      document.open();document.write(await response.text());document.close();}});</script>"""
    return page("Sales pipeline and PIC handoff", body, "pipeline")


def coverage_page() -> str:
    conn = connection()
    market = conn.execute(
        """SELECT CASE WHEN state IN ('Selangor','Federal Territory') THEN 'Klang Valley' ELSE state END,
                  COUNT(*),SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                  SUM(qualified_new),SUM(ab_leads_new),ROUND(AVG(expected_ab_yield),2)
           FROM search_jobs WHERE taxonomy_version=4 GROUP BY 1 ORDER BY COUNT(*) DESC"""
    ).fetchall()
    yield_rows = conn.execute(
        """SELECT sector,strategy_bucket,COUNT(*),SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                  SUM(qualified_new),SUM(ab_leads_new),ROUND(AVG(expected_ab_yield),2),
                  ROUND(100.0*SUM(duplicate_count)/NULLIF(SUM(processed_count),0),1)
           FROM search_jobs WHERE taxonomy_version=4 GROUP BY sector,strategy_bucket
           ORDER BY SUM(ab_leads_new) DESC,AVG(expected_ab_yield) DESC LIMIT 100"""
    ).fetchall()
    coverage = conn.execute(
        """SELECT c.state_name,COALESCE(ic.industry_label,c.sector),COUNT(*),
                  SUM(CASE WHEN li.sales_rank IN ('A','B') THEN 1 ELSE 0 END)
           FROM companies c LEFT JOIN company_industry_classification ic
             ON ic.company_id=c.id AND ic.taxonomy_version=4
           LEFT JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
           WHERE c.state_name IN ('Selangor','Federal Territory','Johor','Penang')
           GROUP BY c.state_name,2 ORDER BY c.state_name,COUNT(*) DESC"""
    ).fetchall()
    conn.close()
    body = f"""<p>New V4 search allocation: 55% Klang Valley, 25% Johor, 20% Penang. Legacy nationwide data remains available but is not prioritized.</p>
      <h2>Query allocation and results</h2>{table(['Market','Queries','Completed','Qualified','A/B leads','Expected A/B'],market)}
      <h2>A/B yield by industry and strategy</h2>{table(['Industry','Strategy','Queries','Completed','Qualified','A/B leads','Expected A/B','Duplicate %'],yield_rows)}
      <h2>Lead coverage in primary markets</h2>{table(['State','Industry','Locations','A/B leads'],coverage)}"""
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
            if parsed.path in ("/", "/index.html"):
                self.respond(dashboard_page(read_dashboard_data()), "text/html; charset=utf-8")
            elif parsed.path == "/searches":
                query = parse_qs(parsed.query).get("q", [""])[0]
                try:
                    page_number = int(parse_qs(parsed.query).get("page", ["1"])[0])
                except ValueError:
                    page_number = 1
                self.respond(searches_page(query, page_number), "text/html; charset=utf-8")
            elif parsed.path == "/leads":
                self.respond(ranked_leads_page(parse_qs(parsed.query)), "text/html; charset=utf-8")
            elif parsed.path == "/pipeline":
                self.respond(pipeline_page(), "text/html; charset=utf-8")
            elif parsed.path == "/coverage":
                self.respond(coverage_page(), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                self.respond(json.dumps(load_status()), "application/json; charset=utf-8")
            elif parsed.path == "/export/companies.csv":
                stream_company_csv(self)
            elif parsed.path == "/export/call-list.csv":
                stream_call_list_csv(self)
            elif parsed.path == "/export/companies.xlsx":
                export_path = DB_PATH.parent / "sherlockmaps-companies.xlsx"
                export_path.write_bytes(company_export_xlsx())
                self.download_file(export_path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sherlockmaps-companies.xlsx")
            elif parsed.path == "/export/report.html":
                report = report_page(read_dashboard_data()).encode("utf-8")
                self.download(report, "text/html; charset=utf-8", "sherlockmaps-report.html", inline=True)
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_error(500, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
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
    print(f"Dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
