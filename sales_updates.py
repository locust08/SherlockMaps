"""Validated, optimistic-concurrency sales edits using the existing activity log."""
import math
from datetime import datetime

from lead_intelligence_v4 import SALES_STATUSES, update_sales_lead


class StaleSalesEdit(ValueError):
    pass


def save_sales_update(conn, payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a sales update object')
    try:
        organization_id = int(payload['organization_id'])
    except (KeyError, ValueError, TypeError):
        raise ValueError('Choose a valid organization')
    status = payload.get('status')
    if status not in SALES_STATUSES:
        raise ValueError('Choose a valid sales status')
    record_contact = payload.get('record_contact', False)
    if not isinstance(record_contact, bool):
        raise ValueError('Record contact must be true or false')
    fields = {'organization_id': organization_id, 'status': status, 'record_contact': record_contact}
    for key, limit in (('assigned_pic', 120), ('notes', 2000), ('call_outcome', 240),
                       ('lost_reason', 500), ('next_action_at', 80)):
        if key in payload:
            value = payload[key]
            if not isinstance(value, str) or len(value) > limit or '\x00' in value:
                raise ValueError(f'{key.replace("_", " ")} must be text of at most {limit} characters')
            fields[key] = value.strip()
    if fields.get('next_action_at'):
        try:
            date = datetime.fromisoformat(fields['next_action_at'])
            if date.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError('Follow-up time must include a timezone')
    if 'proposal_value' in payload:
        try:
            value = float(payload['proposal_value']) if payload['proposal_value'] != '' else None
        except (ValueError, TypeError):
            raise ValueError('Proposal value must be a non-negative amount in MYR')
        if value is not None and (not math.isfinite(value) or value < 0 or value > 1e12):
            raise ValueError('Proposal value must be a finite amount between 0 and 1 trillion MYR')
        fields['proposal_value'] = value
    if not isinstance(payload.get('expected_updated_at'), str) or not payload['expected_updated_at']:
        raise ValueError('Reload the lead before saving')
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT updated_at FROM sales_leads WHERE organization_id=?',
                           (organization_id,)).fetchone()
        if not row:
            raise ValueError('Unknown organization')
        if row[0] != payload['expected_updated_at']:
            raise StaleSalesEdit('This lead changed since you opened it. Reload before editing again.')
        suppressed = conn.execute('SELECT 1 FROM contact_suppression WHERE organization_id=?',
                                  (organization_id,)).fetchone()
        if (suppressed or status == 'DO_NOT_CONTACT') and (status != 'DO_NOT_CONTACT' or record_contact):
            raise ValueError('Do-not-contact leads cannot be reactivated or logged as contacted here')
        update_sales_lead(conn, fields, actor=fields.get('assigned_pic') or 'Dashboard user', replace_proposal_value=True)
        return {'organization_id': organization_id, 'status': status,
                'updated_at': conn.execute('SELECT updated_at FROM sales_leads WHERE organization_id=?',
                                           (organization_id,)).fetchone()[0]}
    except Exception:
        conn.rollback()
        raise
