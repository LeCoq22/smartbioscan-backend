import { readFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
const { PGlite } = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite')
const db = new PGlite()
const owner='00000000-0000-0000-0000-000000000001',other='00000000-0000-0000-0000-000000000002'
let seq=20
const uuid=()=>`00000000-0000-0000-0000-${String(seq++).padStart(12,'0')}`
await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
create schema auth; create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('test.uid',true),'')::uuid$$;
grant usage on schema auth to authenticated,service_role;
create type subscription_type as enum ('monthly','semestral','free_trial');
create type subscription_status as enum ('active','cancelled');
create table nutris(id uuid primary key,role text default 'user',is_suspended boolean default false,
 subscription_type subscription_type default 'monthly',subscription_status subscription_status default 'active',
 subscription_start date default current_date, subscription_end date default current_date+30,
 subscription_next_billing_date timestamptz,subscription_cancelled_at timestamptz,mp_preapproval_id text default 'preapproval-1',
 max_patients integer default 15,max_reports_month integer default 30,reports_this_month integer default 0,
 reports_total integer default 0,reports_month_reset date default current_date);
create table patients(id uuid primary key,nutri_id uuid);
create table reports(id uuid primary key,patient_id uuid,nutri_id uuid,measurement_date timestamptz,
 weight_kg numeric,body_fat_pct numeric,muscle_mass_kg numeric,visceral_fat numeric,bmr_kcal numeric,
 metabolic_age integer,csv_raw text,pdf_storage_path text,generation_secs numeric,source text,native_measurement_id uuid);
