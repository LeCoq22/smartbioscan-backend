"""Run once as a job, or every minute from the API lifespan. Never starts charges.

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
from api.billing import handle_payment, handle_managed_invoice, mp, reconcile_account, schema_ready
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


async def recover_replacement_invoice(db, order):
    """The new contract is not the account's current ID until its first charge."""
    provider=order['renewal_preapproval_id']
    offset=0
    while True:
        query=urlencode({'preapproval_id':provider,'offset':offset})
        page=await mp('GET','/authorized_payments/search?'+query)
        invoices=page.get('results') or []
        for item in invoices:
            if item.get('preapproval_id')!=provider or (item.get('payment') or {}).get('status')!='approved':
                continue
            invoice=await mp('GET','/authorized_payments/'+str(item['id']))
            if invoice.get('preapproval_id')!=provider:
                raise RuntimeError('Replacement invoice ownership mismatch')
            await handle_managed_invoice(db,invoice,await mp('GET','/preapproval/'+provider))
        offset+=len(invoices)
        if not invoices or offset >= (page.get('paging') or {}).get('total',offset):
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
    replacements=db.client.table('billing_orders').select('*').eq('status','scheduled').eq('provider_synced',True).execute().data
    for order in replacements:
        if not order.get('renewal_preapproval_id'):
            continue
        try:
            await recover_replacement_invoice(db,order)
        except Exception:
            failed+=1
            logging.exception('Replacement invoice recovery failed for order %s',order['id'])
    renewals = await reconcile(apply=True)
    print(json.dumps({'billing_errors': failed, 'renewals': renewals}), flush=True)
    return int(failed > 0 or bool(renewals['errors']))


async def run_forever():
    # The SDK and database client have synchronous calls. Keep reconciliation
    # off the request event loop; each pass finishes before the next can start.
    while True:
        try:
            result = await asyncio.to_thread(lambda: asyncio.run(main()))
            if result:
                logging.error('Billing reconciliation finished with errors; retrying next pass')
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('Billing reconciliation pass failed; retrying next pass')
        await asyncio.sleep(60)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
