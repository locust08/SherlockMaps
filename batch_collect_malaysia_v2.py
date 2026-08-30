"""Adaptive, checkpointed LOCUS-T Malaysia lead collector (V4)."""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lead_intelligence_v4 import (
    HIGH_VALUE_INDUSTRIES,
    INTELLIGENCE_VERSION,
    PRIMARY_MARKETS,
    backfill_v4,
    score_company,
    sector_ab_fraction,
    setup_v4_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "malaysia_qualified_companies.sqlite"
STATUS_PATH = DATA_DIR / "malaysia_batch_status.json"
LOG_PATH = DATA_DIR / "malaysia_batch.log"
COLLECTOR_VERSION = 4
TAXONOMY_VERSION = 4

TARGET = 400_000
INITIAL_RESULTS = 100
ADAPTIVE_RESULTS = 200
HARD_RESULT_CAP = 500
SCROLL_TIMEOUT = 120
MAX_SCROLL_ATTEMPTS = 8
BASE_WORKERS = 4
DEFAULT_WORKERS = 5
MAXIMUM_WORKERS = 6
MAX_ATTEMPTS = 3
PILOT_QUERIES = 120
THROTTLE_COOLDOWN_SECONDS = 15 * 60
RAM_POLICY_MODE = "adaptive_5_canary_6_max"
RAM_RESERVE_GB = 1.0
RAM_LAUNCH_THRESHOLD_GB = 2.0
RAM_CRITICAL_GB = 0.65
MEMORY_PRESSURE_GRACE_SECONDS = 15
MEMORY_RECOVERY_COOLDOWN_SECONDS = 15
RAM_CANARY_QUERIES = 20
RAM_BASE_UPSCALE_STABLE_SECONDS = 10
RAM_FIFTH_UPSCALE_STABLE_SECONDS = 60
RAM_SIXTH_UPSCALE_STABLE_SECONDS = 5 * 60


@dataclass(frozen=True)
class QueryTask:
    prompt: str
    sector: str
    locality: str
    state: str
    term: str
    geo_level: str = "city"
    parent_prompt: str = ""
    priority: int = 100
    strategy_bucket: str = "commercial"
    expected_ab_yield: float = 0.0
    initial_results: int = INITIAL_RESULTS
    adaptive_results: int = ADAPTIVE_RESULTS
    hard_result_cap: int = HARD_RESULT_CAP
    scroll_timeout: int = SCROLL_TIMEOUT
    taxonomy_version: int = TAXONOMY_VERSION


@dataclass(frozen=True)
class CrawlOutcome:
    task: QueryTask
    links_discovered: int = 0
    processed_count: int = 0
    accepted_count: int = 0
    new_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    error: str = ""
    throttled: bool = False
    page_recycle_count: int = 0
    memory_cleanup_count: int = 0


LOCALITIES: list[tuple[str, str]] = [
    ("Kuala Lumpur", "Federal Territory"), ("Petaling Jaya", "Selangor"),
    ("Shah Alam", "Selangor"), ("Subang Jaya", "Selangor"),
    ("Puchong", "Selangor"), ("Klang", "Selangor"), ("Kajang", "Selangor"),
    ("Ampang Jaya", "Selangor"), ("Cyberjaya", "Selangor"),
    ("Rawang", "Selangor"), ("Semenyih", "Selangor"),
    ("Kuala Selangor", "Selangor"), ("Banting", "Selangor"),
    ("Putrajaya", "Federal Territory"), ("Johor Bahru", "Johor"),
    ("Iskandar Puteri", "Johor"), ("Pasir Gudang", "Johor"),
    ("Skudai", "Johor"), ("Kulai", "Johor"), ("Batu Pahat", "Johor"),
    ("Muar", "Johor"), ("Kluang", "Johor"), ("George Town", "Penang"),
    ("Bayan Lepas", "Penang"), ("Butterworth", "Penang"),
    ("Bukit Mertajam", "Penang"), ("Balik Pulau", "Penang"),
    ("Ipoh", "Perak"), ("Taiping", "Perak"), ("Manjung", "Perak"),
    ("Teluk Intan", "Perak"), ("Batu Gajah", "Perak"),
    ("Kuala Kangsar", "Perak"), ("Seremban", "Negeri Sembilan"),
    ("Nilai", "Negeri Sembilan"), ("Port Dickson", "Negeri Sembilan"),
    ("Kuala Pilah", "Negeri Sembilan"), ("Melaka City", "Melaka"),
    ("Alor Gajah", "Melaka"), ("Jasin", "Melaka"), ("Kuantan", "Pahang"),
    ("Temerloh", "Pahang"), ("Bentong", "Pahang"), ("Raub", "Pahang"),
    ("Cameron Highlands", "Pahang"), ("Kuching", "Sarawak"),
    ("Miri", "Sarawak"), ("Sibu", "Sarawak"), ("Bintulu", "Sarawak"),
    ("Kota Samarahan", "Sarawak"), ("Sri Aman", "Sarawak"),
    ("Kota Kinabalu", "Sabah"), ("Sandakan", "Sabah"), ("Tawau", "Sabah"),
    ("Lahad Datu", "Sabah"), ("Keningau", "Sabah"), ("Kota Belud", "Sabah"),
    ("Alor Setar", "Kedah"), ("Sungai Petani", "Kedah"), ("Kulim", "Kedah"),
    ("Langkawi", "Kedah"), ("Kota Bharu", "Kelantan"),
    ("Pasir Mas", "Kelantan"), ("Tanah Merah", "Kelantan"),
    ("Kuala Terengganu", "Terengganu"), ("Kemaman", "Terengganu"),
    ("Dungun", "Terengganu"), ("Besut", "Terengganu"),
    ("Kangar", "Perlis"), ("Arau", "Perlis"), ("Labuan", "Federal Territory"),
]

SECTOR_TERMS: dict[str, list[str]] = {
    "Education": ["preschool", "kindergarten", "childcare centre", "tuition centre", "learning centre", "enrichment centre", "language school", "music school", "vocational training centre", "computer training centre", "tadika", "taska", "pusat tuisyen", "pusat latihan"],
    "Health, Fitness & Wellness": ["dental clinic", "dentist", "medical clinic", "physiotherapy centre", "chiropractor", "veterinary clinic", "optometrist", "aesthetic clinic", "hearing aid centre", "gym", "fitness centre", "yoga studio", "pilates studio", "wellness centre", "klinik gigi", "klinik perubatan"],
    "Finance": ["accounting firm", "audit firm", "tax consultant", "bookkeeping service", "financial planner", "insurance agency", "mortgage broker", "akauntan", "perunding cukai"],
    "Property": ["real estate agency", "property agent", "estate agent", "property management", "property valuer", "commercial property agency", "ejen hartanah", "penilai hartanah"],
    "F&B": ["restaurant", "cafe", "bakery", "catering service", "food court operator", "dessert shop", "coffee shop", "restoran", "katering"],
    "Home Improvement": ["renovation contractor", "roofing contractor", "waterproofing contractor", "plumbing service", "electrical contractor", "air conditioner service", "painting contractor", "kitchen cabinet contractor", "locksmith", "pool contractor", "flooring contractor", "kontraktor renovasi", "servis penghawa dingin"],
    "Industrial & Manufacturing": ["manufacturer", "factory", "industrial supplier", "machinery supplier", "machine shop", "CNC machining", "automation company", "metal fabricator", "packaging manufacturer", "chemical supplier", "engineering company", "kilang", "pembekal industri"],
    "Construction": ["construction company", "building contractor", "civil engineering contractor", "architect", "quantity surveyor", "scaffolding contractor", "building materials supplier", "kontraktor binaan", "syarikat pembinaan"],
    "Technology": ["software company", "IT services", "managed IT services", "cybersecurity company", "cloud consultant", "app developer", "computer support", "web development company", "syarikat teknologi"],
    "Automotive": ["car workshop", "car repair", "tyre shop", "car detailing", "car tinting", "auto body shop", "towing service", "car rental", "motorcycle workshop", "bengkel kereta", "kedai tayar", "servis kereta"],
    "HR, Events & Entertainment": ["recruitment agency", "manpower agency", "HR consultant", "event planner", "wedding planner", "event management company", "AV rental", "entertainment agency", "photographer", "videographer", "jurugambar perkahwinan", "agensi pekerjaan"],
    "Fashion & Beauty": ["beauty salon", "hair salon", "barber shop", "nail salon", "bridal boutique", "cosmetics store", "fashion boutique", "salon kecantikan", "kedai pengantin"],
    "Maid": ["maid agency", "domestic helper agency", "foreign worker agency", "agensi pembantu rumah", "agensi pekerja asing"],
    "Office & Office Equipment": ["serviced office", "coworking space", "office furniture supplier", "copier supplier", "printer supplier", "stationery supplier", "office equipment supplier", "perabot pejabat", "ruang kerja bersama"],
    "Printing": ["printing service", "digital printing", "label printing", "packaging printing", "signboard maker", "T-shirt printing", "percetakan", "papan tanda"],
    "Retail": ["gift shop", "florist", "pet shop", "sports shop", "homeware store", "wholesale retailer", "specialty store", "kedai hadiah", "kedai bunga"],
    "Landscaping": ["landscape contractor", "landscaping service", "garden maintenance", "tree service", "plant nursery", "kontraktor landskap", "penyelenggaraan taman"],
    "Logistics": ["logistics company", "freight forwarder", "courier service", "warehouse service", "cold chain logistics", "moving company", "syarikat logistik", "penghantaran barang"],
    "Baby": ["baby store", "maternity store", "baby products", "confinement centre", "mother and baby shop", "kedai bayi", "pusat berpantang"],
    "Energy Solutions": ["solar installer", "renewable energy company", "EV charger supplier", "energy consultant", "electrical energy solutions", "pemasang solar", "tenaga boleh diperbaharui"],
    "Travel": ["travel agency", "tour operator", "visa agency", "bus charter", "tourism service", "agensi pelancongan", "pakej pelancongan"],
    "B2B": ["business consultant", "corporate service provider", "company secretary", "wholesaler", "distributor", "procurement service", "management consultant", "perunding perniagaan", "setiausaha syarikat"],
    "Consumer Electronics": ["phone shop", "phone repair", "computer store", "computer repair", "CCTV supplier", "appliance store", "audio visual retailer", "kedai telefon", "kedai komputer"],
    "Hospitality": ["hotel", "boutique hotel", "resort", "homestay", "guest house", "serviced apartment", "event venue", "wedding venue", "hotel bajet"],
    "Pest & Cleaning": ["pest control service", "cleaning service", "commercial cleaning", "carpet cleaning", "disinfection service", "facility management", "servis pembersihan", "kawalan serangga"],
    "Interior Design": ["interior designer", "office interior designer", "kitchen designer", "retail interior designer", "interior design firm", "pereka dalaman", "reka bentuk dalaman"],
}

# V4 adds higher-value, sales-ready searches while retaining V3 provenance.
V4_EXTRA_TERMS: dict[str, list[str]] = {
    "Education": ["private school", "international school", "corporate training provider", "培训中心", "补习中心"],
    "Health, Fitness & Wellness": ["specialist clinic", "dermatology clinic", "fertility clinic", "sports injury clinic", "牙科诊所", "美容诊所"],
    "Finance": ["chartered accountant", "corporate insurance broker", "business tax advisor", "会计师事务所"],
    "Property": ["commercial property consultant", "property maintenance company", "industrial property agent", "房地产中介"],
    "Home Improvement": ["commercial renovation contractor", "office renovation contractor", "kitchen cabinet maker", "solar water heater installer", "装修承包商"],
    "Industrial & Manufacturing": ["contract manufacturer", "precision engineering company", "plastic injection moulding", "food manufacturer", "OEM manufacturer", "工业供应商"],
    "Construction": ["design and build contractor", "M&E contractor", "commercial construction contractor", "CIDB contractor", "建筑承包商"],
    "Automotive": ["continental car workshop", "commercial vehicle workshop", "fleet maintenance service", "汽车维修"],
    "B2B": ["SME business consultant", "corporate training consultant", "business process outsourcing", "商业顾问"],
    "Logistics": ["haulage company", "last mile delivery company", "ecommerce fulfilment", "物流公司"],
    "Pest & Cleaning": ["office cleaning service", "industrial cleaning service", "building maintenance service", "commercial pest control", "清洁服务"],
    "Interior Design": ["commercial interior design", "office fit out contractor", "retail fit out contractor", "室内设计"],
    "HR, Events & Entertainment": ["corporate event company", "exhibition contractor", "conference organiser"],
    "Printing": ["commercial printer", "packaging supplier", "large format printing"],
    "Hospitality": ["business hotel", "corporate event venue"],
}

PRIMARY_MARKET_LOCALITIES: list[tuple[str, str]] = [
    (locality, state) for locality, state in LOCALITIES
    if state in PRIMARY_MARKETS and locality != "Labuan"
] + [
    ("Sungai Buloh", "Selangor"), ("Damansara", "Selangor"),
    ("Kota Damansara", "Selangor"), ("Seri Kembangan", "Selangor"),
    ("Bandar Baru Bangi", "Selangor"), ("Balakong", "Selangor"),
    ("Senai", "Johor"), ("Masai", "Johor"), ("Pontian", "Johor"),
    ("Segamat", "Johor"), ("Tangkak", "Johor"), ("Kota Tinggi", "Johor"),
    ("Perai", "Penang"), ("Batu Kawan", "Penang"),
    ("Kepala Batas", "Penang"), ("Nibong Tebal", "Penang"),
]

CLASSIFICATION_ONLY_INDUSTRIES = ("Government", "Others")

# Lower values run first. Previously covered Education/Home categories remain
# append-only but yield to newly introduced commercial industries.
SECTOR_PRIORITY: dict[str, int] = {
    label: (40 if label == "Education" else 35 if label in {
        "Home Improvement", "Interior Design", "Pest & Cleaning", "Landscaping"
    } else 10)
    for label in SECTOR_TERMS
}

SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Education": ("school", "academy", "training", "tuition", "learning", "childcare", "preschool", "kindergarten", "tadika", "taska", "college", "music"),
    "Health, Fitness & Wellness": ("clinic", "dental", "medical", "health", "physio", "chiro", "veter", "optom", "aesthetic", "gym", "fitness", "yoga", "pilates", "wellness", "klinik"),
    "Finance": ("account", "audit", "tax", "bookkeep", "financial", "insurance", "mortgage", "akaunt", "cukai"),
    "Property": ("property", "real estate", "estate agent", "valuer", "hartanah", "property management"),
    "F&B": ("restaurant", "cafe", "bakery", "catering", "food", "coffee", "dessert", "restoran", "katering"),
    "Home Improvement": ("renov", "roof", "waterproof", "plumb", "electric", "air condition", "paint", "cabinet", "locksmith", "flooring", "contractor"),
    "Industrial & Manufacturing": ("manufactur", "factory", "industrial", "machinery", "machine", "cnc", "automation", "fabricat", "packaging", "chemical", "kilang"),
    "Construction": ("construction", "building", "civil", "architect", "surveyor", "scaffold", "binaan", "pembinaan"),
    "Technology": ("software", "information technology", "it service", "cyber", "cloud", "app develop", "computer support", "web develop", "technology"),
    "Automotive": ("car", "auto", "vehicle", "tyre", "tire", "workshop", "towing", "motorcycle", "bengkel", "detailing", "tint"),
    "HR, Events & Entertainment": ("recruit", "manpower", "human resource", "event", "wedding", "entertain", "photo", "video", "agensi pekerjaan"),
    "Fashion & Beauty": ("beauty", "salon", "barber", "nail", "bridal", "cosmetic", "fashion", "kecantikan", "pengantin"),
    "Maid": ("maid", "domestic helper", "foreign worker", "pembantu rumah", "pekerja asing"),
    "Office & Office Equipment": ("serviced office", "cowork", "office furniture", "copier", "printer", "stationery", "office equipment", "perabot pejabat"),
    "Printing": ("print", "label", "signboard", "papan tanda", "percetakan"),
    "Retail": ("shop", "store", "retail", "florist", "wholesale", "kedai"),
    "Landscaping": ("landscap", "garden", "tree service", "nursery", "taman"),
    "Logistics": ("logistic", "freight", "courier", "warehouse", "moving", "penghantaran"),
    "Baby": ("baby", "maternity", "confinement", "berpantang"),
    "Energy Solutions": ("solar", "renewable", "ev charger", "energy", "tenaga"),
    "Travel": ("travel", "tour", "visa", "tourism", "pelancongan"),
    "B2B": ("business consultant", "corporate service", "company secretar", "wholesale", "distributor", "procurement", "management consultant", "perniagaan"),
    "Consumer Electronics": ("phone", "computer", "cctv", "appliance", "electronic", "audio visual"),
    "Hospitality": ("hotel", "resort", "homestay", "guest house", "serviced apartment", "venue"),
    "Pest & Cleaning": ("pest", "clean", "carpet", "disinfection", "facility", "serangga", "pembersihan"),
    "Interior Design": ("interior", "pereka dalaman", "reka bentuk dalaman"),
}