insert into patients values('${owner}','${owner}');
insert into nutris(id) values('${owner}'),('${other}');
grant all on nutris to service_role; grant select,update on nutris to authenticated;
alter table nutris enable row level security;
create policy own on nutris for all to authenticated using(id=auth.uid()) with check(id=auth.uid());`)
for(const file of ['2026_06_25_cupo_reset_aniversario.sql','20261013114739_admin_account_extensions.sql','2026_07_31_mp_webhook_audit.sql','20261015130425_dashboard_billing.sql'])
 await db.exec(await readFile(new URL('../migrations/'+file,import.meta.url),'utf8'))
const scalar=async(sql,args=[]) => (await db.query(sql,args)).rows[0].v
const date=async(days=0) => scalar('select (billing_today()+$1::int)::text v',[days])
const gate=()=>scalar('select can_generate_report($1) v',[owner])
const row=()=>scalar('select to_jsonb(n) v from nutris n where id=$1',[owner])
const basic={id:'bioscan_basico_mensual',title:'Básico',months:1,subscription_type:'monthly',max_reports_month:30,max_patients:15,unit_price:28500}
const plus={...basic,id:'bioscan_plus_mensual',title:'Plus',max_reports_month:100,max_patients:40,unit_price:65000}
const create=async(extra={})=>scalar('select billing_create_order($1::jsonb) v',[JSON.stringify({
 id:uuid(),nutri_id:owner,kind:'pack',mode:'cycle',reports:10,amount:'9500',paid_price:'28500',
 target_plan:null,current_plan:basic,preapproval_id:'preapproval-1',previous_end:await date(30),
 cycle_start:await date(),cycle_end:await date(30),expiry_policy:'activation',...extra})])
const pay=async(o,id=uuid(),amount=o.amount,status='approved')=>scalar('select billing_accept_payment($1,$2,$3,$4,$5,now()) v',[o.id,id,amount,'ARS',status])
let expired=await create();
await db.query("update billing_orders set checkout_expires_at=now()-interval '1 minute' where id=$1",[expired.id]);
await db.query('select billing_tick($1)',[owner]);
assert.equal(await scalar('select status v from billing_orders where id=$1',[expired.id]),'cancelled');
let o=await create(); assert.equal(o.status,'checkout')
let payment=uuid(); await pay(o,payment); assert.equal((await gate()).remaining,40)
await pay(o,payment); assert.equal((await gate()).remaining,40)
assert.equal((await row()).subscription_end,await date(30))
await pay(o,uuid()); assert.equal((await gate()).remaining,40) // second payment needs refund, never double grant
for(let i=0;i<35;i++) await db.query('select increment_report_count($1)',[owner])
assert.equal((await gate()).remaining,5)
// A purchased balance is usable even if it was bought in a previous cycle.
await db.query("update billing_pack_credits set created_at=now()-interval '2 years' where order_id=$1",[o.id])
assert.equal((await gate()).remaining,5)
let bad=await create(); assert.equal((await pay(bad,uuid(),1)).status,'review'); assert.equal((await gate()).remaining,5)
let up=await create({kind:'change',mode:'exhaustion',reports:0,target_plan:plus,requested_plan:plus.id,amount:65000})
await pay(up)
await db.query('select billing_tick($1)',[owner]); assert.equal((await row()).max_reports_month,30) // not paused yet
await db.query('select billing_pause_confirmed($1)',[up.id]); await db.query('select billing_tick($1)',[owner])
assert.equal((await row()).max_reports_month,100); assert.equal((await gate()).remaining,105)
assert.equal((await row()).subscription_end,await date(30))
await db.query('select billing_sync_confirmed($1)',[up.id])
// No repeated credits on old checkout payment after activation.
await pay(up,(await scalar('select to_jsonb(o) v from billing_orders o where id=$1',[up.id])).payment_id)
assert.equal((await gate()).remaining,105)
// Downgrade must retain old limits until expiry + verified charge.
let down=await create({kind:'change',mode:'expiry',reports:0,target_plan:basic,requested_plan:basic.id,amount:28500,current_plan:plus})
await db.query('select billing_tick($1)',[owner]); assert.equal((await row()).max_reports_month,100)
assert.equal((await create({kind:'change',mode:'expiry',reports:0,target_plan:basic,requested_plan:basic.id})).ok,false) // block conflicting purchases
await db.query('select billing_sync_confirmed($1)',[down.id])
await db.query('update billing_orders set previous_end=current_date where id=$1',[down.id])
await pay(down); await db.query('select billing_tick($1)',[owner])
assert.equal((await row()).max_reports_month,30)
await db.query('select billing_sync_confirmed($1)',[down.id])
// Upgrade with remaining credits waits, then activates upon consuming the last credit.
let waiting=await create({kind:'change',mode:'exhaustion',reports:0,target_plan:plus,requested_plan:plus.id,amount:65000})
await pay(waiting); await db.query('select billing_pause_confirmed($1)',[waiting.id]); await db.query('select billing_tick($1)',[owner])
assert.equal((await row()).max_reports_month,30)
for(let i=0;i<30;i++) await db.query('select increment_report_count($1)',[owner])
assert.equal((await row()).max_reports_month,100); assert.equal((await gate()).remaining,105)
await db.query('select billing_sync_confirmed($1)',[waiting.id]);
// Refund revokes only unspent package credits, never changes the expiry.
const refundOrder=await create(), refundPayment=uuid();
await pay(refundOrder,refundPayment); assert.equal((await gate()).pack_remaining,15);
await pay(refundOrder,refundPayment,refundOrder.amount,'refunded'); assert.equal((await gate()).pack_remaining,5);
// Scheduled downgrade can be cancelled, with provider restoration still required.
const cancelOrder=await create({kind:'change',mode:'expiry',reports:0,target_plan:basic,requested_plan:basic.id,provider_before:{status:'authorized'}});
assert.equal((await scalar('select billing_cancel_order($1,$2) v',[cancelOrder.id,other])).ok,false);
assert.equal((await scalar('select billing_cancel_order($1,$2) v',[cancelOrder.id,owner])).ok,true);
assert.equal(await scalar('select status v from billing_orders where id=$1',[cancelOrder.id]),'cancelling');
await db.query("update billing_orders set status='cancelled' where id=$1",[cancelOrder.id]);
assert.equal((await gate()).pack_remaining,5); // Original balance survives both plan changes.
// Renewal uses a verified receipt once and requires the matching provider date/amount.
await db.query('update nutris set subscription_end=current_date where id=$1',[owner]);
const renewal=uuid();
assert.equal((await scalar('select billing_renew($1,$2,$3,$4,now(),$5) v',[owner,renewal,65000,'ARS',await date(30)])).ok,true);
await db.query('select increment_report_count($1)',[owner]);
const afterRenewal=(await gate()).remaining;
assert.equal((await gate()).pack_remaining,5); // Renewal does not reset purchased credits.
assert.equal((await scalar('select billing_renew($1,$2,$3,$4,now(),$5) v',[owner,renewal,65000,'ARS',await date(30)])).duplicate,true);
assert.equal((await gate()).remaining,afterRenewal);
// Lease ensures two workers cannot issue conflicting provider updates.
const token=uuid(); assert.equal(await scalar('select billing_claim_sync($1,$2) v',[owner,token]),true)
assert.equal(await scalar('select billing_claim_sync($1,$2) v',[owner,uuid()]),false)
await db.query('select billing_release_sync($1,$2)',[owner,token]); assert.equal(await scalar('select billing_claim_sync($1,$2) v',[owner,uuid()]),true)
// Atomic report creation retries do not spend credits twice.
const rid=uuid(), payload={id:rid,patient_id:owner,nutri_id:owner,measurement_date:await date()};
await db.query('select billing_create_report($1::jsonb)',[JSON.stringify(payload)]);
const used=(await row()).reports_total;
await db.query('select billing_create_report($1::jsonb)',[JSON.stringify(payload)]);
assert.equal((await row()).reports_total,used);
await assert.rejects(()=>db.query('select billing_create_report($1::jsonb)',[JSON.stringify({...payload,id:uuid(),patient_id:other})]),/ownership/);
assert.equal((await row()).reports_total,used);
// Legacy renewal and receipt commit together; repeated/stale events preserve the audit.
const legacyInvoice='invoice-legacy', oldEnd=await date(30), legacyEnd=await date(60);
const legacyArgs=[other,'preapproval-1',legacyInvoice,oldEnd,JSON.stringify({subscription_end:legacyEnd}),JSON.stringify({action:'renewal_extended'})];
const renewLegacy=()=>scalar('select billing_apply_legacy_renewal($1,$2,$3,$4,$5::jsonb,$6::jsonb) v',legacyArgs);
assert.equal((await renewLegacy()).subscription_end,legacyEnd);
assert.equal((await renewLegacy()).detail,'already_processed');
await db.query("update payment_webhook_events set processing_status='ignored',detail='{}' where provider_event_id=$1",[legacyInvoice]);
assert.equal(await scalar('select processing_status v from payment_webhook_events where provider_event_id=$1',[legacyInvoice]),'processed');
assert.equal(await scalar('select subscription_end::text v from nutris where id=$1',[other]),legacyEnd);
// A notification received after expiry must credit a pack paid before the deadline.
const late=await create(), latePayment=uuid();
await db.query("update billing_orders set status='cancelled',review_reason='checkout_expired',checkout_expires_at=now()-interval '1 second' where id=$1",[late.id]);
const packBefore=(await gate()).pack_remaining;
const lateResult=await scalar("select billing_accept_payment($1,$2,$3,'ARS','approved',now()-interval '2 seconds') v",[late.id,latePayment,late.amount]);
assert.equal(lateResult.ok,true);
assert.equal((await gate()).pack_remaining,packBefore+10);
await pay(late,latePayment,late.amount,'partially_refunded');
assert.equal((await gate()).pack_remaining,packBefore);
// Preserve the old expiry when upgrading, while retaining independent 30-day quotas.
await db.query("update nutris set subscription_end=billing_today()+15,subscription_start=billing_today()-15,reports_this_month=30,reports_month_reset=billing_today(),max_reports_month=30,billing_current_plan=null where id=$1",[owner]);
const preserving=await create({kind:'change',mode:'exhaustion',reports:0,target_plan:plus,requested_plan:plus.id,amount:65000,previous_end:await date(15),expiry_policy:'extend_expiry'});
await pay(preserving);
await db.query('select billing_pause_confirmed($1)',[preserving.id]);
await db.query('select billing_tick($1)',[owner]);
assert.equal((await row()).subscription_end,await date(45));
assert.equal((await gate()).pack_remaining,5);
await db.query("update nutris set subscription_start=billing_today()-30,reports_this_month=99,reports_month_reset=billing_today()-1 where id=$1",[owner]);
assert.equal((await gate()).used,0);
assert.equal((await gate()).remaining,105);
// Tenant isolation and no ability to mint a purchase/payment via browser RPC.
await db.exec(`set role authenticated; select set_config('test.uid','${other}',false);`)
assert.equal((await db.query('select * from billing_orders')).rows.length,0)
assert.equal((await gate()).ok,false)
await assert.rejects(()=>db.query('select billing_pause_confirmed($1)',[waiting.id]),/permission denied/)
await assert.rejects(()=>db.exec(`insert into billing_pack_credits(order_id,nutri_id,granted,remaining) values('${uuid()}','${other}',10,10)`),/permission denied/)
await assert.rejects(()=>db.exec(`update nutris set billing_paid_price=1 where id='${other}'`),/managed by the server/);
await assert.rejects(()=>db.exec(`update nutris set max_reports_month=10000 where id='${other}'`),/managed by the server/);
await db.close(); console.log('Billing SQL scenarios passed: non-expiring credits, retries, upgrade, downgrade, concurrency lease, RLS.')
