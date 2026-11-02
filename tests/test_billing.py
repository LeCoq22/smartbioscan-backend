from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from billing_domain import pack_price, change_mode, new_end, cycle_bounds
from api import billing


@pytest.mark.parametrize('paid,months,reports,quantity,expected',[
    ('28500',1,30,10,'9500.00'),('65000',1,100,10,'6500.00'),
    ('142500',6,30,10,'7916.67'),('325000',6,100,10,'5416.67'),
    ('24500',1,30,10,'8166.67')])
def test_packs_use_price_actually_paid(paid,months,reports,quantity,expected):
    assert pack_price(paid,months,reports,quantity)==Decimal(expected)


@pytest.mark.parametrize('paid',['0','-1','NaN','Infinity'])
def test_price_must_be_known_and_finite(paid):
    with pytest.raises(ValueError): pack_price(paid,1,30,10)


def test_plan_change_timing_and_dates():
    basic={'id':'basic','max_reports_month':30}; plus={'id':'plus','max_reports_month':100}
    assert change_mode(basic,plus)=='exhaustion'
    assert change_mode(plus,basic)=='expiry'
    assert new_end(date(2026,10,15),date(2026,10,30),1,'activation')==date(2026,11,14)
    assert new_end(date(2026,10,15),date(2026,10,30),1,'extend_expiry')==date(2026,11,29)
    assert new_end(date(2026,8,31),date(2026,8,31),6)==date(2027,2,28)


def test_managed_monthly_cycle_is_30_days_not_calendar_month():
    n={'subscription_start':'2026-01-31','subscription_end':'2026-03-02',
       'subscription_type':'monthly','billing_current_plan':{'id':'basic'}}
    assert cycle_bounds(n,date(2026,3,1))==(date(2026,1,31),date(2026,3,2))


def test_feature_disabled_never_creates_checkout(monkeypatch):
    monkeypatch.setenv('BILLING_MANAGEMENT_ENABLED','false')
    app=FastAPI(); billing.register_routes(app,lambda:'owner'); client=TestClient(app)
    assert client.get('/api/billing/summary').json()=={'enabled':False}
    assert client.post('/api/billing/orders',json={'request_id':'00000000-0000-0000-0000-000000000001','reports':10}).status_code==503


@pytest.mark.asyncio
async def test_upgrade_pauses_provider_before_marking_paid_ready(monkeypatch):
    db=MagicMock(); calls=[]
    async def provider(method,path,payload=None):
        calls.append((method,payload))
        return {'status':'authorized' if len(calls)==1 else 'paused'}
    monkeypatch.setattr(billing,'mp',provider)
    rpc=MagicMock(); monkeypatch.setattr(billing,'rpc',rpc)
    order={'id':'order','kind':'change','mode':'exhaustion','status':'paid','preapproval_id':'mp','nutri_id':'owner'}
    db.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data=order
    await billing.sync_order(db,order)
    assert calls==[('GET',None),('PUT',{'status':'paused'}),('GET',None)]
    assert [c.args[1] for c in rpc.call_args_list]==['billing_pause_confirmed','billing_tick']


@pytest.mark.asyncio
async def test_provider_failure_does_not_mark_sync_complete(monkeypatch):
    monkeypatch.setattr(billing,'mp',AsyncMock(side_effect=RuntimeError('network')))
    rpc=MagicMock(); monkeypatch.setattr(billing,'rpc',rpc)
    order={'id':'order','kind':'change','mode':'exhaustion','status':'syncing','preapproval_id':'mp','nutri_id':'owner','renewal_preapproval_id':'new',
           'target_plan':{'title':'Plus','unit_price':65000,'months':1},'new_end':'2026-11-14'}
    with pytest.raises(RuntimeError): await billing.sync_order(MagicMock(),order)
    rpc.assert_not_called()


def replacement_order(status='syncing'):
    return {'id':'order','kind':'change','mode':'exhaustion','status':status,
            'preapproval_id':'old','renewal_preapproval_id':'new','nutri_id':'owner',
            'target_plan':{'title':'Plus','unit_price':65000,'months':1},
            'new_end':'2030-12-16','previous_end':'2030-11-16','provider_synced':False}


