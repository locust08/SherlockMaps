"""Read-only, reproducible 500-record V4 rollout quality report."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

from batch_collect_malaysia_v2 import DB_PATH, QueryTask, is_relevant, normalize_phone, website_domain


def audit(sample_size: int = 500) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """WITH sampled AS (
             SELECT c.*,COALESCE(ic.industry_label,c.sector) industry,
                    ROW_NUMBER() OVER (
                      PARTITION BY c.state_name,COALESCE(ic.industry_label,c.sector)
                      ORDER BY abs((c.id * 1103515245 + 12345) % 2147483647)) sample_order
             FROM companies c LEFT JOIN company_industry_classification ic
               ON ic.company_id=c.id AND ic.taxonomy_version=4
             WHERE COALESCE(ic.industry_label,c.sector) NOT IN ('Government','Others')
           ) SELECT * FROM sampled ORDER BY sample_order,state_name,industry LIMIT ?""",
        (sample_size,),
    ).fetchall()
    relevance = 0
    contact_match = 0
    for row in rows:
        task = QueryTask("audit", row["industry"], row["locality"] or "", row["state_name"] or "", "audit")
        relevance += int(is_relevant(task, row["business_name"], row["maps_category"] or ""))
        phone_ok = not row["phone"] or normalize_phone(row["phone"]) == (row["normalized_phone"] or "")
        website_ok = not row["website"] or website_domain(row["website"]) == (row["website_domain"] or "")
        contact_match += int(phone_ok and website_ok)
    grouping = conn.execute(
        """SELECT COUNT(*),SUM(CASE WHEN conflict=0 THEN 1 ELSE 0 END) FROM (
             SELECT co.organization_id,
                    CASE WHEN COUNT(DISTINCT NULLIF(c.website_domain,''))>1
                           AND COUNT(DISTINCT NULLIF(c.normalized_phone,''))>1 THEN 1 ELSE 0 END conflict
             FROM company_organization co JOIN companies c ON c.id=co.company_id
             GROUP BY co.organization_id HAVING COUNT(*)>1)"""
    ).fetchone()
    call_list_rows = conn.execute(
        """SELECT COUNT(*),COUNT(DISTINCT sl.organization_id)
           FROM sales_leads sl JOIN lead_intelligence li
             ON li.company_id=sl.representative_company_id AND li.intelligence_version=4
           LEFT JOIN contact_suppression cs ON cs.organization_id=sl.organization_id
           WHERE li.sales_rank IN ('A','B') AND sl.status NOT IN ('DO_NOT_CONTACT','LOST')
             AND cs.organization_id IS NULL"""
    ).fetchone()
    ranks = dict(conn.execute(
        "SELECT sales_rank,COUNT(*) FROM lead_intelligence WHERE intelligence_version=4 GROUP BY sales_rank"
    ).fetchall())
    offers = dict(conn.execute(
        "SELECT primary_offer,COUNT(*) FROM lead_intelligence WHERE intelligence_version=4 GROUP BY primary_offer"
    ).fetchall())
    conn.close()
    sampled = len(rows)
    grouped_total = int(grouping[0] or 0)
    grouped_consistent = int(grouping[1] or 0)
    return {
        "sample_size": sampled,
        "category_relevance_rule_rate": round(relevance / sampled, 4) if sampled else 0,
        "phone_website_normalization_match_rate": round(contact_match / sampled, 4) if sampled else 0,
        "multi_location_group_consistency_proxy": round(grouped_consistent / grouped_total, 4) if grouped_total else 1.0,
        "call_list_rows": int(call_list_rows[0] or 0),
        "call_list_unique_organizations": int(call_list_rows[1] or 0),
        "call_list_has_duplicate_organization": call_list_rows[0] != call_list_rows[1],
        "rank_counts": Counter(ranks),
        "offer_counts": Counter(offers),
        "note": "Rule-based rollout audit; manually review the exported stratified sample before bulk outreach.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=500)
    print(json.dumps(audit(parser.parse_args().sample_size), indent=2))
