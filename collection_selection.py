"""Validate dashboard campaign scope and generate only its requested searches."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import batch_collect_malaysia_v2 as batch


def selection_path(database: Path) -> Path:
    return database.with_suffix(database.suffix + '.collection.json')


def catalog() -> dict:
    locations = sorted(set(batch.LOCALITIES + batch.PRIMARY_MARKET_LOCALITIES))
    return {'industries': sorted(batch.SECTOR_TERMS),
            'states': sorted({state for _, state in locations}),
            'cities': [{'state': state, 'locality': city} for city, state in locations]}


def text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label} must be nonempty text, at most 120 characters, without control characters')
    return ' '.join(value.split())


def string_list(value, label: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{label} must be a list of at most {maximum} items')
    return list(dict.fromkeys(text(item, label) for item in value))


def normalize_selection(value: dict | None) -> dict:
    if value is None:
        return {'mode': 'default'}
    if not isinstance(value, dict) or value.get('mode') not in ('default', 'selected'):
        raise ValueError('Choose the default plan or selected industries and locations')
    if value['mode'] == 'default':
        return {'mode': 'default'}
    options = catalog()
    industries = string_list(value.get('industries', []), 'Industries', 30)
    states = string_list(value.get('states', []), 'States', 16)
    keywords = string_list(value.get('custom_keywords', []), 'Custom keywords', 20)
    if set(industries) - set(options['industries']) or set(states) - set(options['states']):
        raise ValueError('Industry or state is not in the supported Malaysian list')
    custom_industry = value.get('custom_industry') if keywords else None
    if keywords:
        if custom_industry not in options['industries']:
            raise ValueError('Assign custom keywords to a supported industry')
        if any(not keyword_tokens(keyword) for keyword in keywords):
            raise ValueError('Custom keywords need a specific business type, not just generic words such as service or company')
        industries = [custom_industry]
    if not industries or not states:
        raise ValueError('Select at least one industry and one state')
    locations = {}
    for key, limit in (('cities', 120), ('custom_locations', 30)):
        values = value.get(key, [])
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f'{key} must be a list of at most {limit} locations')
        cleaned = []
        for item in values:
            if not isinstance(item, dict):
                raise ValueError('Each location must contain a state and locality')
            state = text(item.get('state'), 'Location state')
            locality = text(item.get('locality'), 'Locality')
            if state not in states:
                raise ValueError('Every location must belong to a selected state')
            location = {'state': state, 'locality': locality}
            if key == 'cities' and location not in options['cities']:
                raise ValueError('Unknown city; enter it as a custom area instead')
            if location not in cleaned:
                cleaned.append(location)
        locations[key] = cleaned
    return {'mode': 'selected', 'industries': industries, 'states': states,
            **locations, 'custom_keywords': keywords, 'custom_industry': custom_industry}


def load_selection(database: Path, path: Path | None = None) -> dict:
    source = path or selection_path(database)
    if not source.exists():
        if path is not None:
            raise ValueError('Selection file does not exist')
        return {'mode': 'default'}
    return normalize_selection(json.loads(source.read_text(encoding='utf-8')))


def selected_manifest(selection: dict, conn: sqlite3.Connection | None = None) -> list[batch.QueryTask]:
    locations = selection['cities'] + selection['custom_locations']
    if not locations:
        locations = [item for item in catalog()['cities'] if item['state'] in selection['states']]
    completed = {row[0] for row in conn.execute('SELECT prompt FROM search_jobs WHERE status="completed"')} if conn else set()
    tasks = {}
    for industry in selection['industries']:
        keywords = selection['custom_keywords'] or list(dict.fromkeys(
            batch.SECTOR_TERMS[industry] + batch.V4_EXTRA_TERMS.get(industry, [])))
        for keyword in keywords:
            for location in locations:
                city, state = location['locality'], location['state']
                prompt = f'{keyword} in {city}, {state}, Malaysia'
                if prompt in completed:
                    continue
                tasks[prompt] = batch.QueryTask(prompt, industry, city, state, keyword,
                    strategy_bucket='custom' if selection['custom_keywords'] else 'selected')
    return batch.rank_market_tasks(batch.with_expected_speed(list(tasks.values()), batch.observed_ab_hourly_rates(conn)))


def preview_selection(value: dict, database: Path) -> dict:
    selection = normalize_selection(value)
    conn = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) if database.exists() else None
    try:
        tasks = batch.build_manifest(conn, selection=selection)
        if conn:
            statuses = dict(conn.execute('SELECT prompt,status FROM search_jobs WHERE taxonomy_version=?',
                                         (batch.TAXONOMY_VERSION,)))
            # Restart recovers interrupted running jobs to pending; preview that
            # recovery without mutating the database.
            tasks = [task for task in tasks if statuses.get(task.prompt, 'pending') in ('pending', 'failed', 'running')]
        return {'selection': selection, 'query_count': len(tasks),
                'sample': [{'query': task.prompt, 'industry': task.sector, 'state': task.state,
                            'locality': task.locality} for task in tasks[:30]],
                'location_notice': 'Search areas are not geographic boundaries; Google Maps may return nearby businesses.'}
    finally:
        if conn:
            conn.close()


def keyword_tokens(keyword: str) -> list[str]:
    generic = {'commercial', 'service', 'services', 'contractor', 'contractors', 'company',
               'companies', 'specialist', 'specialists', 'supplier', 'suppliers', 'in', 'and', 'for', 'the'}
    return [token for token in re.findall(r'\w+', keyword.lower()) if len(token) >= 3 and token not in generic]


def custom_keyword_matches(keyword: str, name: str, category: str) -> bool:
    words = re.findall(r'\w+', (name + ' ' + category).lower())
    return any(word.startswith(token[:5]) for token in keyword_tokens(keyword) for word in words)