def replacement_remote(status='authorized'):
    return {'id':'new','external_reference':'billing-sub:order','status':status,
            'next_payment_date':'2030-12-16T19:07:50.000-04:00',
            'auto_recurring':{'transaction_amount':65000,'currency_id':'ARS',
                              'frequency':30,'frequency_type':'days'}}


@pytest.mark.asyncio
async def test_authorized_replacement_closes_old_contract_before_switch(monkeypatch):
    calls=[]
    remote=replacement_remote()
    old={'status':'paused'}
    async def provider(method,path,payload=None):
        calls.append((method,path,payload))
        if path.endswith('/new'): return remote
        if method=='PUT': old.update(payload)
        return old
    monkeypatch.setattr(billing,'mp',provider)
    confirm=MagicMock(); monkeypatch.setattr(billing,'rpc',confirm)
    await billing.sync_order(MagicMock(),replacement_order())
    assert [c for c in calls if c[0]=='PUT']==[('PUT','/preapproval/old',{'status':'cancelled'})]
    assert confirm.call_count==1 and confirm.call_args.args[1]=='billing_sync_confirmed'


@pytest.mark.asyncio
async def test_wrong_replacement_schedule_is_paused_not_accepted(monkeypatch):
    # Actual sandbox behavior: changing amount succeeded while date/frequency
    # stayed on the original schedule. No PUT of those fields is allowed now.
    remote=replacement_remote(); remote['auto_recurring']['frequency']=1
    remote['auto_recurring']['frequency_type']='months'
    remote['next_payment_date']='2030-11-03T19:00:42.000-04:00'
    calls=[]
    async def provider(method,path,payload=None):
        calls.append((method,path,payload))
        if method=='PUT': remote.update(payload)
        return remote
    monkeypatch.setattr(billing,'mp',provider)
    confirm=MagicMock(); monkeypatch.setattr(billing,'rpc',confirm)
    with pytest.raises(billing.HTTPException) as error:
        await billing.sync_order(MagicMock(),replacement_order())
    assert error.value.status_code==409
    assert [c for c in calls if c[0]=='PUT']==[('PUT','/preapproval/new',{'status':'paused'})]
    confirm.assert_not_called()


@pytest.mark.asyncio
async def test_pending_replacement_cannot_close_old_or_complete_change(monkeypatch):
    provider=AsyncMock(return_value=replacement_remote('pending'))
    monkeypatch.setattr(billing,'mp',provider)
    confirm=MagicMock(); monkeypatch.setattr(billing,'rpc',confirm)
    await billing.sync_order(MagicMock(),replacement_order())
    assert all(c.args[0]=='GET' for c in provider.call_args_list)
    confirm.assert_not_called()


@pytest.mark.asyncio
async def test_new_contract_is_pending_without_card_or_authorization(monkeypatch):
    monkeypatch.setenv('FRONTEND_URL','https://app.example.com')
    order=replacement_order(); order.pop('renewal_preapproval_id')
    remote={**replacement_remote('pending'),'init_point':'https://www.mercadopago.com.ar/test'}
    provider=AsyncMock(return_value=remote); monkeypatch.setattr(billing,'mp',provider)
    db=MagicMock(); db.get_nutri.return_value={'email':'buyer@example.com'}
    await billing.renewal_contract(db,order,'2030-12-16')
    payload=provider.call_args.args[2]
    assert payload['status']=='pending' and 'card_token_id' not in payload
    assert payload['auto_recurring']['start_date']=='2030-12-16T12:00:00.000-03:00'
    assert payload['auto_recurring']['frequency']==30
    assert order['renewal_preapproval_id']=='new'


def test_provider_dates_are_compared_in_argentina():
    assert billing.provider_day('2030-12-15T23:30:00-04:00')=='2030-12-16'
    assert billing.provider_day('2030-12-16T00:30:00+01:00')=='2030-12-15'


@pytest.mark.asyncio
async def test_no_catalog_fallback_without_a_paid_receipt(monkeypatch):
    monkeypatch.setattr(billing,'mp',AsyncMock(return_value={'results':[]}))
    with pytest.raises(billing.HTTPException,match='409'):
        await billing.verified_price({'mp_preapproval_id':'mp','subscription_start':'2026-10-01','subscription_end':'2026-10-31'})


