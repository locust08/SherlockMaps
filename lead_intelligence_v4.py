"""Append-only LOCUS-T V4 lead intelligence, organization, and sales workflow."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

INTELLIGENCE_VERSION = 4
SALES_STATUSES = (
    "NEW", "REVIEWED", "CONTACTED", "QUALIFIED", "DISCOVERY",
    "PROPOSAL_SENT", "WON", "LOST", "DO_NOT_CONTACT",
)
PRIMARY_MARKETS = {"Selangor", "Federal Territory", "Johor", "Penang"}
PRIMARY_MARKET_ALLOCATION = {
    "Klang Valley": 0.55,
    "Johor": 0.25,
    "Penang": 0.20,
}
HIGH_VALUE_INDUSTRIES = {
    "Construction", "Home Improvement", "Interior Design", "Pest & Cleaning",
    "Landscaping", "Health, Fitness & Wellness", "Education", "Automotive",
    "Finance", "Property", "Industrial & Manufacturing", "Logistics", "B2B",
}
HIGH_INTENT_CATEGORY_MARKERS = (
    "contractor", "construction", "renovation", "interior", "cleaning", "facility",
    "clinic", "dental", "dentist", "aesthetic", "veterinary", "physiotherapy",
    "tuition", "preschool", "kindergarten", "training", "workshop", "repair",
    "account", "insurance", "property", "architect", "manufacturer", "industrial",
    "machinery", "logistics", "consultant", "printing", "hotel", "travel agency",
)
SHARED_PROFILE_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "youtube.com",
    "linktr.ee", "wa.me", "api.whatsapp.com", "sites.google.com", "business.site",
    "wixsite.com", "weebly.com", "wordpress.com", "blogspot.com", "carousell.com.my",
    "foodpanda.my", "grab.com", "shopee.com.my", "lazada.com.my",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def setup_v4_schema(conn: sqlite3.Connection) -> None:
    """Create V4 tables and columns without replacing source Maps data."""
    for column, definition in {
        "reviews_count": "INTEGER",
        "operational_status": "TEXT NOT NULL DEFAULT 'active'",
        "listing_checked_at": "TEXT",
        "branch_count": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        ensure_column(conn, "companies", column, definition)
    for column, definition in {
        "strategy_bucket": "TEXT NOT NULL DEFAULT 'legacy'",
        "expected_ab_yield": "REAL NOT NULL DEFAULT 0",
        "ab_leads_new": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        ensure_column(conn, "search_jobs", column, definition)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY,
            organization_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            website_domain TEXT,
            primary_phone TEXT,
            location_count INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_refreshed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_organization (
            company_id INTEGER PRIMARY KEY,
            organization_id INTEGER NOT NULL,
            match_method TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE TABLE IF NOT EXISTS website_intelligence (
            company_id INTEGER PRIMARY KEY,
            public_email TEXT,
            has_contact_form INTEGER,
            whatsapp_url TEXT,
            social_urls_json TEXT NOT NULL DEFAULT '[]',
            page_count INTEGER,
            has_sitemap INTEGER,
            has_robots INTEGER,
            has_local_pages INTEGER,
            has_cta INTEGER,
            has_ga4 INTEGER,
            has_gtm INTEGER,
            has_google_ads_tag INTEGER,
            has_conversion_tracking INTEGER,
            has_meta_pixel INTEGER,
            mobile_ready INTEGER,
            performance_score REAL,
            evidence_url TEXT,
            checked_at TEXT,
            confidence INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS lead_intelligence (
            company_id INTEGER NOT NULL,
            intelligence_version INTEGER NOT NULL,
            organization_id INTEGER,
            primary_offer TEXT NOT NULL,
            secondary_offers_json TEXT NOT NULL DEFAULT '[]',
            sales_readiness_score INTEGER NOT NULL,
            sales_rank TEXT NOT NULL,
            reachability_score INTEGER NOT NULL,
            service_need_score INTEGER NOT NULL,
            capacity_score INTEGER NOT NULL,
            demand_score INTEGER NOT NULL,
            freshness_score INTEGER NOT NULL,
            evidence_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(company_id,intelligence_version),
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE TABLE IF NOT EXISTS sales_leads (
            organization_id INTEGER PRIMARY KEY,
            representative_company_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'NEW' CHECK(status IN {SALES_STATUSES}),
            assigned_pic TEXT,
            next_action_at TEXT,
            last_contact_at TEXT,
            contact_count INTEGER NOT NULL DEFAULT 0,
            call_outcome TEXT,
            requirement TEXT,
            budget_range TEXT,
            proposal_value REAL,
            proposal_url TEXT,
            lost_reason TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE TABLE IF NOT EXISTS sales_activities (
            id INTEGER PRIMARY KEY,
            organization_id INTEGER NOT NULL,
            company_id INTEGER,
            activity_type TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            channel TEXT NOT NULL DEFAULT 'CALL',
            outcome TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contact_suppression (
            organization_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'ALL',
            requested_at TEXT NOT NULL,
            source TEXT NOT NULL,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_lead_intelligence_rank_offer
            ON lead_intelligence(intelligence_version,sales_rank,primary_offer,sales_readiness_score DESC);
        CREATE INDEX IF NOT EXISTS ix_lead_intelligence_score
            ON lead_intelligence(intelligence_version,sales_readiness_score DESC,company_id);
        CREATE INDEX IF NOT EXISTS ix_company_organization_org ON company_organization(organization_id);
        CREATE INDEX IF NOT EXISTS ix_sales_leads_status_pic ON sales_leads(status,assigned_pic);
        CREATE INDEX IF NOT EXISTS ix_sales_activities_org ON sales_activities(organization_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_companies_last_seen_v4 ON companies(last_seen_at DESC,id);
        CREATE INDEX IF NOT EXISTS ix_companies_state_v4 ON companies(state_name,id);
        CREATE INDEX IF NOT EXISTS ix_companies_lead_tier_v4 ON companies(lead_tier,id);
        CREATE INDEX IF NOT EXISTS ix_search_jobs_v4_strategy
            ON search_jobs(taxonomy_version,status,strategy_bucket,priority,expected_ab_yield DESC);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('v4_schema_ready',?)",
        (utc_now(),),
    )


def organization_key(company: sqlite3.Row | tuple) -> tuple[str, str, int]:
    domain = normalized(company["website_domain"] if isinstance(company, sqlite3.Row) else company[3])
    phone = normalized(company["normalized_phone"] if isinstance(company, sqlite3.Row) else company[2])
    name = normalized(company["business_name"] if isinstance(company, sqlite3.Row) else company[1])
    if domain and not any(domain == shared or domain.endswith("." + shared) for shared in SHARED_PROFILE_DOMAINS):
        return f"domain:{domain}", "website_domain", 95
    if phone:
        return f"phone:{phone}", "business_phone", 90
    base = re.sub(r"\b(branch|outlet|cawangan|hq|sdn\.?\s*bhd\.?|enterprise)\b.*$", "", name)
    base = re.sub(r"\s*(?:[-–|]|\()[^)]*$", "", base)
    base = re.sub(r"[^a-z0-9]+", " ", base).strip() or name
    return f"name:{base}", "normalized_brand", 70


def assign_organization(conn: sqlite3.Connection, company_id: int) -> int:
    conn.row_factory = sqlite3.Row
    company = conn.execute(
        """SELECT id,business_name,normalized_phone,website_domain,first_seen_at
           FROM companies WHERE id=?""",
        (company_id,),
    ).fetchone()
    if not company:
        raise ValueError(f"Unknown company {company_id}")
    key, method, confidence = organization_key(company)
    now = utc_now()
    conn.execute(
        """INSERT INTO organizations(organization_key,display_name,website_domain,primary_phone,
               location_count,first_seen_at,last_refreshed_at)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(organization_key) DO UPDATE SET
               display_name=CASE WHEN length(excluded.display_name)>length(display_name)
                                 THEN excluded.display_name ELSE display_name END,
               website_domain=COALESCE(NULLIF(website_domain,''),excluded.website_domain),
               primary_phone=COALESCE(NULLIF(primary_phone,''),excluded.primary_phone),
               last_refreshed_at=excluded.last_refreshed_at""",
        (key, company["business_name"], company["website_domain"], company["normalized_phone"],
         company["first_seen_at"] or now, now),
    )
    organization_id = int(conn.execute(
        "SELECT id FROM organizations WHERE organization_key=?", (key,)
    ).fetchone()[0])
    conn.execute(
        """INSERT INTO company_organization(company_id,organization_id,match_method,confidence,assigned_at)
           VALUES(?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET
           organization_id=excluded.organization_id,match_method=excluded.match_method,
           confidence=excluded.confidence,assigned_at=excluded.assigned_at""",
        (company_id, organization_id, method, confidence, now),
    )
    conn.execute(
        """UPDATE organizations SET location_count=(
               SELECT COUNT(*) FROM company_organization WHERE organization_id=organizations.id)
           WHERE id=?""",
        (organization_id,),
    )
    return organization_id


def _rating(value: Any) -> float:
    try:
        return float(str(value or "").replace(",", "."))
    except ValueError:
        return 0.0


def _recent(value: str | None, days: int = 30) -> bool:
    if not value:
        return False
    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return seen >= datetime.now(timezone.utc) - timedelta(days=days)
    except ValueError:
        return False


def score_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT c.*,COALESCE(ic.industry_label,c.sector) industry,
                  a.status audit_status,a.https_ok,a.homepage_ok,a.has_meta_description,
                  a.has_local_business_schema,
                  wi.public_email,wi.has_contact_form,wi.whatsapp_url,wi.social_urls_json,
                  wi.page_count,wi.has_sitemap,wi.has_robots,wi.has_local_pages,wi.has_cta,
                  wi.has_ga4,wi.has_gtm,wi.has_google_ads_tag,wi.has_conversion_tracking,
                  wi.has_meta_pixel,wi.mobile_ready,wi.performance_score,wi.checked_at web_checked_at,
                  co.organization_id,o.location_count
           FROM companies c
           LEFT JOIN company_industry_classification ic
             ON ic.company_id=c.id AND ic.taxonomy_version=4
           LEFT JOIN website_audits a ON a.company_id=c.id
           LEFT JOIN website_intelligence wi ON wi.company_id=c.id
           LEFT JOIN company_organization co ON co.company_id=c.id
           LEFT JOIN organizations o ON o.id=co.organization_id
           WHERE c.id=?""",
        (company_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown company {company_id}")
    organization_id = int(row["organization_id"] or assign_organization(conn, company_id))
    website = bool(normalized(row["website"]))
    phone = bool(normalized(row["phone"]))
    social = json.loads(row["social_urls_json"] or "[]")
    reachability = (10 if phone else 0)
    reachability += 5 if row["public_email"] or row["has_contact_form"] else 0
    reachability += 3 if row["whatsapp_url"] else 0
    reachability += 2 if social else 0

    industry = row["industry"] or "Others"
    category = normalized(row["maps_category"])
    high_intent = industry in HIGH_VALUE_INDUSTRIES or any(x in category for x in HIGH_INTENT_CATEGORY_MARKERS)
    demand = 10 if high_intent else 5
    if row["state_name"] in PRIMARY_MARKETS:
        demand += 5
    elif row["state_name"]:
        demand += 3

    capacity = 8 if int(row["location_count"] or 1) >= 2 else 0
    capacity += 6 if int(row["reviews_count"] or 0) >= 20 else 0
    capacity += 3 if _rating(row["rating"]) >= 4 else 0
    capacity += 5 if row["website_domain"] else 0
    entity_haystack = normalized(f"{row['business_name']} {row['maps_category']}")
    capacity += 3 if any(x in entity_haystack for x in ("sdn bhd", "enterprise", "company", "firm", "agency")) else 0

    seo_gaps: list[str] = []
    if website and row["audit_status"] == "completed":
        if not row["has_meta_description"]:
            seo_gaps.append("missing_meta_description")
        if not row["has_local_business_schema"]:
            seo_gaps.append("missing_local_business_schema")
    if row["web_checked_at"]:
        for key, label in (
            ("has_sitemap", "missing_sitemap"), ("has_robots", "missing_robots"),
            ("has_local_pages", "missing_local_pages"), ("mobile_ready", "not_mobile_ready"),
        ):
            if not row[key]:
                seo_gaps.append(label)
    seo_need = min(30, len(seo_gaps) * 6)
    ads_ready = bool(
        website and row["homepage_ok"] and row["https_ok"] and phone and high_intent
    )
    ads_need = 0
    if ads_ready:
        ads_need = 10
        ads_need += 5 if row["has_cta"] or phone else 0
        ads_need += 8 if not row["has_google_ads_tag"] else 0
        ads_need += 7 if not row["has_conversion_tracking"] else 0
    website_need = 30 if (not website and phone) else 0
    if website_need:
        primary_offer = "WEBSITE_BUILD"
        service_need = website_need
        secondary: list[str] = []
    elif len(seo_gaps) >= 2:
        primary_offer = "SEO_UPGRADE"
        service_need = seo_need
        secondary = ["GOOGLE_ADS_READY"] if ads_ready else []
    elif ads_ready:
        primary_offer = "GOOGLE_ADS_READY"
        service_need = ads_need
        secondary = ["SEO_UPGRADE"] if seo_gaps else []
    else:
        primary_offer = "ENRICHMENT_REQUIRED"
        service_need = max(seo_need, ads_need)
        secondary = []
    if website and (row["audit_status"] == "failed" or not row["https_ok"]):
        secondary.append("WEB_MAINTENANCE")
    if phone and row["operational_status"] == "active":
        secondary.append("GBP_OPTIMIZATION")
    secondary = sorted(set(x for x in secondary if x != primary_offer))

    freshness = 5 if _recent(row["last_seen_at"], 30) else 0
    freshness += 5 if row["operational_status"] == "active" and (phone or website) else 0
    score = min(100, reachability + service_need + capacity + demand + freshness)
    rank = "A" if score >= 75 else "B" if score >= 60 else "C" if score >= 45 else "D"
    evidence = {
        "industry": industry, "category": row["maps_category"], "seo_gaps": seo_gaps,
        "ads_ready": ads_ready, "website_present": website, "phone_present": phone,
        "reviews_count": int(row["reviews_count"] or 0), "rating": row["rating"],
        "location_count": int(row["location_count"] or 1),
        "high_intent": high_intent, "website_checked_at": row["web_checked_at"],
    }
    checked = utc_now()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    conn.execute(
        """INSERT INTO lead_intelligence(company_id,intelligence_version,organization_id,primary_offer,
               secondary_offers_json,sales_readiness_score,sales_rank,reachability_score,
               service_need_score,capacity_score,demand_score,freshness_score,evidence_json,
               checked_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company_id,intelligence_version) DO UPDATE SET
               organization_id=excluded.organization_id,primary_offer=excluded.primary_offer,
               secondary_offers_json=excluded.secondary_offers_json,
               sales_readiness_score=excluded.sales_readiness_score,sales_rank=excluded.sales_rank,
               reachability_score=excluded.reachability_score,service_need_score=excluded.service_need_score,
               capacity_score=excluded.capacity_score,demand_score=excluded.demand_score,
               freshness_score=excluded.freshness_score,evidence_json=excluded.evidence_json,
               checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
        (company_id, INTELLIGENCE_VERSION, organization_id, primary_offer, json.dumps(secondary),
         score, rank, reachability, service_need, capacity, demand, freshness,
         json.dumps(evidence, ensure_ascii=False), checked, expires),
    )
    representative = conn.execute(
        """SELECT li.company_id FROM lead_intelligence li
           WHERE li.organization_id=? AND li.intelligence_version=?
           ORDER BY li.sales_readiness_score DESC,li.company_id LIMIT 1""",
        (organization_id, INTELLIGENCE_VERSION),
    ).fetchone()
    representative_id = int(representative[0]) if representative else company_id
    conn.execute(
        """INSERT INTO sales_leads(organization_id,representative_company_id,status,updated_at)
           VALUES(?,?,'NEW',?) ON CONFLICT(organization_id) DO UPDATE SET
           representative_company_id=excluded.representative_company_id""",
        (organization_id, representative_id, checked),
    )
    return {"company_id": company_id, "organization_id": organization_id,
            "score": score, "rank": rank, "primary_offer": primary_offer}


def bulk_score_missing(conn: sqlite3.Connection) -> int:
    """Score the initial historical corpus in set-based SQL.

    Incremental listings still use ``score_company`` for full evidence. This path
    avoids hundreds of thousands of Python/SQLite round trips during migration.
    """
    before = int(conn.execute(
        "SELECT COUNT(*) FROM lead_intelligence WHERE intelligence_version=4"
    ).fetchone()[0])
    high_value = ",".join("?" for _ in HIGH_VALUE_INDUSTRIES)
    conn.execute(
        f"""INSERT OR IGNORE INTO lead_intelligence(
             company_id,intelligence_version,organization_id,primary_offer,secondary_offers_json,
             sales_readiness_score,sales_rank,reachability_score,service_need_score,capacity_score,
             demand_score,freshness_score,evidence_json,checked_at,expires_at)
           WITH base AS (
             SELECT c.id company_id,co.organization_id,COALESCE(ic.industry_label,c.sector) industry,
                    CASE WHEN COALESCE(c.website,'')<>'' THEN 1 ELSE 0 END website,
                    CASE WHEN COALESCE(c.phone,'')<>'' THEN 1 ELSE 0 END phone,
                    CASE WHEN wi.public_email IS NOT NULL OR wi.has_contact_form=1 THEN 1 ELSE 0 END email_form,
                    COALESCE(wi.whatsapp_url,'')<>'' whatsapp,
                    CASE WHEN COALESCE(wi.social_urls_json,'[]') NOT IN ('','[]') THEN 1 ELSE 0 END social,
                    CASE WHEN a.status='completed' AND COALESCE(a.has_meta_description,0)=0 THEN 1 ELSE 0 END
                      + CASE WHEN a.status='completed' AND COALESCE(a.has_local_business_schema,0)=0 THEN 1 ELSE 0 END
                      + CASE WHEN wi.checked_at IS NOT NULL AND COALESCE(wi.has_sitemap,0)=0 THEN 1 ELSE 0 END
                      + CASE WHEN wi.checked_at IS NOT NULL AND COALESCE(wi.has_robots,0)=0 THEN 1 ELSE 0 END
                      + CASE WHEN wi.checked_at IS NOT NULL AND COALESCE(wi.has_local_pages,0)=0 THEN 1 ELSE 0 END
                      + CASE WHEN wi.checked_at IS NOT NULL AND COALESCE(wi.mobile_ready,0)=0 THEN 1 ELSE 0 END seo_gaps,
                    CASE WHEN COALESCE(c.website,'')<>'' AND a.homepage_ok=1 AND a.https_ok=1
                               AND COALESCE(c.phone,'')<>'' THEN 1 ELSE 0 END ads_ready,
                    COALESCE(wi.has_google_ads_tag,0) ads_tag,
                    COALESCE(wi.has_conversion_tracking,0) conversion_tracking,
                    COALESCE(o.location_count,1) location_count,COALESCE(c.reviews_count,0) reviews_count,
                    CAST(REPLACE(COALESCE(c.rating,'0'),',','.') AS REAL) rating,
                    CASE WHEN c.state_name IN ('Selangor','Federal Territory','Johor','Penang') THEN 1 ELSE 0 END primary_market,
                    CASE WHEN julianday(c.last_seen_at)>=julianday('now','-30 days') THEN 1 ELSE 0 END recent,
                    CASE WHEN c.operational_status='active' THEN 1 ELSE 0 END active,
                    CASE WHEN COALESCE(c.website_domain,'')<>'' THEN 1 ELSE 0 END owned_domain,
                    CASE WHEN lower(COALESCE(c.business_name,'')||' '||COALESCE(c.maps_category,''))
                              LIKE '%sdn%bh%' OR lower(COALESCE(c.business_name,'')||' '||COALESCE(c.maps_category,''))
                              LIKE '%enterprise%' THEN 1 ELSE 0 END entity_signal,
                    a.status audit_status,a.https_ok
             FROM companies c
             JOIN company_organization co ON co.company_id=c.id
             LEFT JOIN organizations o ON o.id=co.organization_id
             LEFT JOIN company_industry_classification ic ON ic.company_id=c.id AND ic.taxonomy_version=4
             LEFT JOIN website_audits a ON a.company_id=c.id
             LEFT JOIN website_intelligence wi ON wi.company_id=c.id
             LEFT JOIN lead_intelligence old ON old.company_id=c.id AND old.intelligence_version=4
             WHERE old.company_id IS NULL
           ), components AS (
             SELECT *,
                    MIN(20,phone*10+email_form*5+whatsapp*3+social*2) reachability,
                    CASE WHEN website=0 AND phone=1 THEN 30
                         WHEN seo_gaps>=2 THEN MIN(30,seo_gaps*6)
                         WHEN ads_ready=1 THEN MIN(30,15+(1-ads_tag)*8+(1-conversion_tracking)*7)
                         ELSE MAX(MIN(30,seo_gaps*6),CASE WHEN ads_ready=1 THEN 15 ELSE 0 END) END service_need,
                    MIN(25,CASE WHEN location_count>=2 THEN 8 ELSE 0 END
                         +CASE WHEN reviews_count>=20 THEN 6 ELSE 0 END
                         +CASE WHEN rating>=4 THEN 3 ELSE 0 END+owned_domain*5+entity_signal*3) capacity,
                    (CASE WHEN industry IN ({high_value}) THEN 10 ELSE 5 END)
                         +(CASE WHEN primary_market=1 THEN 5 ELSE 3 END) demand,
                    recent*5+CASE WHEN active=1 AND (phone=1 OR website=1) THEN 5 ELSE 0 END freshness
             FROM base
           ), scored AS (
             SELECT *,MIN(100,reachability+service_need+capacity+demand+freshness) total,
                    CASE WHEN website=0 AND phone=1 THEN 'WEBSITE_BUILD'
                         WHEN seo_gaps>=2 THEN 'SEO_UPGRADE'
                         WHEN ads_ready=1 THEN 'GOOGLE_ADS_READY' ELSE 'ENRICHMENT_REQUIRED' END offer
             FROM components
           )
           SELECT company_id,4,organization_id,offer,
                  CASE WHEN offer='SEO_UPGRADE' AND ads_ready=1 THEN '["GOOGLE_ADS_READY"]'
                       WHEN website=1 AND (audit_status='failed' OR COALESCE(https_ok,0)=0)
                         THEN '["WEB_MAINTENANCE"]' ELSE '[]' END,
                  total,CASE WHEN total>=75 THEN 'A' WHEN total>=60 THEN 'B'
                             WHEN total>=45 THEN 'C' ELSE 'D' END,
                  reachability,service_need,capacity,demand,freshness,
                  json_object('industry',industry,'seo_gap_count',seo_gaps,'ads_ready',ads_ready,
                              'website_present',website,'phone_present',phone,'reviews_count',reviews_count,
                              'location_count',location_count,'classification_source','bulk_v4_backfill'),
                  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now','+30 days')
           FROM scored""",
        tuple(HIGH_VALUE_INDUSTRIES),
    )
    conn.execute(
        """INSERT INTO sales_leads(organization_id,representative_company_id,status,updated_at)
           SELECT organization_id,company_id,'NEW',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM (
             SELECT organization_id,company_id,
                    ROW_NUMBER() OVER(PARTITION BY organization_id
                      ORDER BY sales_readiness_score DESC,company_id) position
             FROM lead_intelligence WHERE intelligence_version=4)
           WHERE position=1
           ON CONFLICT(organization_id) DO UPDATE SET
             representative_company_id=excluded.representative_company_id"""
    )
    after = int(conn.execute(
        "SELECT COUNT(*) FROM lead_intelligence WHERE intelligence_version=4"
    ).fetchone()[0])
    return after - before


def backfill_v4(conn: sqlite3.Connection, limit: int | None = None) -> int:
    setup_v4_schema(conn)
    sql = """SELECT c.id FROM companies c LEFT JOIN company_organization co
             ON co.company_id=c.id WHERE co.company_id IS NULL ORDER BY c.id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    organization_ids = [int(row[0]) for row in conn.execute(sql).fetchall()]
    for index, company_id in enumerate(organization_ids, 1):
        assign_organization(conn, company_id)
        if index % 1000 == 0:
            conn.commit()
    conn.execute(
        """UPDATE organizations SET location_count=(
             SELECT COUNT(*) FROM company_organization co WHERE co.organization_id=organizations.id)"""
    )
    if limit:
        score_ids = [int(row[0]) for row in conn.execute(
            """SELECT c.id FROM companies c LEFT JOIN lead_intelligence li
               ON li.company_id=c.id AND li.intelligence_version=4
               WHERE li.company_id IS NULL ORDER BY c.id LIMIT ?""", (int(limit),)
        ).fetchall()]
        for company_id in score_ids:
            score_company(conn, company_id)
        scored = len(score_ids)
    else:
        scored = bulk_score_missing(conn)
    conn.execute("UPDATE companies SET branch_count=COALESCE((SELECT o.location_count FROM company_organization co JOIN organizations o ON o.id=co.organization_id WHERE co.company_id=companies.id),1)")
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('v4_backfill_at',?)", (utc_now(),))
    conn.commit()
    return scored


def update_sales_lead(conn: sqlite3.Connection, payload: dict[str, Any], actor: str = "PIC") -> None:
    organization_id = int(payload["organization_id"])
    requested_status = normalized(payload.get("status")).upper()
    if requested_status not in SALES_STATUSES:
        raise ValueError(f"Invalid status {requested_status!r}")
    current = conn.execute(
        "SELECT status,representative_company_id,contact_count FROM sales_leads WHERE organization_id=?",
        (organization_id,),
    ).fetchone()
    if not current:
        raise ValueError(f"Unknown organization {organization_id}")
    old_status, company_id, contact_count = current
    contacted = requested_status in {"CONTACTED", "QUALIFIED", "DISCOVERY", "PROPOSAL_SENT", "WON", "LOST"}
    now = utc_now()
    fields = {
        "status": requested_status,
        "assigned_pic": payload.get("assigned_pic"),
        "next_action_at": payload.get("next_action_at"),
        "last_contact_at": payload.get("last_contact_at") or (now if contacted else None),
        "contact_count": int(contact_count or 0) + (1 if contacted and old_status == "NEW" else 0),
        "call_outcome": payload.get("call_outcome"),
        "requirement": payload.get("requirement"),
        "budget_range": payload.get("budget_range"),
        "proposal_value": float(payload["proposal_value"]) if payload.get("proposal_value") else None,
        "proposal_url": payload.get("proposal_url"),
        "lost_reason": payload.get("lost_reason"),
        "notes": payload.get("notes"),
        "updated_at": now,
    }
    conn.execute(
        """UPDATE sales_leads SET status=:status,assigned_pic=COALESCE(:assigned_pic,assigned_pic),
           next_action_at=COALESCE(:next_action_at,next_action_at),last_contact_at=COALESCE(:last_contact_at,last_contact_at),
           contact_count=:contact_count,call_outcome=COALESCE(:call_outcome,call_outcome),
           requirement=COALESCE(:requirement,requirement),budget_range=COALESCE(:budget_range,budget_range),
           proposal_value=COALESCE(:proposal_value,proposal_value),proposal_url=COALESCE(:proposal_url,proposal_url),
           lost_reason=COALESCE(:lost_reason,lost_reason),notes=COALESCE(:notes,notes),updated_at=:updated_at
           WHERE organization_id=:organization_id""",
        {**fields, "organization_id": organization_id},
    )
    conn.execute(
        """INSERT INTO sales_activities(organization_id,company_id,activity_type,old_status,new_status,
           channel,outcome,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (organization_id, company_id, "STATUS_UPDATE", old_status, requested_status, "CALL",
         payload.get("call_outcome"), payload.get("notes"), actor, now),
    )
    if requested_status == "DO_NOT_CONTACT":
        conn.execute(
            """INSERT INTO contact_suppression(organization_id,reason,channel,requested_at,source,notes)
               VALUES(?,?,'ALL',?,'PIC',?) ON CONFLICT(organization_id) DO UPDATE SET
               reason=excluded.reason,requested_at=excluded.requested_at,notes=excluded.notes""",
            (organization_id, payload.get("lost_reason") or "Opt-out request", now, payload.get("notes")),
        )
    conn.commit()


def sector_ab_fraction(conn: sqlite3.Connection, sector: str) -> float:
    row = conn.execute(
        """SELECT AVG(CASE WHEN li.sales_rank IN ('A','B') THEN 1.0 ELSE 0.0 END)
           FROM companies c LEFT JOIN lead_intelligence li
             ON li.company_id=c.id AND li.intelligence_version=4
           WHERE c.sector=?""",
        (sector,),
    ).fetchone()
    return float(row[0] or 0.35)