DENSE_CHILDREN: dict[str, list[str]] = {
    "Kuala Lumpur": ["KLCC", "Bukit Bintang", "Bangsar", "Mont Kiara", "Sri Hartamas", "Cheras", "Kepong", "Setapak", "Wangsa Maju", "Sentul", "Brickfields", "Seputeh", "Old Klang Road", "Kuchai Lama", "Sri Petaling", "Bukit Jalil", "OUG", "Desa ParkCity"],
    "Petaling Jaya": ["SS2", "Damansara Utama", "Bandar Utama", "Kota Damansara", "Kelana Jaya", "Ara Damansara", "Taman Paramount"],
    "Shah Alam": ["Seksyen 7", "Seksyen 13", "Seksyen 15", "Glenmarie", "Kota Kemuning", "Setia Alam"],
    "Subang Jaya": ["SS15", "USJ", "Bandar Sunway", "Putra Heights"],
    "Puchong": ["Bandar Puteri Puchong", "Puchong Jaya", "Bandar Kinrara", "IOI Puchong"],
    "Klang": ["Bandar Bukit Tinggi", "Meru", "Kapar", "Port Klang"],
    "Kajang": ["Bandar Baru Bangi", "Bangi", "Balakong", "Sungai Chua"],
    "Johor Bahru": ["Tebrau", "Mount Austin", "Taman Molek", "Permas Jaya", "Bukit Indah", "Danga Bay"],
    "Iskandar Puteri": ["Nusajaya", "Gelang Patah", "Medini"],
    "George Town": ["Tanjung Tokong", "Tanjung Bungah", "Jelutong", "Gelugor", "Air Itam", "Pulau Tikus"],
    "Bayan Lepas": ["Bayan Baru", "Sungai Ara", "Relau"],
    "Butterworth": ["Raja Uda", "Bagan Ajam", "Perai"],
    "Ipoh": ["Ipoh Garden", "Bercham", "Menglembu", "Meru Raya", "Station 18"],
    "Seremban": ["Seremban 2", "Senawang", "Rasah", "Oakland"],
    "Melaka City": ["Ayer Keroh", "Batu Berendam", "Bukit Baru", "Kota Laksamana"],
    "Kuantan": ["Indera Mahkota", "Beserah", "Semambu", "Bandar Kuantan"],
    "Kuching": ["Tabuan Jaya", "Pending", "Satok", "Batu Kawa", "Petra Jaya"],
    "Kota Kinabalu": ["Likas", "Lintas", "Inanam", "Penampang", "Kepayan"],
    "Alor Setar": ["Mergong", "Anak Bukit", "Simpang Kuala"],
    "Kota Bharu": ["Kubang Kerian", "Wakaf Che Yeh", "Pengkalan Chepa"],
    "Kuala Terengganu": ["Gong Badak", "Batu Buruk", "Chendering"],
    "Sungai Buloh": ["Bukit Rahman Putra", "Kampung Baru Sungai Buloh", "Sierramas"],
    "Seri Kembangan": ["Serdang", "Taman Equine", "Putra Permai"],
    "Bandar Baru Bangi": ["Bangi Gateway", "Seksyen 7 Bangi", "Seksyen 9 Bangi"],
    "Senai": ["Senai Industrial Park", "Taman Perindustrian Senai", "Kempas"],
    "Masai": ["Bandar Seri Alam", "Taman Rinting", "Plentong"],
    "Batu Kawan": ["Batu Kawan Industrial Park", "Bandar Cassia"],
    "Perai": ["Perai Industrial Estate", "Prai", "Seberang Jaya"],
}

