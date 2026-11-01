"""Run every minute as a separate worker. Never creates checkouts or charges.

Recover lost payment notifications, retry provider synchronization, and reconcile
recurring renewals through the same verified handlers used by webhooks.
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()
from db import DB
from api.billing import handle_payment, mp, reconcile_account, schema_ready
from scripts.reconcile_mp_subscriptions import reconcile


async def recover_checkout(db, order):
    offset = 0
    while True:
        query = urlencode({'external_reference': 'billing:' + order['id'], 'limit': 100, 'offset': offset})
        page = await mp('GET', '/v1/payments/search?' + query)
        rows = page.get('results') or []
        for item in rows:
            # Fetch the resource itself instead of trusting a stale search snapshot.
            payment = await mp('GET', f"/v1/payments/{item['id']}")
            if payment.get('external_reference') == 'billing:' + order['id']:
                await handle_payment(payment)
        offset += len(rows)
        if not rows or offset >= (page.get('paging') or {}).get('total', offset):
            break


async def main():
    if not schema_ready():
        raise RuntimeError('BILLING_SCHEMA_READY must be true for the billing worker')
    db = DB()
    failed = 0
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    orders = db.client.table('billing_orders').select('id,status,review_reason').in_(
        'status', ['checkout', 'cancelled']).gte('created_at', since).execute().data
    for order in orders:
        if order['status'] == 'cancelled' and order.get('review_reason') != 'checkout_expired':
            continue
        try:
            await recover_checkout(db, order)
        except Exception:
            failed += 1
            logging.exception('Billing payment recovery failed for order %s', order['id'])
    rows = db.client.table('billing_orders').select('nutri_id').in_(
        'status', ['paid', 'scheduled', 'syncing', 'cancelling']).execute().data
    for nutri_id in sorted({r['nutri_id'] for r in rows}):
        try:
            await reconcile_account(db, nutri_id)
        except Exception:
            failed += 1
            logging.exception('Billing reconciliation failed for account %s', nutri_id)
    renewals = await reconcile(apply=True)
    print(json.dumps({'billing_errors': failed, 'renewals': renewals}))
    return int(failed > 0 or bool(renewals['errors']))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
