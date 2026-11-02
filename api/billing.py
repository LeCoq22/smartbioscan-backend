"""Dashboard billing. Disabled until migration + provider sandbox checks pass.

Only provider-verified payments can grant credits. Database RPCs serialize grants;
provider updates are retried by the reconciliation worker before activation.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from billing_domain import cycle_bounds, pack_price, change_mode, new_end
from api.subscriptions import PLANS


def enabled():
    return os.getenv('BILLING_MANAGEMENT_ENABLED','false').lower()=='true'


def require_enabled():
    if not enabled():
        raise HTTPException(503,'La gestión de planes todavía no está habilitada')
    if not os.getenv('MP_WEBHOOK_SECRET') or os.getenv('BILLING_EXPIRY_POLICY','extend_expiry') != 'extend_expiry':
        raise HTTPException(503,'La configuración de cobros todavía no está completa')


def rpc(db,name,**args):
    result=db.client.rpc(name,{f'p_{k}':v for k,v in args.items()}).execute().data
    if isinstance(result,dict) and result.get('ok') is False:
        raise HTTPException(409, result.get('reason','La operación cambió; actualizá el panel'))
    return result


async def mp(method,path,payload=None):
    token=os.getenv('MP_ACCESS_TOKEN')
    if not token:
        raise HTTPException(503,'Pagos no configurados')
    async with httpx.AsyncClient(timeout=30) as client:
        response=await client.request(method,'https://api.mercadopago.com'+path,
            headers={'Authorization':f'Bearer {token}'},json=payload)
    if response.status_code not in (200,201):
        raise HTTPException(502,'No se pudo confirmar la operación con Mercado Pago; podés reintentar')
    return response.json()


def account(db,nutri_id):
    n=db.get_nutri(nutri_id)
    if not n or n.get('is_suspended') or n.get('subscription_status')!='active':
        raise HTTPException(409,'Necesitás una cuenta activa')
    if not n.get('mp_preapproval_id') or n.get('subscription_type') not in ('monthly','semestral'):
        raise HTTPException(409,'Esta cuenta no tiene un plan de pago automático compatible')
    if not n.get('subscription_end') or str(n['subscription_end'])[:10] <= datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date().isoformat():
        raise HTTPException(409,'El período está vencido; renová antes de comprar reportes extra')
    return n


def plan_for(n):
    if n.get('billing_current_plan'):
        return n['billing_current_plan']
    suffix='semestral' if n['subscription_type']=='semestral' else 'mensual'
    tier={30:'basico',100:'plus'}.get(n['max_reports_month'])
    if not tier:
        raise HTTPException(409,'El cupo personalizado requiere revisión antes de cambiar de plan')
    key=f'bioscan_{tier}_{suffix}'
    return {'id':key,**PLANS[key]}


def pending(db,nutri_id):
    rows=db.client.table('billing_orders').select('*').eq('nutri_id',nutri_id).neq('kind','pack').in_(
        'status',['checkout','paid','scheduled','syncing','cancelling']).order('created_at').execute().data
    return rows[0] if rows else None


async def verified_price(n):
    if n.get('billing_current_plan') and n.get('billing_paid_price'):
        return str(n['billing_paid_price'])
    # Historical paid amount, never the current catalog or browser-supplied price.
    data=await mp('GET',f"/authorized_payments/search?preapproval_id={n['mp_preapproval_id']}")
    invoices=data.get('results',[])
    start=str(n['subscription_start'])[:10]
    end=str(n['subscription_end'])[:10]
    approved=[i for i in invoices if i.get('preapproval_id')==n['mp_preapproval_id']
              and (i.get('payment') or {}).get('status')=='approved'
              and start <= str(i.get('debit_date') or '')[:10] < end]
    if not approved:
        raise HTTPException(409,'No encontramos el precio efectivamente pagado del período; requiere conciliación')
    latest=max(approved,key=lambda i:i.get('debit_date',''))
    payment_id=(latest.get('payment') or {}).get('id')
    if not payment_id:
        raise HTTPException(409,'Falta el comprobante del período')
    payment=await mp('GET',f'/v1/payments/{payment_id}')
    if payment.get('status')!='approved' or payment.get('currency_id')!='ARS' or payment.get('transaction_amount_refunded',0):
        raise HTTPException(409,'El comprobante del período no es válido')
    return str(payment['transaction_amount'])


def provider_day(value):
    """MP timestamps can use -04:00 even when the contract is in Argentina."""
    if not value:
        return None
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise HTTPException(502,'Mercado Pago devolvió una fecha sin zona horaria')
    return parsed.astimezone(ZoneInfo('America/Argentina/Buenos_Aires')).date().isoformat()


def renewal_matches(remote, target, due):
    recurring=remote.get('auto_recurring') or {}
    return (provider_day(remote.get('next_payment_date'))==due
        and Decimal(str(recurring.get('transaction_amount',0)))==Decimal(str(target['unit_price']))
        and recurring.get('currency_id')=='ARS'
        and recurring.get('frequency')==(30 if target['months']==1 else 6)
        and recurring.get('frequency_type')==('days' if target['months']==1 else 'months'))


async def set_provider_status(provider, status):
    remote=await mp('GET',f'/preapproval/{provider}')
    if remote.get('status')!=status:
        await mp('PUT',f'/preapproval/{provider}',{'status':status})
        remote=await mp('GET',f'/preapproval/{provider}')
    if remote.get('status')!=status:
        raise HTTPException(502,'Mercado Pago todavía no confirmó el estado de la suscripción')
    return remote


async def renewal_contract(db, order, due):
    """A new hosted authorization, never an unsupported schedule update.

    Creating a pending contract cannot charge or authorize the buyer. A lost
    creation response may leave an unused pending contract, but its checkout is
    never returned unless its ID has been persisted successfully.
    """
    renewal_id=order.get('renewal_preapproval_id')
    if renewal_id:
        remote=await mp('GET',f'/preapproval/{renewal_id}')
    else:
        today=datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date().isoformat()
        if due<=today:
            raise HTTPException(409,'Venció el plazo para autorizar este cambio; requiere conciliación')
        n=db.get_nutri(order['nutri_id'])
        target=order['target_plan']
        remote=await mp('POST','/preapproval',{
            'reason':target['title'], 'external_reference':'billing-sub:'+order['id'],
            'payer_email':n['email'], 'status':'pending',
            'back_url':os.environ['FRONTEND_URL'].rstrip('/')+'/?billing=return',
            'auto_recurring':{'transaction_amount':float(target['unit_price']),
                'currency_id':'ARS','frequency':30 if target['months']==1 else 6,
                'frequency_type':'days' if target['months']==1 else 'months',
                'start_date':due+'T12:00:00.000-03:00'}})
        if remote.get('external_reference')!='billing-sub:'+order['id']:
            raise HTTPException(502,'La autorización no corresponde a este cambio')
        renewal_id=remote['id']
        values={'renewal_preapproval_id':renewal_id,'renewal_init_point':remote['init_point'],
                'renewal_due':due}
        db.client.table('billing_orders').update(values).eq('id',order['id']).execute()
        order.update(values)
    if remote.get('external_reference')!='billing-sub:'+order['id']:
        raise HTTPException(502,'La autorización no corresponde a este cambio')
    return remote


async def sync_order(db,order):
    """Pause the old debit, verify a buyer-authorized replacement, then switch."""
    if order['kind']=='pack' or order['status'] in ('checkout','review','cancelled') or (order['status']=='scheduled' and order.get('provider_synced')):
        return
    provider=order['preapproval_id']
    if order['status']=='cancelling':
        if order.get('renewal_preapproval_id'):
            await set_provider_status(order['renewal_preapproval_id'],'cancelled')
        before=order['provider_before']
        check=await set_provider_status(provider,before['status'])
        if (provider_day(check.get('next_payment_date'))!=provider_day(before['next_payment_date'])
            or Decimal(str((check.get('auto_recurring') or {}).get('transaction_amount',0)))!=Decimal(str(before['auto_recurring']['transaction_amount']))):
            raise HTTPException(502,'La restauración del plan anterior requiere conciliación')
        db.client.table('billing_orders').update({'status':'cancelled'}).eq('id',order['id']).eq('status','cancelling').execute()
        return
    if order['mode']=='exhaustion' and order['status']=='paid':
        await set_provider_status(provider,'paused')
        rpc(db,'billing_pause_confirmed',order_id=order['id'])
        rpc(db,'billing_tick',nutri_id=order['nutri_id'])
        order=db.client.table('billing_orders').select('*').eq('id',order['id']).single().execute().data
        if order['status']=='paid':
            return
    if order['status'] not in ('scheduled','syncing'):
        return
    if order['status']=='scheduled' and order.get('provider_synced'):
        return  # The next invoice advances the provider date; recover it separately.
    due=order['new_end'] if order['status']=='syncing' else order['previous_end']
    remote=await renewal_contract(db,order,due)
    if remote.get('status')=='pending':
        if not renewal_matches(remote,order['target_plan'],due):
            raise HTTPException(409,'La fecha del nuevo débito requiere revisión antes de autorizarlo')
        return
    if remote.get('status')!='authorized':
        raise HTTPException(409,'La nueva autorización no está activa; contactá a soporte')
    if not renewal_matches(remote,order['target_plan'],due):
        # Do not leave an authorized replacement able to debit earlier than paid access.
        await set_provider_status(order['renewal_preapproval_id'],'paused')
        raise HTTPException(409,'Se pausó el nuevo débito porque su fecha o importe requiere revisión')
    if order['status']=='scheduled':
        if not order.get('provider_synced'):
            n=db.get_nutri(order['nutri_id'])
            if str(n.get('subscription_end'))[:10]!=order['previous_end']:
                await set_provider_status(order['renewal_preapproval_id'],'paused')
                raise HTTPException(409,'El plan anterior se renovó durante el cambio; requiere conciliación')
            await set_provider_status(provider,'paused')
            rpc(db,'billing_sync_confirmed',order_id=order['id'])
        return
    await set_provider_status(provider,'cancelled')
    rpc(db,'billing_sync_confirmed',order_id=order['id'])


async def reconcile_account(db,nutri_id):
    token=str(uuid4())
    claimed=db.client.rpc('billing_claim_sync',{'p_nutri_id':nutri_id,'p_token':token}).execute().data
    if not claimed:
        return
    try:
        rpc(db,'billing_tick',nutri_id=nutri_id)
        order=pending(db,nutri_id)
        if order:
            await sync_order(db,order)
    finally:
        db.client.rpc('billing_release_sync',{'p_nutri_id':nutri_id,'p_token':token}).execute()


class Purchase(BaseModel):
    request_id: UUID
    plan_id: Optional[str] = None
    reports: Optional[int] = None


def register_routes(app,get_current_nutri):
    router=APIRouter(prefix='/api/billing',tags=['billing'])

    @router.get('/summary')
    async def summary(nutri_id: str=Depends(get_current_nutri)):
        if not enabled():
            return {'enabled':False}
        from db import DB
        db=DB()
        await reconcile_account(db,nutri_id)
        n=account(db,nutri_id)
        current=plan_for(n)
        order=pending(db,nutri_id)
        paid=order['paid_price'] if order else await verified_price(n)
        cycle_start,cycle_end=cycle_bounds(n,datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date())
        quota=db.can_generate_report(nutri_id)
        reviews=db.client.table('billing_orders').select('review_reason').eq('nutri_id',nutri_id).in_('status',['review','applied']).order('created_at',desc=True).limit(20).execute().data
        return {'enabled':True,'current_plan':current,'subscription_end':n['subscription_end'],
                'cycle_end':cycle_end.isoformat(),'quota':quota,
                'expiry_policy':os.getenv('BILLING_EXPIRY_POLICY','extend_expiry'),
                'plans':[{'id':key,**value} for key,value in PLANS.items()],
                'packs':[{'reports':q,'amount':str(pack_price(paid,current['months'],current['max_reports_month'],q))} for q in (10,)],
                'pending':order,'paid_price':paid,'review_required':any(r.get('review_reason') for r in reviews)}

    @router.post('/orders')
    async def purchase(body:Purchase,nutri_id:str=Depends(get_current_nutri)):
        require_enabled()
        from db import DB
        db=DB()
        # Reuse the exact original order on network retries (including its quote).
        existing=db.client.table('billing_orders').select('*').eq('id',str(body.request_id)).eq('nutri_id',nutri_id).execute().data
        if existing:
            order=existing[0]
            if order.get('requested_plan')!=body.plan_id or order.get('reports')!=(body.reports or 0):
                raise HTTPException(409,'La clave de la compra ya fue usada para otra operación')
        else:
            await reconcile_account(db,nutri_id)
            n=account(db,nutri_id)
            current=plan_for(n)
            if (body.plan_id is None)==(body.reports is None):
                raise HTTPException(422,'Elegí un plan o un paquete')
            queued=pending(db,nutri_id)
            paid=queued['paid_price'] if queued else await verified_price(n)
            cs,ce=cycle_bounds(n,datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date())
            if body.reports is not None:
                try:
                    amount=pack_price(paid,current['months'],current['max_reports_month'],body.reports)
                except ValueError as exc:
                    raise HTTPException(422,str(exc)) from exc
                target=None; mode='cycle'; kind='pack'
            else:
                if body.plan_id not in PLANS:
                    raise HTTPException(422,'Plan desconocido')
                target={'id':body.plan_id,**PLANS[body.plan_id]}
                try:
                    mode=change_mode(current,target)
                except ValueError as exc:
                    raise HTTPException(409,str(exc)) from exc
                kind='change'; amount=Decimal(str(target['unit_price']))
            before=None
            if kind=='change':
                remote=await mp('GET',f"/preapproval/{n['mp_preapproval_id']}")
                recurring=remote.get('auto_recurring') or {}
                if remote.get('status')!='authorized' or not remote.get('next_payment_date'):
                    raise HTTPException(409,'La suscripción requiere conciliación antes de cambiar de plan')
                before={k:remote[k] for k in ('reason','status','next_payment_date')}
                before['auto_recurring']={k:recurring[k] for k in ('frequency','frequency_type','transaction_amount','currency_id')}
            order=rpc(db,'billing_create_order',order={
                'id':str(body.request_id),'nutri_id':nutri_id,'kind':kind,'mode':mode,'provider_before':before,
                'requested_plan':body.plan_id,'reports':body.reports or 0,
                'amount':str(amount),'paid_price':paid,'target_plan':target,
                'current_plan':current,'preapproval_id':n['mp_preapproval_id'],
                'previous_end':str(n['subscription_end'])[:10],
                'cycle_start':cs.isoformat(),'cycle_end':ce.isoformat(),
                'expiry_policy':os.getenv('BILLING_EXPIRY_POLICY','extend_expiry')})
        if order['status']=='scheduled':
            await reconcile_account(db,nutri_id)
            order=db.client.table('billing_orders').select('*').eq('id',order['id']).single().execute().data
            return {'order_id':order['id'],'status':'scheduled','init_point':order.get('renewal_init_point')}
        if order['status']!='checkout':
            return {'order_id':order['id'],'status':order['status']}
        if order.get('init_point'):
            return {'order_id':order['id'],'init_point':order['init_point'],'status':'checkout'}
        front=os.environ['FRONTEND_URL'].rstrip('/')
        back=os.environ['BACKEND_URL'].rstrip('/')
        result=await mp('POST','/checkout/preferences',{
            'items':[{'title':f"{order['reports']} reportes extra" if order['kind']=='pack' else order['target_plan']['title'],
                      'quantity':1,'unit_price':float(order['amount']),'currency_id':'ARS'}],
            'external_reference':'billing:'+order['id'],
            'notification_url':back+'/payments/webhook',
            'back_urls':{s:front+'/?billing=return' for s in ('success','pending','failure')},
            'auto_return':'approved','expires':True,
            'expiration_date_to':order['checkout_expires_at']})
        db.client.table('billing_orders').update({'init_point':result['init_point'],
            'preference_id':result['id']}).eq('id',order['id']).eq('status','checkout').execute()
        return {'order_id':order['id'],'init_point':result['init_point'],'status':'checkout'}

    @router.post('/orders/{order_id}/cancel')
    async def cancel_order(order_id:UUID,nutri_id:str=Depends(get_current_nutri)):
        require_enabled()
        from db import DB
        db=DB()
        result=rpc(db,'billing_cancel_order',order_id=str(order_id),nutri_id=nutri_id)
        await reconcile_account(db,nutri_id)
        return result

    @router.post('/orders/{order_id}/renewal')
    async def authorize_renewal(order_id:UUID,nutri_id:str=Depends(get_current_nutri)):
        require_enabled()
        from db import DB
        db=DB()
        rows=db.client.table('billing_orders').select('*').eq('id',str(order_id)).eq('nutri_id',nutri_id).execute().data
        if not rows:
            raise HTTPException(404,'Cambio no encontrado')
        if rows[0]['status'] not in ('scheduled','syncing'):
            raise HTTPException(409,'Este cambio no necesita una autorización pendiente')
        await reconcile_account(db,nutri_id)
        order=db.client.table('billing_orders').select('*').eq('id',str(order_id)).single().execute().data
        if order['provider_synced']:
            return {'status':order['status']}
        if not order.get('renewal_init_point'):
            raise HTTPException(503,'La autorización todavía se está preparando')
        return {'status':order['status'],'init_point':order['renewal_init_point']}

    app.include_router(router)


async def handle_payment(payment):
    reference=str(payment.get('external_reference') or '')
    if not reference.startswith('billing:'):
        return None
    # Process existing orders even if the UI feature flag has been disabled.
    from db import DB
    db=DB()
    try:
        order_id=str(UUID(reference.removeprefix('billing:')))
    except ValueError:
        raise HTTPException(400,'Referencia inválida')
    result=rpc(db,'billing_accept_payment',order_id=order_id,payment_id=str(payment['id']),
        amount=str(payment.get('transaction_amount',0)),currency=payment.get('currency_id',''),
        status=('partially_refunded' if payment.get('status')=='approved' and payment.get('transaction_amount_refunded',0) else payment.get('status','')),
        paid_at=payment.get('date_approved'))
    if result.get('nutri_id'):
        await reconcile_account(db,result['nutri_id'])
    return result


def schema_ready():
    return os.getenv('BILLING_SCHEMA_READY',str(enabled())).lower()=='true'


async def handle_replacement_preapproval(db, remote):
    reference=str(remote.get('external_reference') or '')
    if not reference.startswith('billing-sub:'):
        return None
    if not schema_ready():
        raise HTTPException(503,'El esquema de cobros todavía no está disponible')
    try: order_id=str(UUID(reference.removeprefix('billing-sub:')))
    except ValueError: raise HTTPException(400,'Referencia de autorización inválida')
    rows=db.client.table('billing_orders').select('*').eq('id',order_id).execute().data
    if not rows or rows[0].get('renewal_preapproval_id')!=remote.get('id'):
        raise HTTPException(503,'La autorización todavía no está asociada; reintentar')
    await reconcile_account(db,rows[0]['nutri_id'])
    return {'ok':True,'detail':'replacement_authorization_checked'}


def managed_preapproval(db,preapproval_id):
    if not schema_ready():
        return False
    for field in ('preapproval_id','renewal_preapproval_id'):
        rows=db.client.table('billing_orders').select('id').eq(field,preapproval_id).eq('kind','change').neq('status','checkout').neq('status','cancelled').limit(1).execute().data
        if rows: return True
    return False


async def handle_managed_invoice(db,invoice,preapproval):
    provider=invoice['preapproval_id']
    if not managed_preapproval(db,provider):
        return None
    replacement=db.client.table('billing_orders').select('*').eq('renewal_preapproval_id',provider).execute().data
    old_orders=db.client.table('billing_orders').select('*').eq('preapproval_id',provider).in_(
        'status',['paid','scheduled','syncing','cancelling','applied','review']).order('created_at',desc=True).limit(1).execute().data
    if not replacement:
        # While the buyer has not authorized the replacement, the old plan may
        # renew normally. After a pause/switch, an old invoice needs manual review.
        if old_orders and old_orders[0]['status']=='scheduled' and not old_orders[0]['provider_synced']:
            return None
        raise HTTPException(409,'Cobro del contrato anterior durante un cambio: requiere conciliación')
    order=replacement[0]
    if old_orders and (old_orders[0]['status'] in ('paid','syncing','cancelling') or
                       (old_orders[0]['status']=='scheduled' and old_orders[0]['provider_synced'])):
        raise HTTPException(409,'Cobro durante otro cambio de abono: requiere conciliación')
    payment_id=(invoice.get('payment') or {}).get('id')
    if not payment_id:
        raise HTTPException(502,'Factura sin comprobante de pago')
    payment=await mp('GET',f'/v1/payments/{payment_id}')
    if payment.get('status')!='approved':
        return {'ok':True,'detail':'payment_not_approved'}
    if payment.get('transaction_amount_refunded',0):
        raise HTTPException(409,'El pago de renovación tiene una devolución; requiere conciliación')
    if order['status']=='scheduled' and order['mode']=='expiry':
        if not order.get('provider_synced'):
            raise HTTPException(503,'La sustitución del débito anterior todavía no fue confirmada')
        result=rpc(db,'billing_accept_payment',order_id=order['id'],payment_id=str(payment['id']),
            amount=str(payment['transaction_amount']),currency=payment.get('currency_id',''),
            status=payment['status'],paid_at=payment.get('date_approved'))
        await reconcile_account(db,order['nutri_id'])
        return result
    if order['status']!='applied':
        raise HTTPException(409,'Cobro antes de completar el cambio: requiere conciliación')
    n=db.get_nutri(order['nutri_id'])
    if n.get('mp_preapproval_id')!=provider:
        raise HTTPException(409,'El cobro pertenece a un contrato reemplazado')
    return rpc(db,'billing_renew',nutri_id=n['id'],payment_id=str(payment['id']),
        amount=str(payment['transaction_amount']),currency=payment.get('currency_id',''),
        paid_at=payment.get('date_approved'),next_date=provider_day(preapproval.get('next_payment_date')))