@pytest.mark.asyncio
async def test_pending_downgrade_does_not_repeat_provider_changes(monkeypatch):
    provider=AsyncMock(); monkeypatch.setattr(billing,'mp',provider)
    await billing.sync_order(MagicMock(),{'kind':'change','status':'scheduled','provider_synced':True})
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_managed_paid_plan_preserves_its_actual_price(monkeypatch):
    provider=AsyncMock(); monkeypatch.setattr(billing,'mp',provider)
    assert await billing.verified_price({'billing_current_plan':{'id':'plan'},'billing_paid_price':24500})=='24500'
    provider.assert_not_called()


@pytest.mark.parametrize('quantity',[0,1,9,11,20,30,-10])
def test_only_ten_report_packs(quantity):
    with pytest.raises(ValueError): pack_price(28500,1,30,quantity)

@pytest.mark.asyncio
async def test_recurring_payment_routes_through_verified_invoice(monkeypatch):
    from api import subscriptions
    payment={'id':185855908039,'status':'approved'}
    invoice={'id':7033427997,'preapproval_id':'preapproval','payment':payment}
    response=MagicMock(status_code=200); response.json.return_value={'results':[invoice]}
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    handler=AsyncMock(return_value={'ok':True,'detail':'renewal_extended'})
    monkeypatch.setattr(subscriptions,'handle_authorized_payment',handler)
    sdk=MagicMock()
    assert (await subscriptions.handle_recurring_payment(sdk,payment))['ok']
    handler.assert_awaited_once_with(sdk,'7033427997')
    assert client.get.call_args.kwargs['params']=={'payment_id':'185855908039'}

@pytest.mark.asyncio
async def test_unsearchable_invoice_requests_retry(monkeypatch):
    from api import subscriptions
    response=MagicMock(status_code=200); response.json.return_value={'results':[]}
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    with pytest.raises(billing.HTTPException) as exc:
        await subscriptions.handle_recurring_payment(MagicMock(),{'id':1,'status':'approved'})
    assert exc.value.status_code==503

@pytest.mark.asyncio
async def test_provider_outage_requests_retry(monkeypatch):
    from api import subscriptions
    response=MagicMock(status_code=503, text='unavailable')
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    with pytest.raises(billing.HTTPException) as exc:
        await subscriptions.handle_authorized_payment(MagicMock(),'invoice')
    assert exc.value.status_code==502


def test_confirmed_policy_preserves_prepaid_days_by_default():
    assert new_end(date(2026,10,15),date(2026,10,30),1)==date(2026,11,29)
    assert new_end(date(2026,10,15),date(2026,12,30),6)==date(2027,6,30)

def test_extended_monthly_contract_still_refreshes_quota_every_thirty_days():
    n={'subscription_start':'2026-10-15','subscription_end':'2026-11-29',
       'subscription_type':'monthly','billing_current_plan':{'id':'plus'}}
    assert cycle_bounds(n,date(2026,11,13))==(date(2026,10,15),date(2026,11,14))
    assert cycle_bounds(n,date(2026,11,14))==(date(2026,11,14),date(2026,11,29))

def test_old_activation_configuration_cannot_discard_paid_days(monkeypatch):
    monkeypatch.setenv('BILLING_MANAGEMENT_ENABLED','true')
    monkeypatch.setenv('MP_WEBHOOK_SECRET','test')
    monkeypatch.setenv('BILLING_EXPIRY_POLICY','activation')
    with pytest.raises(billing.HTTPException): billing.require_enabled()