POSTCODES_BY_STATE: dict[str, list[str]] = {
    "Federal Territory": ["50000", "50450", "51200", "52100", "56000", "58200", "60000", "57000"],
    "Selangor": ["40100", "40460", "46000", "47300", "47500", "47600", "47810", "43000", "43200", "41000"],
    "Johor": ["80000", "80300", "81100", "81200", "81300", "81700", "79100"],
    "Penang": ["10000", "10350", "11600", "11900", "13600", "14000"],
    "Perak": ["30000", "30250", "31400", "31650", "32000", "34000"],
    "Negeri Sembilan": ["70000", "70200", "70450", "71800", "71900"],
    "Melaka": ["75000", "75200", "75450", "78000"],
    "Pahang": ["25000", "25200", "25300", "28000", "28700"],
    "Kedah": ["05000", "05150", "08000", "09000", "07000"],
    "Kelantan": ["15000", "15150", "16150", "17000"],
    "Terengganu": ["20000", "21000", "24000", "23000"],
    "Sabah": ["88000", "88300", "88450", "90000", "91000"],
    "Sarawak": ["93000", "93350", "96000", "97000", "98000"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def usable(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"n/a", "none", "null"} else text


def integer_count(value: Any) -> int:
    match = re.search(r"[\d,.]+", str(value or ""))
    if not match:
        return 0
    return int(re.sub(r"\D", "", match.group(0)) or 0)


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", usable(value))
    if digits.startswith("60"):
        return "+" + digits
    if digits.startswith("0") and len(digits) >= 9:
        return "+60" + digits[1:]
    return digits


def website_domain(website: str) -> str:
    try:
        return urlparse(website).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def parent_brand_key(name: str) -> str:
    text = normalized(name)
    text = re.sub(r"\b(branch|outlet|cawangan|hq|sdn\.?\s*bhd\.?|enterprise)\b.*$", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            prompt TEXT PRIMARY KEY, sector TEXT NOT NULL, locality TEXT NOT NULL,
            state TEXT NOT NULL, term TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0,
            qualified_new INTEGER NOT NULL DEFAULT 0, started_at TEXT,
            completed_at TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, sector TEXT NOT NULL,
            business_name TEXT NOT NULL, maps_category TEXT, address TEXT NOT NULL,
            city_state TEXT NOT NULL, phone TEXT, website TEXT NOT NULL,
            website_domain TEXT NOT NULL, rating TEXT, opening_hours TEXT,
            plus_code TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provenance (
            company_id INTEGER NOT NULL, prompt TEXT NOT NULL, seen_at TEXT NOT NULL,
            PRIMARY KEY (company_id, prompt)
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            name TEXT PRIMARY KEY, qualified_count INTEGER NOT NULL, reached_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_history (
            prompt TEXT NOT NULL, archived_at TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY (prompt, archived_at)
        );
        CREATE TABLE IF NOT EXISTS raw_observations (
            observation_key TEXT PRIMARY KEY, prompt TEXT NOT NULL, source_url TEXT,
            place_id TEXT, payload_json TEXT NOT NULL, accepted INTEGER NOT NULL,
            rejection_reason TEXT, company_id INTEGER, observed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS worker_events (
            id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, details TEXT,
            worker_count INTEGER, available_ram_gb REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS website_audits (
            company_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            https_ok INTEGER, homepage_ok INTEGER, has_title INTEGER,
            has_meta_description INTEGER, has_viewport INTEGER,
            has_local_business_schema INTEGER, checked_at TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS search_jobs (
            taxonomy_version INTEGER NOT NULL, prompt TEXT NOT NULL,
            sector TEXT NOT NULL, locality TEXT NOT NULL, state TEXT NOT NULL,
            term TEXT NOT NULL, geo_level TEXT NOT NULL DEFAULT 'city',
            parent_prompt TEXT, priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0, links_discovered INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0, qualified_new INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0,
            worker_id TEXT, throttle_detected INTEGER NOT NULL DEFAULT 0,
            started_at TEXT, completed_at TEXT, error TEXT,
            PRIMARY KEY (taxonomy_version, prompt)
        );
        CREATE TABLE IF NOT EXISTS company_industry_classification (
            company_id INTEGER NOT NULL, taxonomy_version INTEGER NOT NULL,
            industry_slug TEXT NOT NULL, industry_label TEXT NOT NULL,
            classification_source TEXT NOT NULL, confidence INTEGER NOT NULL,
            classified_at TEXT NOT NULL,
            PRIMARY KEY (company_id, taxonomy_version),
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        """
    )
    for column, definition in {
        "collector_version": "INTEGER NOT NULL DEFAULT 1", "geo_level": "TEXT NOT NULL DEFAULT 'city'",
        "parent_prompt": "TEXT", "links_discovered": "INTEGER NOT NULL DEFAULT 0",
        "processed_count": "INTEGER NOT NULL DEFAULT 0", "rejected_count": "INTEGER NOT NULL DEFAULT 0",
        "duplicate_count": "INTEGER NOT NULL DEFAULT 0", "worker_id": "TEXT",
        "throttle_detected": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        ensure_column(conn, "jobs", column, definition)
    for column, definition in {
        "maps_place_id": "TEXT", "source_url": "TEXT", "lead_tier": "TEXT",
        "parent_brand_key": "TEXT", "qualification_score": "INTEGER NOT NULL DEFAULT 0",
        "country": "TEXT NOT NULL DEFAULT 'Malaysia'", "state_name": "TEXT",
        "locality": "TEXT", "geo_level": "TEXT NOT NULL DEFAULT 'city'", "postcode": "TEXT",
        "normalized_name": "TEXT", "normalized_address": "TEXT", "normalized_phone": "TEXT",
        "website_audit_status": "TEXT NOT NULL DEFAULT 'pending'",
    }.items():
        ensure_column(conn, "companies", column, definition)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_place_id ON companies(maps_place_id) WHERE maps_place_id IS NOT NULL AND maps_place_id <> ''")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_companies_phone ON companies(normalized_phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_companies_name_address ON companies(normalized_name, normalized_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_search_jobs_status_priority ON search_jobs(taxonomy_version,status,priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_classification_industry ON company_industry_classification(taxonomy_version,industry_label)")
    setup_v4_schema(conn)
    migrate_legacy_data(conn)
    migrate_v3_classifications(conn)
    conn.commit()
    return conn


def migrate_legacy_data(conn: sqlite3.Connection) -> None:
    migrated = conn.execute("SELECT value FROM metadata WHERE key='v2_migrated'").fetchone()
    if migrated:
        return
    archived_at = utc_now()
    columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    for row in conn.execute("SELECT * FROM jobs").fetchall():
        payload = json.dumps(dict(zip(columns, row)), default=str)
        conn.execute("INSERT OR IGNORE INTO job_history VALUES (?, ?, ?)", (row[0], archived_at, payload))
    rows = conn.execute("SELECT id,business_name,address,phone,website,city_state FROM companies").fetchall()
    for company_id, name, address, phone, website, city_state in rows:
        state = city_state.split(",", 1)[-1].strip() if city_state else ""
        locality = city_state.split(",", 1)[0].strip() if city_state else ""
        conn.execute(
            """UPDATE companies SET normalized_name=?, normalized_address=?, normalized_phone=?,
               parent_brand_key=?, lead_tier='SEO_UPGRADE', qualification_score=80,
               state_name=?, locality=?, website_audit_status='pending' WHERE id=?""",
            (normalized(name), normalized(address), normalize_phone(phone), parent_brand_key(name), state, locality, company_id),
        )
        if usable(website):
            conn.execute("INSERT OR IGNORE INTO website_audits(company_id,status) VALUES(?,'pending')", (company_id,))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('v2_migrated', ?)", (archived_at,))


def industry_slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def classify_industry(name: str, category: str, legacy_sector: str = "") -> tuple[str, int, str]:
    haystack = normalized(f"{name} {category}")
    if any(marker in haystack for marker in ("government", "ministry", "municipal", "jabatan", "kementerian", "majlis perbandaran", "agensi kerajaan")):
        return "Government", 85, "name_category"
    for label, keywords in SECTOR_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return label, 75, "name_category"
    legacy = normalized(legacy_sector)
    fallbacks = (
        ("education", "Education"), ("childcare", "Education"), ("training", "Education"),
        ("health", "Health, Fitness & Wellness"), ("dental", "Health, Fitness & Wellness"),
        ("automotive", "Automotive"), ("beauty", "Fashion & Beauty"),
        ("hospitality", "Hospitality"), ("food", "F&B"),
        ("property", "Property"), ("construction", "Construction"),
        ("logistics", "Logistics"), ("interior", "Interior Design"),
        ("cleaning", "Pest & Cleaning"), ("facility", "Pest & Cleaning"),
        ("home", "Home Improvement"), ("professional", "B2B"),
    )
    for marker, label in fallbacks:
        if marker in legacy:
            return label, 55, "legacy_sector"
    return "Others", 25, "fallback"


def set_company_classification(
    conn: sqlite3.Connection,
    company_id: int,
    label: str,
    confidence: int,
    source: str,
) -> None:
    conn.execute(
        """INSERT INTO company_industry_classification(
               company_id,taxonomy_version,industry_slug,industry_label,
               classification_source,confidence,classified_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(company_id,taxonomy_version) DO UPDATE SET
               industry_slug=CASE WHEN excluded.confidence>confidence THEN excluded.industry_slug ELSE industry_slug END,
               industry_label=CASE WHEN excluded.confidence>confidence THEN excluded.industry_label ELSE industry_label END,
               classification_source=CASE WHEN excluded.confidence>confidence THEN excluded.classification_source ELSE classification_source END,
               confidence=MAX(confidence,excluded.confidence),
               classified_at=CASE WHEN excluded.confidence>confidence THEN excluded.classified_at ELSE classified_at END""",
        (company_id, TAXONOMY_VERSION, industry_slug(label), label, source, confidence, utc_now()),
    )


def migrate_v3_classifications(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT c.id,c.business_name,c.maps_category,c.sector
           FROM companies c LEFT JOIN company_industry_classification ic
             ON ic.company_id=c.id AND ic.taxonomy_version=?
           WHERE ic.company_id IS NULL""",
        (TAXONOMY_VERSION,),
    ).fetchall()
    for company_id, name, category, legacy_sector in rows:
        label, confidence, source = classify_industry(name or "", category or "", legacy_sector or "")
        set_company_classification(conn, int(company_id), label, confidence, source)
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('v4_classified',?)", (utc_now(),))


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def market_name(state: str) -> str:
    if state in {"Selangor", "Federal Territory"}:
        return "Klang Valley"
    return state


def strategy_for(sector: str, term: str, geo_level: str = "city") -> tuple[str, int]:
    if geo_level in {"district", "postcode", "industrial_park", "neighbourhood"}:
        return "long_tail", 20
    if term in V4_EXTRA_TERMS.get(sector, []):
        return "experimental", 15 if sector in HIGH_VALUE_INDUSTRIES else 30
    if sector in HIGH_VALUE_INDUSTRIES:
        return "high_value", 10
    if sector in {"F&B", "Retail"}:
        return "volume_gated", 50
    return "commercial", 30


def expected_ab_yield(conn: sqlite3.Connection | None, sector: str, term: str) -> float:
    if conn is None:
        return 4.0
    row = conn.execute(
        """SELECT AVG(qualified_new) FROM search_jobs
           WHERE status='completed' AND sector=? AND term=?""",
        (sector, term),
    ).fetchone()
    if not row or row[0] is None:
        row = conn.execute(
            "SELECT AVG(qualified_new) FROM search_jobs WHERE status='completed' AND sector=?",
            (sector,),
        ).fetchone()
    raw_yield = float(row[0] or 8.0)
    return round(raw_yield * sector_ab_fraction(conn, sector), 2)


def yield_estimate_cache(conn: sqlite3.Connection | None) -> dict[tuple[str, str], float]:
    if conn is None:
        return {}
    term_yields = {
        (str(row[0]), str(row[1])): float(row[2] or 0)
        for row in conn.execute(
            """SELECT sector,term,AVG(qualified_new) FROM search_jobs
               WHERE status='completed' GROUP BY sector,term"""
        )
    }
    sector_yields = {
        str(row[0]): float(row[1] or 0)
        for row in conn.execute(
            """SELECT sector,AVG(qualified_new) FROM search_jobs
               WHERE status='completed' GROUP BY sector"""
        )
    }
    ab_fractions = {
        str(row[0]): float(row[1] or 0.35)
        for row in conn.execute(
            """SELECT COALESCE(ic.industry_label,c.sector),
                      AVG(CASE WHEN li.sales_rank IN ('A','B') THEN 1.0 ELSE 0.0 END)
               FROM companies c
               LEFT JOIN company_industry_classification ic
                 ON ic.company_id=c.id AND ic.taxonomy_version=4
               LEFT JOIN lead_intelligence li
                 ON li.company_id=c.id AND li.intelligence_version=4
               GROUP BY COALESCE(ic.industry_label,c.sector)"""
        )
    }
    cache: dict[tuple[str, str], float] = {}
    for sector, terms in SECTOR_TERMS.items():
        fraction = ab_fractions.get(sector, 0.35)
        for term in set(terms + V4_EXTRA_TERMS.get(sector, [])):
            raw = term_yields.get((sector, term), sector_yields.get(sector, 8.0))
            cache[(sector, term)] = round(raw * fraction, 2)
    return cache


def weighted_market_order(tasks: list[QueryTask]) -> list[QueryTask]:
    """Interleave tasks at 55% Klang Valley, 25% Johor, and 20% Penang."""
    groups: dict[str, deque[QueryTask]] = {name: deque() for name in ("Klang Valley", "Johor", "Penang")}
    for task in sorted(tasks, key=lambda item: (item.priority, -item.expected_ab_yield, item.term, item.prompt)):
        groups.setdefault(market_name(task.state), deque()).append(task)
    cycle = ["Klang Valley"] * 11 + ["Johor"] * 5 + ["Penang"] * 4
    ordered: list[QueryTask] = []
    while any(groups.get(name) for name in cycle):
        progressed = False
        for name in cycle:
            if groups.get(name):
                ordered.append(groups[name].popleft())
                progressed = True
        if not progressed:
            break
    for group in groups.values():
        ordered.extend(group)
    return ordered


def build_manifest(conn: sqlite3.Connection | None = None) -> list[QueryTask]:
    tasks: list[QueryTask] = []
    estimate_cache = yield_estimate_cache(conn)
    estimate = lambda sector, term: estimate_cache.get((sector, term), 4.0)
    completed_prompts: set[str] = set()
    if conn is not None:
        completed_prompts = {row[0] for row in conn.execute(
            "SELECT DISTINCT prompt FROM search_jobs WHERE status='completed'"
        )}
    for locality, state in PRIMARY_MARKET_LOCALITIES:
        for sector, base_terms in SECTOR_TERMS.items():
            terms = list(dict.fromkeys(base_terms + V4_EXTRA_TERMS.get(sector, [])))
            for term in terms:
                prompt = f"{term} in {locality}, {state}, Malaysia"
                if prompt in completed_prompts:
                    continue
                bucket, priority = strategy_for(sector, term)
                tasks.append(QueryTask(
                    prompt=prompt, sector=sector, locality=locality, state=state, term=term,
                    geo_level="city", priority=priority, strategy_bucket=bucket,
                    expected_ab_yield=estimate(sector, term),
                ))
    if conn is not None:
        # Carry forward unfinished V3 prompts in the selected conversion markets.
        carry_forward = conn.execute(
            """SELECT prompt,sector,locality,state,term,geo_level,parent_prompt
               FROM search_jobs WHERE taxonomy_version=3 AND status<>'completed'
                 AND state IN ('Selangor','Federal Territory','Johor','Penang')"""
        ).fetchall()
        for prompt, sector, locality, state, term, geo_level, parent_prompt in carry_forward:
            if prompt in completed_prompts:
                continue
            bucket, priority = strategy_for(sector, term, geo_level)
            tasks.append(QueryTask(
                prompt=prompt, sector=sector, locality=locality, state=state, term=term,
                geo_level=geo_level, parent_prompt=parent_prompt or "", priority=priority,
                strategy_bucket=bucket, expected_ab_yield=estimate(sector, term),
            ))
        saturated = conn.execute(
            """SELECT prompt,sector,locality,state,term,strategy_bucket,expected_ab_yield
               FROM search_jobs WHERE taxonomy_version=? AND geo_level='city' AND status='completed'
                 AND (links_discovered>=80 OR processed_count>=100 OR qualified_new>=20)""",
            (TAXONOMY_VERSION,),
        ).fetchall()
        for prompt, sector, locality, state, term, bucket, estimate in saturated:
            tasks.extend(expand_task(QueryTask(
                prompt=prompt, sector=sector, locality=locality, state=state, term=term,
                strategy_bucket=bucket or "long_tail", expected_ab_yield=float(estimate or 0),
            )))
        existing_children = conn.execute(
            """SELECT prompt,sector,locality,state,term,geo_level,parent_prompt,priority,
                      strategy_bucket,expected_ab_yield
               FROM search_jobs WHERE taxonomy_version=? AND geo_level<>'city'""",
            (TAXONOMY_VERSION,),
        ).fetchall()
        tasks.extend(QueryTask(
            prompt=row[0], sector=row[1], locality=row[2], state=row[3], term=row[4],
            geo_level=row[5], parent_prompt=row[6] or "", priority=int(row[7]),
            strategy_bucket=row[8] or "long_tail", expected_ab_yield=float(row[9] or 0),
        ) for row in existing_children)
    # Keep a deep, conversion-market-only reserve ready. Dynamic expansion remains
    # the main source of children, but pre-seeding the highest-value branches
    # guarantees at least 15,000 uncompleted prompts after restart.
    existing_prompts = {task.prompt for task in tasks}
    if len(existing_prompts) < 15_500:
        for parent in list(tasks):
            if parent.sector not in HIGH_VALUE_INDUSTRIES or parent.geo_level != "city":
                continue
            for child in expand_task(parent):
                if child.prompt in completed_prompts or child.prompt in existing_prompts:
                    continue
                tasks.append(child)
                existing_prompts.add(child.prompt)
                if len(existing_prompts) >= 15_500:
                    break
            if len(existing_prompts) >= 15_500:
                break
    unique = {task.prompt: task for task in tasks}
    return weighted_market_order(list(unique.values()))


def expand_task(task: QueryTask) -> list[QueryTask]:
    if task.geo_level != "city":
        return []
    children = [QueryTask(
        prompt=f"{task.term} in {child}, {task.state}, Malaysia",
        sector=task.sector, locality=child, state=task.state, term=task.term,
        geo_level="district", parent_prompt=task.prompt, priority=task.priority + 10,
        strategy_bucket="long_tail", expected_ab_yield=task.expected_ab_yield,
    ) for child in DENSE_CHILDREN.get(task.locality, [])]
    children.extend(QueryTask(
        prompt=f"{task.term} in {postcode}, {task.state}, Malaysia",
        sector=task.sector, locality=postcode, state=task.state, term=task.term,
        geo_level="postcode", parent_prompt=task.prompt, priority=task.priority + 20,
        strategy_bucket="long_tail", expected_ab_yield=task.expected_ab_yield,
    ) for postcode in POSTCODES_BY_STATE.get(task.state, []))
    return children


def register_manifest(conn: sqlite3.Connection, manifest: list[QueryTask]) -> None:
    conn.executemany(
        """INSERT INTO search_jobs(
               taxonomy_version,prompt,sector,locality,state,term,geo_level,
               parent_prompt,priority,status,strategy_bucket,expected_ab_yield)
           VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)
           ON CONFLICT(taxonomy_version,prompt) DO UPDATE SET
               priority=excluded.priority,sector=excluded.sector,locality=excluded.locality,
               state=excluded.state,term=excluded.term,geo_level=excluded.geo_level,
               parent_prompt=excluded.parent_prompt,strategy_bucket=excluded.strategy_bucket,
               expected_ab_yield=excluded.expected_ab_yield""",
        [
            (task.taxonomy_version, task.prompt, task.sector, task.locality, task.state,
             task.term, task.geo_level, task.parent_prompt, task.priority,
             task.strategy_bucket, task.expected_ab_yield)
            for task in manifest
        ],
    )
    conn.commit()


def is_relevant(task: QueryTask, name: str, category: str) -> bool:
    haystack = normalized(f"{name} {category}")
    return any(keyword in haystack for keyword in SECTOR_KEYWORDS[task.sector])


def find_existing_company(conn: sqlite3.Connection, place_id: str, phone: str, name: str, address: str, domain: str) -> sqlite3.Row | None:
    if place_id:
        row = conn.execute("SELECT id FROM companies WHERE maps_place_id=?", (place_id,)).fetchone()
        if row:
            return row
    # A distinct stable Place ID represents a distinct physical branch even when
    # a chain shares one central phone/domain. Fall back to phone only when Maps
    # did not expose a stable location identifier.
    if phone and not place_id:
        row = conn.execute("SELECT id FROM companies WHERE normalized_phone=?", (phone,)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT id FROM companies WHERE normalized_name=? AND normalized_address=?", (name, address)).fetchone()
    if row:
        return row
    if domain:
        return conn.execute(
            "SELECT id FROM companies WHERE website_domain=? AND normalized_name=? AND normalized_address=?",
            (domain, name, address),
        ).fetchone()
    return None


def save_one_result(conn: sqlite3.Connection, task: QueryTask, raw: dict[str, Any]) -> tuple[str, int | None]:
    now = utc_now()
    name = usable(raw.get("name"))
    address = usable(raw.get("address"))
    category = usable(raw.get("category"))
    phone = normalize_phone(raw.get("phone"))
    website = usable(raw.get("website"))
    website = website if website.startswith(("http://", "https://")) else ""
    domain = website_domain(website)
    place_id = usable(raw.get("place_id"))
    source_url = usable(raw.get("source_url"))
    reviews_count = integer_count(raw.get("reviews_count"))
    if bool(raw.get("is_closed")):
        return "permanently_closed", None
    if not name or not address:
        return "missing_name_or_address", None
    if not phone and not website:
        return "missing_contact", None
    if not is_relevant(task, name, category):
        return "irrelevant_category", None
    norm_name, norm_address = normalized(name), normalized(address)
    existing = find_existing_company(conn, place_id, phone, norm_name, norm_address, domain)
    lead_tier = "SEO_UPGRADE" if website else "WEBSITE_BUILD"
    score = min(100, 50 + (20 if phone else 0) + (20 if website else 0) + (10 if category else 0))
    if existing:
        company_id = int(existing[0])
        conn.execute(
            """UPDATE companies SET last_seen_at=?, phone=CASE WHEN phone='' THEN ? ELSE phone END,
               website=CASE WHEN website='' THEN ? ELSE website END,
               website_domain=CASE WHEN website_domain='' THEN ? ELSE website_domain END,
               maps_place_id=COALESCE(NULLIF(maps_place_id,''),?), source_url=COALESCE(NULLIF(source_url,''),?),
               qualification_score=MAX(qualification_score,?),reviews_count=MAX(COALESCE(reviews_count,0),?),
               operational_status='active',listing_checked_at=? WHERE id=?""",
            (now, phone, website, domain, place_id, source_url, score, reviews_count, now, company_id),
        )
        conn.execute("INSERT OR IGNORE INTO provenance(company_id,prompt,seen_at) VALUES(?,?,?)", (company_id, task.prompt, now))
        set_company_classification(conn, company_id, task.sector, 90, "search_term")
        score_company(conn, company_id)
        return "duplicate", company_id
    identity_source = f"place:{place_id}" if place_id else (f"phone:{phone}" if phone else f"location:{norm_name}|{norm_address}")
    identity = identity_source
    suffix = 0
    while conn.execute("SELECT 1 FROM companies WHERE identity=?", (identity,)).fetchone():
        suffix += 1
        identity = f"{identity_source}:{suffix}"
    cursor = conn.execute(
        """INSERT INTO companies(identity,sector,business_name,maps_category,address,city_state,
           phone,website,website_domain,rating,opening_hours,plus_code,first_seen_at,last_seen_at,
           maps_place_id,source_url,lead_tier,parent_brand_key,qualification_score,country,state_name,
           locality,geo_level,normalized_name,normalized_address,normalized_phone,website_audit_status,
           reviews_count,operational_status,listing_checked_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (identity, task.sector, name, category, address, f"{task.locality}, {task.state}", phone,
         website, domain, usable(raw.get("rating")), usable(raw.get("opening_hours")),
         usable(raw.get("plus_code")), now, now, place_id, source_url, lead_tier,
         parent_brand_key(name), score, "Malaysia", task.state, task.locality, task.geo_level,
         norm_name, norm_address, phone, "pending" if website else "not_applicable",
         reviews_count, "active", now),
    )
    company_id = int(cursor.lastrowid)
    conn.execute("INSERT OR IGNORE INTO provenance(company_id,prompt,seen_at) VALUES(?,?,?)", (company_id, task.prompt, now))
    set_company_classification(conn, company_id, task.sector, 90, "search_term")
    if website:
        conn.execute("INSERT OR IGNORE INTO website_audits(company_id,status) VALUES(?,'pending')", (company_id,))
    score_company(conn, company_id)
    return "new", company_id


def persist_observation(conn: sqlite3.Connection, task: QueryTask, raw: dict[str, Any]) -> str:
    key_source = "|".join((task.prompt, usable(raw.get("place_id")), usable(raw.get("source_url")), usable(raw.get("name")), usable(raw.get("address"))))
    observation_key = hashlib.sha256(key_source.encode("utf-8", errors="ignore")).hexdigest()
    existing = conn.execute("SELECT rejection_reason,company_id FROM raw_observations WHERE observation_key=?", (observation_key,)).fetchone()
    if existing:
        if existing[1]:
            set_company_classification(conn, int(existing[1]), task.sector, 90, "search_term")
            score_company(conn, int(existing[1]))
            conn.commit()
        return "duplicate_observation"
    reason, company_id = save_one_result(conn, task, raw)
    conn.execute(
        """INSERT INTO raw_observations(observation_key,prompt,source_url,place_id,payload_json,
           accepted,rejection_reason,company_id,observed_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (observation_key, task.prompt, usable(raw.get("source_url")), usable(raw.get("place_id")),
         json.dumps(raw, ensure_ascii=False), int(reason in {"new", "duplicate"}),
         "" if reason in {"new", "duplicate"} else reason, company_id, utc_now()),
    )
    conn.commit()
    return reason


def crawl_task(task: QueryTask, db_path: str) -> CrawlOutcome:
    counters = {"processed": 0, "accepted": 0, "new": 0, "duplicate": 0, "rejected": 0}
    conn = open_db(Path(db_path))
    critical_since: float | None = None

    def memory_guard() -> bool:
        nonlocal critical_since
        if available_memory_gb() < RAM_CRITICAL_GB:
            critical_since = critical_since or time.monotonic()
            return time.monotonic() - critical_since >= MEMORY_PRESSURE_GRACE_SECONDS
        critical_since = None
        return False

    def callback(company: Any) -> None:
        raw = company.to_dict()
        counters["processed"] += 1
        reason = persist_observation(conn, task, raw)
        if reason == "new":
            counters["new"] += 1
            counters["accepted"] += 1
        elif reason in {"duplicate", "duplicate_observation"}:
            counters["duplicate"] += 1
            counters["accepted"] += 1
        else:
            counters["rejected"] += 1

    try:
        from core.api.server import run_crawl_in_process
        payload = run_crawl_in_process(
            prompt=task.prompt, output_format="json", headless=True, locale="en-MY",
            max_results=task.initial_results, scroll_timeout=task.scroll_timeout,
            max_scroll_attempts=MAX_SCROLL_ATTEMPTS, adaptive_results=task.adaptive_results,
            hard_result_cap=task.hard_result_cap, include_metrics=True, result_callback=callback,
            memory_guard=memory_guard, page_recycle_interval=10, track_reviews=False,
        )
        return CrawlOutcome(task, int(payload["links_discovered"]), counters["processed"],
                            counters["accepted"], counters["new"], counters["duplicate"], counters["rejected"],
                            page_recycle_count=int(payload.get("page_recycle_count", 0)),
                            memory_cleanup_count=int(payload.get("memory_cleanup_count", 0)))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        lowered = error.lower()
        throttled = any(marker in lowered for marker in ("google_block", "captcha", "unusual traffic", "too many requests"))
        return CrawlOutcome(task, processed_count=counters["processed"], accepted_count=counters["accepted"],
                            new_count=counters["new"], duplicate_count=counters["duplicate"],
                            rejected_count=counters["rejected"], error=error, throttled=throttled)
    finally:
        conn.close()


def available_memory_gb() -> float:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended_virtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return round(status.avail_phys / (1024 ** 3), 2)
    return 99.0


def desired_worker_count(
    available_ram: float,
    preferred: int = DEFAULT_WORKERS,
    maximum: int = MAXIMUM_WORKERS,
    cooldown: bool = False,
    error_rate: float = 0.0,
    current_limit: int = 1,
    five_worker_canary_complete: bool = False,
) -> int:
    if cooldown:
        return 1
    if error_rate >= 0.05:
        return 1
    if available_ram < RAM_CRITICAL_GB:
        return max(1, current_limit - 1)
    if available_ram < RAM_RESERVE_GB:
        return min(current_limit, BASE_WORKERS)

    preferred = max(1, min(preferred, maximum, MAXIMUM_WORKERS))
    maximum = max(preferred, min(maximum, MAXIMUM_WORKERS))
    if current_limit < min(preferred, BASE_WORKERS):
        return min(preferred, BASE_WORKERS)
    if current_limit < preferred:
        return preferred if available_ram >= RAM_LAUNCH_THRESHOLD_GB else current_limit
    if (current_limit == preferred and maximum > preferred
            and five_worker_canary_complete
            and available_ram >= RAM_LAUNCH_THRESHOLD_GB):
        return min(preferred + 1, maximum)
    return min(current_limit, maximum)


def worker_upscale_stable_seconds(next_worker_count: int) -> int:
    if next_worker_count >= 6:
        return RAM_SIXTH_UPSCALE_STABLE_SECONDS
    if next_worker_count >= 5:
        return RAM_FIFTH_UPSCALE_STABLE_SECONDS
    return RAM_BASE_UPSCALE_STABLE_SECONDS


def ram_operating_state(available_ram: float) -> str:
    if available_ram < RAM_CRITICAL_GB:
        return "critical"
    if available_ram < RAM_LAUNCH_THRESHOLD_GB:
        return "constrained"
    return "healthy"


def qualified_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])


def record_event(conn: sqlite3.Connection, event_type: str, details: str, workers: int) -> None:
    conn.execute("INSERT INTO worker_events(event_type,details,worker_count,available_ram_gb,created_at) VALUES(?,?,?,?,?)",
                 (event_type, details, workers, available_memory_gb(), utc_now()))
    conn.commit()


def record_checkpoints(conn: sqlite3.Connection, count: int, target: int) -> None:
    for threshold in (10_000, 25_000, 50_000, 100_000, 150_000, 200_000, 300_000, target):
        if count >= threshold:
            conn.execute("INSERT OR IGNORE INTO checkpoints(name,qualified_count,reached_at) VALUES(?,?,?)", (str(threshold), count, utc_now()))


def rolling_metrics(conn: sqlite3.Connection, hours: int = 24) -> dict[str, Any]:
    row = conn.execute(
        """WITH completed AS (
               SELECT completed_at,qualified_new FROM jobs
                WHERE status='completed' AND completed_at IS NOT NULL
               UNION ALL
               SELECT completed_at,qualified_new FROM search_jobs
                WHERE status='completed' AND completed_at IS NOT NULL
           ), windowed AS (
               SELECT completed_at,qualified_new FROM completed
                WHERE julianday(completed_at)>=julianday('now',?)
           )
           SELECT COUNT(*),COALESCE(SUM(qualified_new),0),MIN(completed_at),
                  (SELECT MIN(completed_at) FROM completed)
             FROM windowed""",
        (f"-{hours} hours",),
    ).fetchone()
    query_count, qualified_added, window_first, all_time_first = int(row[0]), int(row[1]), row[2], row[3]
    if all_time_first:
        elapsed = float(conn.execute(
            "SELECT MIN(?,MAX(0.25,(julianday('now')-julianday(?))*24.0))",
            (float(hours), all_time_first),
        ).fetchone()[0])
    else:
        elapsed = 0.0
    if elapsed >= float(hours) - 0.001:
        sample_start = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
            (f"-{hours} hours",),
        ).fetchone()[0]
    else:
        sample_start = window_first
    return {
        "queries_completed": query_count,
        "qualified_added": qualified_added,
        "sample_start": sample_start,
        "window_hours": round(elapsed, 3),
        "query_rate": round(query_count / elapsed, 2) if elapsed else 0.0,
        "qualified_rate": round(qualified_added / elapsed, 2) if elapsed else 0.0,
    }


def write_status(
    conn: sqlite3.Connection,
    target: int,
    workers: int,
    started_at: float,
    halt_reason: str | None = None,
    pilot_status: str = "pending",
    worker_limit: int | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    count = qualified_count(conn)
    jobs = dict(conn.execute(
        "SELECT status,COUNT(*) FROM search_jobs WHERE taxonomy_version=? GROUP BY status",
        (TAXONOMY_VERSION,),
    ).fetchall())
    lead_ranks = dict(conn.execute(
        """SELECT sales_rank,COUNT(*) FROM lead_intelligence
           WHERE intelligence_version=4 GROUP BY sales_rank"""
    ).fetchall())
    offers = dict(conn.execute(
        """SELECT primary_offer,COUNT(*) FROM lead_intelligence
           WHERE intelligence_version=4 GROUP BY primary_offer"""
    ).fetchall())
    metrics_24h = rolling_metrics(conn, 24)
    metrics_1h = rolling_metrics(conn, 1)
    rate = float(metrics_24h["qualified_rate"])
    eta = round((target - count) / rate, 1) if rate > 0 and count < target else None
    ram = available_memory_gb()
    diagnostics = diagnostics or {}
    payload = json.dumps({
        "updated_at": utc_now(), "collector_version": COLLECTOR_VERSION,
        "target_qualified_companies": target, "qualified_companies": count,
        "active_workers": workers, "actual_browser_processes": workers,
        "scheduler_worker_limit": worker_limit if worker_limit is not None else workers,
        "preferred_workers": DEFAULT_WORKERS,
        "maximum_workers": MAXIMUM_WORKERS,
        "available_ram_gb": ram, "reserved_ram_gb": RAM_RESERVE_GB,
        "ram_policy_mode": RAM_POLICY_MODE,
        "ram_operating_state": ram_operating_state(ram),
        "memory_pressure_pauses": int(diagnostics.get("memory_pressure_pauses", 0)),
        "reclaimed_jobs": int(diagnostics.get("reclaimed_jobs", 0)),
        "lowest_available_ram_gb": diagnostics.get("lowest_available_ram_gb", ram),
        "lowest_available_ram_at": diagnostics.get("lowest_available_ram_at"),
        "page_recycle_count": int(diagnostics.get("page_recycle_count", 0)),
        "memory_cleanup_count": int(diagnostics.get("memory_cleanup_count", 0)),
        "ram_canary_completed": int(diagnostics.get("ram_canary_completed", 0)),
        "ram_canary_target": RAM_CANARY_QUERIES,
        "ram_canary_status": (
            "passed_5_worker_canary"
            if int(diagnostics.get("ram_canary_completed", 0)) >= RAM_CANARY_QUERIES
            else "running_5_worker_canary"
        ),
        "qualified_last_hour": metrics_1h["qualified_added"],
        "queries_completed_24h": metrics_24h["queries_completed"],
        "query_rate_per_hour_24h": metrics_24h["query_rate"],
        "query_rate_window_hours": metrics_24h["window_hours"],
        "query_rate_sample_start": metrics_24h["sample_start"],
        "qualified_added_24h": metrics_24h["qualified_added"],
        "qualified_rate_per_hour_24h": metrics_24h["qualified_rate"],
        "eta_hours": eta, "pilot_status": pilot_status, "job_status_counts": jobs,
        "lead_rank_counts": lead_ranks, "primary_offer_counts": offers,
        "primary_markets": ["Klang Valley", "Johor", "Penang"],
        "market_query_allocation": {"Klang Valley": 55, "Johor": 25, "Penang": 20},
        "uptime_seconds": int(time.time() - started_at), "halt_reason": halt_reason,
        "database": str(DB_PATH),
    }, indent=2)
    temporary_status = STATUS_PATH.with_suffix(".json.tmp")
    temporary_status.write_text(payload, encoding="utf-8")
    os.replace(temporary_status, STATUS_PATH)


def low_yield_siblings(conn: sqlite3.Connection, task: QueryTask) -> bool:
    if not task.parent_prompt:
        return False
    rows = conn.execute(
        """SELECT ab_leads_new,duplicate_count,processed_count FROM search_jobs
           WHERE taxonomy_version=? AND parent_prompt=? AND term=? AND status='completed'
           ORDER BY completed_at DESC LIMIT 2""",
        (task.taxonomy_version, task.parent_prompt, task.term),
    ).fetchall()
    if len(rows) != 2:
        return False
    low_ab = all(int(row[0] or 0) < 3 for row in rows)
    duplicate_saturated = all(
        int(row[2] or 0) > 0 and int(row[1] or 0) / int(row[2]) >= 0.80 for row in rows
    )
    return low_ab or duplicate_saturated


def count_ab_leads_for_prompt(conn: sqlite3.Connection, task: QueryTask) -> int:
    row = conn.execute(
        "SELECT started_at FROM search_jobs WHERE taxonomy_version=? AND prompt=?",
        (task.taxonomy_version, task.prompt),
    ).fetchone()
    started_at = row[0] if row else None
    return int(conn.execute(
        """SELECT COUNT(DISTINCT p.company_id) FROM provenance p
           JOIN companies c ON c.id=p.company_id
           JOIN lead_intelligence li ON li.company_id=c.id AND li.intelligence_version=4
           WHERE p.prompt=? AND li.sales_rank IN ('A','B')
             AND (? IS NULL OR julianday(c.first_seen_at)>=julianday(?))""",
        (task.prompt, started_at, started_at),
    ).fetchone()[0])


def run_batch(args: argparse.Namespace) -> int:
    setup_logging()
    started_at = time.time()
    conn = open_db()
    backfilled = backfill_v4(conn)
    logging.info("V4 intelligence backfill refreshed %s existing leads", backfilled)
    # A previous process may have been interrupted after marking jobs running.
    # They are safe to resume because every observation is persisted and deduplicated.
    conn.execute(
        """UPDATE search_jobs SET status='pending',worker_id=NULL,error='Resuming after collector restart'
           WHERE taxonomy_version IN (3,4) AND status='running'""",
    )
    conn.commit()
    manifest = build_manifest(conn)
    register_manifest(conn, manifest)
    logging.info("V4 manifest contains %s queries; target=%s", len(manifest), args.target)
    if args.dry_run:
        print(json.dumps({"queries": len(manifest), "initial_capacity": len(manifest) * args.initial_results,
                          "hard_capacity": len(manifest) * args.hard_result_cap, "target": args.target}))
        return 0
    count = qualified_count(conn)
    if count >= args.target:
        write_status(conn, args.target, 0, started_at, worker_limit=0)
        return 0
    task_by_prompt = {task.prompt: replace(task, initial_results=args.initial_results,
                                           adaptive_results=args.adaptive_results,
                                           hard_result_cap=args.hard_result_cap,
                                           scroll_timeout=args.scroll_timeout) for task in manifest}
    pending = deque(task for task in task_by_prompt.values() if conn.execute(
        "SELECT status FROM search_jobs WHERE taxonomy_version=? AND prompt=?",
        (task.taxonomy_version, task.prompt),
    ).fetchone()[0] in {"pending", "failed"})
    active: dict[concurrent.futures.Future[CrawlOutcome], QueryTask] = {}
    outcomes = deque(maxlen=20)
    throttle_times: deque[float] = deque()
    upscale_candidate: int | None = None
    upscale_candidate_since: float | None = None
    five_worker_canary_prompts: set[str] = set()
    cooldown_until = 0.0
    halt_reason: str | None = None
    pilot_status = "running"
    session_completed = 0
    initial_ram = available_memory_gb()
    diagnostics: dict[str, Any] = {
        "memory_pressure_pauses": 0,
        "reclaimed_jobs": 0,
        "page_recycle_count": 0,
        "memory_cleanup_count": 0,
        "ram_canary_completed": 0,
        "lowest_available_ram_gb": initial_ram,
        "lowest_available_ram_at": utc_now(),
    }
    completed_v3 = int(conn.execute(
        "SELECT COUNT(*) FROM search_jobs WHERE taxonomy_version=? AND status='completed'",
        (TAXONOMY_VERSION,),
    ).fetchone()[0])
    effective_limit = 1
    record_event(conn, "collector_started", f"target={args.target}; preferred={args.workers}", effective_limit)

    with concurrent.futures.ProcessPoolExecutor(max_workers=args.max_workers) as executor:
        while count < args.target and (pending or active):
            now = time.time()
            ram = available_memory_gb()
            if ram < float(diagnostics["lowest_available_ram_gb"]):
                diagnostics["lowest_available_ram_gb"] = ram
                diagnostics["lowest_available_ram_at"] = utc_now()
            error_rate = sum(outcomes) / len(outcomes) if outcomes else 0.0
            desired_limit = desired_worker_count(
                ram, preferred=min(args.workers, args.max_workers),
                maximum=args.max_workers,
                cooldown=now < cooldown_until, error_rate=error_rate,
                current_limit=effective_limit,
                five_worker_canary_complete=(
                    int(diagnostics["ram_canary_completed"]) >= RAM_CANARY_QUERIES
                ),
            )
            if desired_limit < effective_limit:
                effective_limit = desired_limit
                upscale_candidate = None
                upscale_candidate_since = None
                record_event(conn, "concurrency_adjusted", f"immediate downscale at {ram:.2f} GB free", effective_limit)
            elif desired_limit > effective_limit:
                if upscale_candidate != desired_limit:
                    upscale_candidate = desired_limit
                    upscale_candidate_since = now
                required_stability = worker_upscale_stable_seconds(effective_limit + 1)
                if (upscale_candidate_since is not None
                        and now - upscale_candidate_since >= required_stability):
                    effective_limit = min(desired_limit, effective_limit + 1)
                    record_event(
                        conn, "concurrency_adjusted",
                        f"{required_stability}-second adaptive upscale at {ram:.2f} GB free",
                        effective_limit,
                    )
                    upscale_candidate = None
                    upscale_candidate_since = None
            else:
                upscale_candidate = None
                upscale_candidate_since = None

            while (pending and len(active) < effective_limit and count < args.target
                   and ram >= RAM_CRITICAL_GB):
                task = pending.popleft()
                row = conn.execute(
                    "SELECT status,attempts FROM search_jobs WHERE taxonomy_version=? AND prompt=?",
                    (TAXONOMY_VERSION, task.prompt),
                ).fetchone()
                if row and row[0] == "completed":
                    continue
                if low_yield_siblings(conn, task):
                    conn.execute(
                        "UPDATE search_jobs SET status='skipped',error='Two sibling variants produced fewer than 3 new leads' WHERE taxonomy_version=? AND prompt=?",
                        (TAXONOMY_VERSION, task.prompt),
                    )
                    conn.commit()
                    continue
                attempts = int(row[1] if row else 0) + 1
                conn.execute(
                    """UPDATE search_jobs SET status='running',attempts=?,started_at=?,completed_at=NULL,error=NULL,
                       worker_id=? WHERE taxonomy_version=? AND prompt=?""",
                    (attempts, utc_now(), f"slot-{len(active)+1}", TAXONOMY_VERSION, task.prompt),
                )
                conn.commit()
                future = executor.submit(crawl_task, task, str(DB_PATH))
                active[future] = task
                if effective_limit >= 5 and int(diagnostics["ram_canary_completed"]) < RAM_CANARY_QUERIES:
                    five_worker_canary_prompts.add(task.prompt)
                logging.info("START [%s workers] %s", effective_limit, task.prompt)

            if not active:
                if ram < RAM_CRITICAL_GB:
                    write_status(conn, args.target, 0, started_at, pilot_status=pilot_status,
                                 worker_limit=effective_limit, diagnostics=diagnostics)
                    time.sleep(5)
                    continue
                if cooldown_until > time.time():
                    time.sleep(min(5, cooldown_until - time.time()))
                    write_status(conn, args.target, len(active), started_at, pilot_status=pilot_status,
                                 worker_limit=effective_limit, diagnostics=diagnostics)
                    continue
                break
            done, _ = concurrent.futures.wait(active, timeout=5, return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                write_status(conn, args.target, len(active), started_at, pilot_status=pilot_status,
                             worker_limit=effective_limit, diagnostics=diagnostics)
                continue
            for future in done:
                task = active.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = CrawlOutcome(task, error=f"ControllerError: {type(exc).__name__}: {exc}")
                memory_reclaimed = bool(outcome.error and "memory_pressure" in outcome.error.lower())
                if not memory_reclaimed:
                    outcomes.append(1 if outcome.error else 0)
                if outcome.error:
                    row = conn.execute(
                        "SELECT attempts FROM search_jobs WHERE taxonomy_version=? AND prompt=?",
                        (TAXONOMY_VERSION, task.prompt),
                    ).fetchone()
                    attempts = int(row[0])
                    if memory_reclaimed:
                        conn.execute(
                            """UPDATE search_jobs SET status='pending',attempts=?,completed_at=NULL,
                               processed_count=?,error=?,throttle_detected=0,worker_id=NULL
                               WHERE taxonomy_version=? AND prompt=?""",
                            (max(0, attempts - 1), outcome.processed_count, outcome.error,
                             TAXONOMY_VERSION, task.prompt),
                        )
                        pending.append(task)
                        diagnostics["memory_pressure_pauses"] += 1
                        diagnostics["reclaimed_jobs"] += 1
                        cooldown_until = max(cooldown_until, time.time() + MEMORY_RECOVERY_COOLDOWN_SECONDS)
                        effective_limit = 1
                        record_event(conn, "memory_pressure_reclaim", outcome.error, effective_limit)
                        logging.warning("MEMORY RECLAIMED %s after %s processed", task.prompt, outcome.processed_count)
                    else:
                        conn.execute("UPDATE search_jobs SET status='failed',completed_at=?,processed_count=?,error=?,throttle_detected=? WHERE taxonomy_version=? AND prompt=?",
                                     (utc_now(), outcome.processed_count, outcome.error, int(outcome.throttled), TAXONOMY_VERSION, task.prompt))
                    if outcome.throttled and not memory_reclaimed:
                        throttle_times.append(time.time())
                        while throttle_times and throttle_times[0] < time.time() - 7200:
                            throttle_times.popleft()
                        record_event(conn, "throttle", outcome.error, 1)
                        cooldown_until = time.time() + THROTTLE_COOLDOWN_SECONDS
                        effective_limit = 1
                        if len(throttle_times) >= 2:
                            halt_reason = "Persistent Google block/throttle detected twice within two hours."
                    elif not memory_reclaimed and attempts < MAX_ATTEMPTS:
                        pending.append(task)
                    if not memory_reclaimed:
                        logging.error("FAILED %s: %s", task.prompt, outcome.error)
                else:
                    ab_leads_new = count_ab_leads_for_prompt(conn, task)
                    conn.execute(
                        """UPDATE search_jobs SET status='completed',completed_at=?,source_count=?,links_discovered=?,
                           processed_count=?,qualified_new=?,duplicate_count=?,rejected_count=?,ab_leads_new=?,error=NULL
                           WHERE taxonomy_version=? AND prompt=?""",
                        (utc_now(), outcome.processed_count, outcome.links_discovered, outcome.processed_count,
                         outcome.new_count, outcome.duplicate_count, outcome.rejected_count, ab_leads_new,
                         TAXONOMY_VERSION, task.prompt),
                    )
                    completed_v3 += 1
                    session_completed += 1
                    if task.prompt in five_worker_canary_prompts:
                        diagnostics["ram_canary_completed"] = min(
                            int(diagnostics["ram_canary_completed"]) + 1,
                            RAM_CANARY_QUERIES,
                        )
                        five_worker_canary_prompts.discard(task.prompt)
                    diagnostics["page_recycle_count"] += outcome.page_recycle_count
                    diagnostics["memory_cleanup_count"] += outcome.memory_cleanup_count
                    logging.info("DONE %s: %s links / %s processed / %s new / %s A/B / %s duplicate / %s rejected",
                                 task.prompt, outcome.links_discovered, outcome.processed_count,
                                 outcome.new_count, ab_leads_new, outcome.duplicate_count, outcome.rejected_count)
                    if outcome.links_discovered >= 80 or outcome.processed_count >= 100 or outcome.new_count >= 20:
                        children = [replace(child, initial_results=args.initial_results,
                                            adaptive_results=args.adaptive_results,
                                            hard_result_cap=args.hard_result_cap,
                                            scroll_timeout=args.scroll_timeout) for child in expand_task(task)]
                        if children:
                            register_manifest(conn, children)
                            known = set(task_by_prompt)
                            for child in children:
                                if child.prompt not in known:
                                    task_by_prompt[child.prompt] = child
                                    pending.appendleft(child)
                                    known.add(child.prompt)
                conn.commit()
                count = qualified_count(conn)
                record_checkpoints(conn, count, args.target)
                if completed_v3 >= PILOT_QUERIES and pilot_status == "running":
                    failed = int(conn.execute(
                        "SELECT COUNT(*) FROM search_jobs WHERE taxonomy_version=? AND status='failed'",
                        (TAXONOMY_VERSION,),
                    ).fetchone()[0])
                    total = max(1, completed_v3 + failed)
                    irrelevant = int(conn.execute("SELECT COUNT(*) FROM raw_observations WHERE rejection_reason='irrelevant_category'").fetchone()[0])
                    accepted = int(conn.execute("SELECT COUNT(*) FROM raw_observations WHERE accepted=1").fetchone()[0])
                    precision_proxy = accepted / max(1, accepted + irrelevant)
                    pilot_status = "passed" if failed / total < 0.05 and precision_proxy >= 0.95 else "needs_review"
                    record_event(conn, "pilot_complete", f"status={pilot_status}; precision_proxy={precision_proxy:.3f}; failure_rate={failed/total:.3f}", effective_limit)
                write_status(conn, args.target, len(active), started_at, halt_reason, pilot_status,
                             worker_limit=effective_limit, diagnostics=diagnostics)
                if halt_reason:
                    break
            if halt_reason:
                break

    if count >= args.target:
        logging.info("TARGET REACHED: %s qualified locations", count)
    elif not halt_reason:
        halt_reason = f"Query plan exhausted at {count} qualified locations."
    write_status(conn, args.target, 0, started_at, halt_reason, pilot_status,
                 worker_limit=0, diagnostics=diagnostics)
    record_event(conn, "collector_stopped", halt_reason or "target reached", 0)
    conn.close()
    return 0 if count >= args.target else 2


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target", type=int, default=TARGET)
    parser.add_argument("--initial-results", type=int, default=INITIAL_RESULTS)
    parser.add_argument("--adaptive-results", type=int, default=ADAPTIVE_RESULTS)
    parser.add_argument("--hard-result-cap", type=int, default=HARD_RESULT_CAP)
    parser.add_argument("--scroll-timeout", type=int, default=SCROLL_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-workers", type=int, default=MAXIMUM_WORKERS)
    return parser


if __name__ == "__main__":
    raise SystemExit(run_batch(make_parser().parse_args()))
