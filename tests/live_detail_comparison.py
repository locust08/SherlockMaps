"""Paired whole-detail navigation benchmark; live DB is strictly read-only.

Uses the trusted repository's pre-optimization extractor at commit 684bc52.
Writes diagnostic results under ignored data/, never into the lead database.
"""
import argparse
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from batch_collect_malaysia_v2 import DB_PATH
from core.browser.browser_manager import BrowserManager
from core.extractors.maps_extractor import MapsExtractor
from core.models import CrawlerConfig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(
        ["git", "show", "684bc52:core/extractors/maps_extractor.py"], cwd=root, text=True, encoding="utf-8"
    )
    namespace = {"__file__": str(root / "core/extractors/maps_extractor.py"), "__name__": "baseline_extractor"}
    exec(compile(source, "baseline_extractor.py", "exec"), namespace)
    old_type = namespace["MapsExtractor"]
    with sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        # Alternate website-owning and phone-only locations, then freeze the sample.
        groups = [conn.execute(
            "SELECT id,source_url FROM companies WHERE source_url LIKE 'https://www.google.com/maps/place/%' "
            + ("AND website<>'' " if has_site else "AND website='' ")
            + "ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall() for has_site in (True, False)]
    samples = [item for pair in zip(*groups) for item in pair][:args.limit]
    results = []
    manager = BrowserManager(CrawlerConfig(headless=True, locale="en-MY"))
    manager.initialize()
    try:
        for index, (company_id, url) in enumerate(samples):
            record = {"company_id": company_id, "runs": {}}
            order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for variant in order:
                page = manager.recycle_context()
                extractor = old_type(page) if variant == "baseline" else MapsExtractor(page, batched_details=True)
                start = time.monotonic()
                company = extractor._extract_company_details(url, index, track_reviews=False)
                extractor._raise_if_blocked()
                record["runs"][variant] = {"seconds": time.monotonic() - start,
                                           "data": company.to_dict() if company else None}
            left, right = (record["runs"][key]["data"] for key in ("baseline", "candidate"))
            record["differences"] = ([key for key in left if left[key] != right.get(key)]
                                      if left and right else ["extraction_failed"])
            results.append(record)
            print(json.dumps({"index": index, "seconds": {k: round(v["seconds"], 3) for k, v in record["runs"].items()},
                              "differences": record["differences"]}), flush=True)
    finally:
        manager.close()
        output = root / "data" / "detail_comparison.json"
        output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Results: " + str(output), flush=True)


if __name__ == "__main__":
    main()