@pytest.mark.asyncio
async def test_scheduled_authorization_only_pauses_old_contract(monkeypatch):
    order=replacement_order('scheduled'); order['mode']='expiry'
    remote=replacement_remote(); remote['next_payment_date']='2030-11-16T12:00:00-03:00'
    old={'status':'authorized'}; calls=[]
    async def provider(method,path,payload=None):
        calls.append((method,path,payload))
        if path.endswith('/new'): return remote
        if method=='PUT': old.update(payload)
        return old
    monkeypatch.setattr(billing,'mp',provider)
    confirm=MagicMock(); monkeypatch.setattr(billing,'rpc',confirm)
    db=MagicMock(); db.get_nutri.return_value={'subscription_end':'2030-11-16'}
    await billing.sync_order(db,order)
    assert [c for c in calls if c[0]=='PUT']==[('PUT','/preapproval/old',{'status':'paused'})]
    assert [c.args[1] for c in confirm.call_args_list]==['billing_sync_confirmed']


@pytest.mark.asyncio
async def test_cancel_pending_replacement_before_restoring_old(monkeypatch):
    order=replacement_order('cancelling'); order['mode']='expiry'
    before={'status':'authorized','next_payment_date':'2030-11-16T12:00:00-03:00',
            'auto_recurring':{'transaction_amount':28500}}
    order['provider_before']=before
    remotes={'new':replacement_remote(),'old':{**before,'status':'paused'}}
    calls=[]
    async def provider(method,path,payload=None):
        calls.append((method,path,payload))
        remote=remotes[path.split('/')[-1]]
        if method=='PUT': remote.update(payload)
        return remote
    monkeypatch.setattr(billing,'mp',provider)
    await billing.sync_order(MagicMock(),order)
    assert [c for c in calls if c[0]=='PUT']==[
        ('PUT','/preapproval/new',{'status':'cancelled'}),
        ('PUT','/preapproval/old',{'status':'authorized'})]


@pytest.mark.asyncio
async def test_wrong_order_contract_is_not_persisted(monkeypatch):
    monkeypatch.setenv('FRONTEND_URL','https://app.example.com')
    order=replacement_order(); order.pop('renewal_preapproval_id')
    remote={**replacement_remote('pending'),'external_reference':'billing-sub:other','init_point':'https://www.mercadopago.com.ar/test'}
    monkeypatch.setattr(billing,'mp',AsyncMock(return_value=remote))
    db=MagicMock(); db.get_nutri.return_value={'email':'buyer@example.com'}
    with pytest.raises(billing.HTTPException):
        await billing.renewal_contract(db,order,'2030-12-16')
    db.client.table.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_loop_recovers_from_failure(monkeypatch):
    import asyncio
    from scripts import reconcile_billing as worker
    run=AsyncMock(side_effect=[RuntimeError('temporary'), 0])
    sleep=AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(worker.asyncio,'to_thread',run)
    monkeypatch.setattr(worker.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_forever()
    assert run.await_count==2
    assert sleep.call_args_list[0].args==(60,)


@pytest.mark.asyncio
async def test_invoice_recovery_uses_provider_default_pagination(monkeypatch):
    from scripts import reconcile_mp_subscriptions as worker
    db=MagicMock()
    db.client.table.return_value.select.return_value.not_.is_.return_value.lte.return_value.execute.return_value.data=[{
        'id':'owner','mp_preapproval_id':'old','subscription_next_billing_date':'2030-11-16T00:00:00Z'}]
    monkeypatch.setattr(worker,'DB',lambda:db)
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    pages=[{'results':[{'id':'a','preapproval_id':'other','payment':{'status':'approved'},'debit_date':'2030-11-16'}], 'paging':{'total':2}},
           {'results':[{'id':'b','preapproval_id':'old','payment':{'status':'approved'},'debit_date':'2030-11-16'}], 'paging':{'total':2}}]
    responses=[]
    for page in pages:
        response=MagicMock(); response.json.return_value=page; responses.append(response)
    client=MagicMock(); client.get=AsyncMock(side_effect=responses)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setattr(worker.httpx,'AsyncClient',lambda **kw:context)
    handler=AsyncMock(return_value={'ok':True}); monkeypatch.setattr(worker,'handle_authorized_payment',handler)
    result=await worker.reconcile(apply=True)
    assert result['errors']==[] and result['processed']==1
    assert [c.kwargs['params'] for c in client.get.call_args_list]==[
        {'preapproval_id':'old','offset':0},{'preapproval_id':'old','offset':1}]
    assert handler.call_args.args[1]=='b'
