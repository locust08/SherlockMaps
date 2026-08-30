"""Separate LOCUS-T V4 website and sales-opportunity enrichment queue."""

from __future__ import annotations

import concurrent.futures
import json
import re
import sqlite3
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from batch_collect_malaysia_v2 import DB_PATH, open_db, utc_now
from lead_intelligence_v4 import score_company, setup_v4_schema

MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 10
WORKERS = 5


@dataclass(frozen=True)
class AuditResult:
    company_id: int
    evidence_url: str = ""
    https_ok: int = 0
    homepage_ok: int = 0
    has_title: int = 0
    has_meta_description: int = 0
    has_viewport: int = 0
    has_local_business_schema: int = 0
    public_email: str = ""
    has_contact_form: int = 0
    whatsapp_url: str = ""
    social_urls_json: str = "[]"
    page_count: int = 0
    has_sitemap: int = 0
    has_robots: int = 0
    has_local_pages: int = 0
    has_cta: int = 0
    has_ga4: int = 0
    has_gtm: int = 0
    has_google_ads_tag: int = 0
    has_conversion_tracking: int = 0
    has_meta_pixel: int = 0
    mobile_ready: int = 0
    confidence: int = 0
    error: str = ""


def fetch(url: str, max_bytes: int = MAX_BYTES) -> tuple[str, str, int]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SherlockMapsOpportunityAudit/4.0)"},
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
        body = response.read(max_bytes).decode(
            response.headers.get_content_charset() or "utf-8", errors="ignore"
        )
        return body, response.geturl(), int(response.status)


def first_public_email(body: str) -> str:
    candidates = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", body, re.I)
    for email in candidates:
        lowered = email.lower().strip(".,;:'\"")
        if not any(marker in lowered for marker in ("example.com", "sentry.io", "wixpress", "noreply", "no-reply")):
            return lowered
    return ""


def first_link(body: str, patterns: tuple[str, ...]) -> str:
    for href in re.findall(r"href=[\"']([^\"']+)", body, re.I):
        if any(pattern in href.lower() for pattern in patterns):
            return href[:500]
    return ""


def audit(company_id: int, website: str) -> AuditResult:
    try:
        body, final_url, status = fetch(website)
        lowered = body.lower()
        parsed = urlparse(final_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        robots_body = ""
        sitemap_body = ""
        try:
            robots_body, _, robots_status = fetch(urljoin(origin, "/robots.txt"), 150_000)
            has_robots = int(200 <= robots_status < 400 and len(robots_body.strip()) > 5)
        except Exception:
            has_robots = 0
        try:
            sitemap_body, _, sitemap_status = fetch(urljoin(origin, "/sitemap.xml"), 500_000)
            has_sitemap = int(200 <= sitemap_status < 400 and "<url" in sitemap_body.lower())
        except Exception:
            has_sitemap = int("sitemap:" in robots_body.lower())
        page_count = len(re.findall(r"<loc[\s>]", sitemap_body, re.I))
        whatsapp = first_link(body, ("wa.me/", "api.whatsapp.com", "whatsapp://"))
        socials = []
        for markers in (("facebook.com/",), ("instagram.com/",), ("linkedin.com/",), ("tiktok.com/",)):
            value = first_link(body, markers)
            if value:
                socials.append(value)
        has_form = int(bool(re.search(r"<form\b", body, re.I)))
        has_cta = int(bool(
            has_form or whatsapp or re.search(
                r"(contact us|get (?:a )?quote|request (?:a )?quote|book now|enquir|hubungi|sebut harga)",
                lowered,
            )
        ))
        has_ga4 = int(bool(re.search(r"G-[A-Z0-9]{6,}|gtag\s*\(", body, re.I)))
        has_gtm = int(bool(re.search(r"GTM-[A-Z0-9]+", body, re.I)))
        has_ads = int(bool(re.search(r"AW-[0-9]+|google_conversion", body, re.I)))
        has_conversion = int(bool(re.search(r"google_conversion|send_to\s*[:=]\s*[\"']AW-|conversion_id", body, re.I)))
        has_meta_pixel = int(bool(re.search(r"fbq\s*\(|connect\.facebook\.net/.*/fbevents", body, re.I)))
        local_markers = (
            "kuala lumpur", "selangor", "petaling jaya", "shah alam", "klang", "puchong",
            "johor", "penang", "george town", "butterworth", "bukit mertajam",
        )
        viewport = int(bool(re.search(r"<meta[^>]+name=[\"']viewport[\"']", body, re.I)))
        return AuditResult(
            company_id=company_id,
            evidence_url=final_url,
            https_ok=int(final_url.lower().startswith("https://")),
            homepage_ok=int(200 <= status < 400),
            has_title=int(bool(re.search(r"<title[^>]*>\s*[^<]{2,}", body, re.I))),
            has_meta_description=int(bool(re.search(r"<meta[^>]+name=[\"']description[\"']", body, re.I))),
            has_viewport=viewport,
            has_local_business_schema=int("localbusiness" in lowered or "local business" in lowered),
            public_email=first_public_email(body),
            has_contact_form=has_form,
            whatsapp_url=whatsapp,
            social_urls_json=json.dumps(socials),
            page_count=page_count,
            has_sitemap=has_sitemap,
            has_robots=has_robots,
            has_local_pages=int(sum(marker in lowered for marker in local_markers) >= 2 or page_count >= 10),
            has_cta=has_cta,
            has_ga4=has_ga4,
            has_gtm=has_gtm,
            has_google_ads_tag=has_ads,
            has_conversion_tracking=has_conversion,
            has_meta_pixel=has_meta_pixel,
            mobile_ready=viewport,
            confidence=90,
        )
    except Exception as exc:
        return AuditResult(company_id=company_id, evidence_url=website, error=f"{type(exc).__name__}: {exc}"[:500])


def save_result(conn: sqlite3.Connection, result: AuditResult) -> None:
    status = "completed" if result.homepage_ok else "failed"
    checked_at = utc_now()
    conn.execute(
        """INSERT INTO website_audits(company_id,status,https_ok,homepage_ok,has_title,
           has_meta_description,has_viewport,has_local_business_schema,checked_at,error)
           VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET
           status=excluded.status,https_ok=excluded.https_ok,homepage_ok=excluded.homepage_ok,
           has_title=excluded.has_title,has_meta_description=excluded.has_meta_description,
           has_viewport=excluded.has_viewport,has_local_business_schema=excluded.has_local_business_schema,
           checked_at=excluded.checked_at,error=excluded.error""",
        (result.company_id, status, result.https_ok, result.homepage_ok, result.has_title,
         result.has_meta_description, result.has_viewport, result.has_local_business_schema,
         checked_at, result.error),
    )
    conn.execute(
        """INSERT INTO website_intelligence(company_id,public_email,has_contact_form,whatsapp_url,
           social_urls_json,page_count,has_sitemap,has_robots,has_local_pages,has_cta,has_ga4,has_gtm,
           has_google_ads_tag,has_conversion_tracking,has_meta_pixel,mobile_ready,performance_score,
           evidence_url,checked_at,confidence,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company_id) DO UPDATE SET
           public_email=excluded.public_email,has_contact_form=excluded.has_contact_form,
           whatsapp_url=excluded.whatsapp_url,social_urls_json=excluded.social_urls_json,
           page_count=excluded.page_count,has_sitemap=excluded.has_sitemap,has_robots=excluded.has_robots,
           has_local_pages=excluded.has_local_pages,has_cta=excluded.has_cta,has_ga4=excluded.has_ga4,
           has_gtm=excluded.has_gtm,has_google_ads_tag=excluded.has_google_ads_tag,
           has_conversion_tracking=excluded.has_conversion_tracking,has_meta_pixel=excluded.has_meta_pixel,
           mobile_ready=excluded.mobile_ready,evidence_url=excluded.evidence_url,
           checked_at=excluded.checked_at,confidence=excluded.confidence,error=excluded.error""",
        (result.company_id, result.public_email, result.has_contact_form, result.whatsapp_url,
         result.social_urls_json, result.page_count, result.has_sitemap, result.has_robots,
         result.has_local_pages, result.has_cta, result.has_ga4, result.has_gtm,
         result.has_google_ads_tag, result.has_conversion_tracking, result.has_meta_pixel,
         result.mobile_ready, None, result.evidence_url, checked_at, result.confidence, result.error),
    )
    conn.execute("UPDATE companies SET website_audit_status=? WHERE id=?", (status, result.company_id))
    score_company(conn, result.company_id)
    conn.commit()


def run(limit: int = 500) -> int:
    conn = open_db(Path(DB_PATH))
    setup_v4_schema(conn)
    rows = conn.execute(
        """SELECT c.id,c.website FROM companies c
           LEFT JOIN website_intelligence wi ON wi.company_id=c.id
           WHERE c.website<>'' AND (wi.checked_at IS NULL OR julianday(wi.checked_at)<julianday('now','-30 days'))
           ORDER BY CASE WHEN wi.checked_at IS NULL THEN 0 ELSE 1 END,c.id LIMIT ?""",
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
