begin;
set local lock_timeout='3s';
set local statement_timeout='30s';

create function public.billing_today() returns date language sql stable set search_path='' as $$
 select (now() at time zone 'America/Argentina/Buenos_Aires')::date;
$$;
revoke all on function public.billing_today() from public,anon;
grant execute on function public.billing_today() to authenticated,service_role;

alter table public.nutris add column billing_current_plan jsonb;
alter table public.nutris add column billing_paid_price numeric(12,2);

create table public.billing_orders (
 id uuid primary key,
 nutri_id uuid not null references public.nutris(id),
 kind text not null check(kind in ('pack','change')),
 mode text not null check(mode in ('cycle','exhaustion','expiry')),
 status text not null check(status in ('checkout','paid','scheduled','syncing','applied','review','cancelled','cancelling')),
 requested_plan text,
 reports integer not null check(reports in (0,10)),
 amount numeric(12,2) not null check(amount>0),
 paid_price numeric(12,2) not null check(paid_price>0),
 target_plan jsonb, current_plan jsonb not null,
 preapproval_id text not null,
 renewal_preapproval_id text unique, renewal_init_point text, renewal_due date,
 previous_end date not null, cycle_start date not null, cycle_end date not null,
 expiry_policy text not null check(expiry_policy in ('activation','extend_expiry')),
 payment_id text unique,
 provider_paused boolean not null default false,
 provider_synced boolean not null default false,
 activated_on date, new_end date,
 checkout_expires_at timestamptz not null,
 provider_before jsonb, preference_id text, init_point text,
 review_reason text,
 created_at timestamptz not null default now(),
 check((kind='pack' and mode='cycle' and reports>0 and target_plan is null)
    or (kind='change' and mode in ('exhaustion','expiry') and reports=0 and target_plan is not null))
);
create unique index billing_one_change on public.billing_orders(nutri_id)
 where kind='change' and status in ('checkout','paid','scheduled','syncing','cancelling');
create index billing_orders_owner on public.billing_orders(nutri_id,created_at);
create index billing_orders_old_contract on public.billing_orders(preapproval_id);
create index billing_work on public.billing_orders(status) where status in ('paid','scheduled','syncing','cancelling');

create table public.billing_pack_credits (
 order_id uuid primary key references public.billing_orders(id),
 nutri_id uuid not null references public.nutris(id),
 created_at timestamptz not null default now(),
 granted integer not null check(granted=10),
 remaining integer not null check(remaining>=0 and remaining<=granted)
);
create index billing_pack_owner on public.billing_pack_credits(nutri_id,created_at) where remaining>0;

-- Durable receipt ledger also holds duplicate payments/chargebacks for review.
create table public.billing_receipts (
 payment_id text primary key, order_id uuid not null references public.billing_orders(id),
 nutri_id uuid not null references public.nutris(id),
 amount numeric(12,2) not null, currency text not null, status text not null,
 processed_at timestamptz not null default now()
);
create index billing_receipts_owner on public.billing_receipts(nutri_id);

alter table public.billing_orders enable row level security;
alter table public.billing_pack_credits enable row level security;
alter table public.billing_receipts enable row level security;
revoke all on public.billing_orders,public.billing_pack_credits,public.billing_receipts from public,anon,authenticated;
grant select on public.billing_orders,public.billing_pack_credits to authenticated;
grant all on public.billing_orders,public.billing_pack_credits,public.billing_receipts to service_role;
create policy billing_orders_read on public.billing_orders for select to authenticated using(nutri_id=(select auth.uid()));
create policy billing_pack_read on public.billing_pack_credits for select to authenticated using(nutri_id=(select auth.uid()));

create function public.billing_cycle_start(n public.nutris) returns date language sql stable set search_path='' as $$
 select case when n.billing_current_plan is not null and n.subscription_type='monthly'
   then n.subscription_start + greatest(0,(public.billing_today()-n.subscription_start)/30)*30
   else public.cupo_cycle_start(n.subscription_start,public.billing_today()) end;
$$;

-- Both purchased and administrative credits survive renewal and plan changes.
-- Spend the monthly allowance first, then purchased credits, then admin credits.
create or replace function public.can_generate_report(p_nutri_id uuid)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype; used integer; bonus integer; packs integer; remaining integer;
begin
 select * into n from public.nutris where id=p_nutri_id;
 if not found then return jsonb_build_object('ok',false,'reason','not_found'); end if;
 if n.is_suspended or n.subscription_status!='active' then
  return jsonb_build_object('ok',false,'reason','subscription_inactive'); end if;
 if n.subscription_end is not null and (n.subscription_end < public.billing_today()
  or (n.billing_current_plan is not null and n.subscription_end <= public.billing_today())) then
  return jsonb_build_object('ok',false,'reason','subscription_expired'); end if;
 used:=n.reports_this_month;
 if n.subscription_type in ('monthly','semestral') and n.subscription_start is not null
   and (n.reports_month_reset is null or n.reports_month_reset<public.billing_cycle_start(n)) then used:=0; end if;
 select coalesce(sum(reports_remaining),0) into bonus from public.account_extensions where nutri_id=p_nutri_id;
 select coalesce(sum(pc.remaining),0) into packs from public.billing_pack_credits pc
  where nutri_id=p_nutri_id;
 remaining:=greatest(n.max_reports_month-used,0)+bonus+packs;
 return jsonb_build_object('ok',remaining>0,'reason',case when remaining=0 then 'monthly_limit_reached' else null end,
  'used',used,'limit',used+remaining,'remaining',remaining,'extra_remaining',bonus,'pack_remaining',packs);
end; $$;

create function public.billing_create_order(p_order jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype; o public.billing_orders%rowtype; oid uuid:=(p_order->>'id')::uuid;
begin
 perform pg_advisory_xact_lock(hashtextextended(oid::text,0));
 select * into o from public.billing_orders where id=oid;
 if found then
  if o.nutri_id<>(p_order->>'nutri_id')::uuid then return jsonb_build_object('ok',false,'reason','request_conflict'); end if;
  return to_jsonb(o);
 end if;
 select * into n from public.nutris where id=(p_order->>'nutri_id')::uuid for update;
 if not found or n.is_suspended or n.subscription_status!='active' or n.subscription_end<=public.billing_today()
  or n.subscription_end is null or n.mp_preapproval_id is distinct from p_order->>'preapproval_id'
  or n.subscription_end is distinct from (p_order->>'previous_end')::date then
  return jsonb_build_object('ok',false,'reason','account_changed'); end if;
 update public.billing_orders set status='cancelled',review_reason='checkout_expired'
  where nutri_id=n.id and status='checkout' and checkout_expires_at<now();
 if exists(select 1 from public.billing_orders where nutri_id=n.id and kind='change'
   and status in ('checkout','paid','scheduled','syncing','cancelling')
   and (p_order->>'kind'='change' or status='syncing')) then
  return jsonb_build_object('ok',false,'reason','Ya hay un cambio de plan pendiente'); end if;
 insert into public.billing_orders(id,nutri_id,kind,mode,status,requested_plan,reports,amount,paid_price,
  target_plan,current_plan,preapproval_id,previous_end,cycle_start,cycle_end,expiry_policy,checkout_expires_at,provider_before)
 values(oid,n.id,p_order->>'kind',p_order->>'mode',case when p_order->>'mode'='expiry' then 'scheduled' else 'checkout' end,
  p_order->>'requested_plan',(p_order->>'reports')::integer,(p_order->>'amount')::numeric,(p_order->>'paid_price')::numeric,
  nullif(p_order->'target_plan','null'::jsonb),p_order->'current_plan',n.mp_preapproval_id,n.subscription_end,
  (p_order->>'cycle_start')::date,(p_order->>'cycle_end')::date,p_order->>'expiry_policy',
  least(now()+interval '30 minutes',((p_order->>'cycle_end')::date::timestamp at time zone 'America/Argentina/Buenos_Aires')),nullif(p_order->'provider_before','null'::jsonb)) returning * into o;
 return to_jsonb(o);
end; $$;

create function public.billing_accept_payment(p_order_id uuid,p_payment_id text,p_amount numeric,
 p_currency text,p_status text,p_paid_at timestamptz) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare o public.billing_orders%rowtype; n public.nutris%rowtype; prior public.billing_receipts%rowtype;
begin
 -- Same lock order throughout: account, order. Unique payment ID prevents cross-order replay.
 select * into o from public.billing_orders where id=p_order_id;
 if not found then return jsonb_build_object('ok',false,'reason','unknown_order'); end if;
 select * into n from public.nutris where id=o.nutri_id for update;
 select * into o from public.billing_orders where id=p_order_id for update;
 select * into prior from public.billing_receipts where payment_id=p_payment_id;
 if found and prior.order_id<>o.id then return jsonb_build_object('ok',false,'reason','payment_conflict'); end if;
 if found and (prior.status=p_status or (prior.status in ('approved','refunded','charged_back','partially_refunded') and p_status in ('pending','in_process','authorized'))) then
  return jsonb_build_object('ok',true,'nutri_id',o.nutri_id,'status',o.status,'duplicate',true); end if;
 insert into public.billing_receipts(payment_id,order_id,nutri_id,amount,currency,status)
 values(p_payment_id,o.id,o.nutri_id,p_amount,p_currency,p_status)
 on conflict(payment_id) do update set status=excluded.status,processed_at=now();
 if p_status in ('refunded','charged_back','partially_refunded') then
  if o.payment_id is not null and o.payment_id<>p_payment_id then
   return jsonb_build_object('ok',true,'status','duplicate_payment_reversed');
  end if;
  update public.billing_pack_credits set remaining=0 where order_id=o.id;
  update public.billing_orders set review_reason='payment_reversed',status='review' where id=o.id;
  return jsonb_build_object('ok',true,'status','review');
 end if;
 if p_status<>'approved' then return jsonb_build_object('ok',true,'status','payment_pending'); end if;
 if o.payment_id is not null and o.payment_id<>p_payment_id then
  update public.billing_orders set review_reason='duplicate_charge_requires_refund' where id=o.id;
  return jsonb_build_object('ok',true,'status','duplicate_charge_review'); end if;
 if (o.status not in ('checkout','scheduled','paid') and not (o.status='cancelled' and o.review_reason='checkout_expired')) or p_amount<>o.amount or p_currency<>'ARS' or p_paid_at is null
   or (p_paid_at at time zone 'America/Argentina/Buenos_Aires')::date<(o.created_at at time zone 'America/Argentina/Buenos_Aires')::date
   or n.is_suspended or n.mp_preapproval_id is distinct from o.preapproval_id
   or (o.mode<>'expiry' and (p_paid_at>o.checkout_expires_at or (o.kind='change' and n.subscription_end<>o.previous_end)))
   or (o.mode='expiry' and ((p_paid_at at time zone 'America/Argentina/Buenos_Aires')::date<o.previous_end or not o.provider_synced)) then
  update public.billing_orders set status='review',payment_id=p_payment_id,review_reason='payment_or_cycle_mismatch' where id=o.id;
  return jsonb_build_object('ok',true,'status','review'); end if;
 if o.kind='pack' then
  insert into public.billing_pack_credits(order_id,nutri_id,granted,remaining) values(o.id,o.nutri_id,o.reports,o.reports)
   on conflict(order_id) do nothing;
  update public.billing_orders set status='applied',payment_id=p_payment_id,review_reason=null where id=o.id;
 elsif o.mode='expiry' then
  update public.billing_orders set status='paid',payment_id=p_payment_id,provider_paused=true where id=o.id;
 else
  update public.billing_orders set status='paid',payment_id=p_payment_id,review_reason=null where id=o.id;
 end if;
 return jsonb_build_object('ok',true,'nutri_id',o.nutri_id);
end; $$;

create function public.billing_pause_confirmed(p_order_id uuid) returns void
language sql security invoker set search_path='' as $$
 update public.billing_orders set provider_paused=true where id=p_order_id and status='paid';
$$;

create function public.billing_tick(p_nutri_id uuid) returns void
language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype; o public.billing_orders%rowtype; q jsonb; end_date date; base date;
begin
 select * into n from public.nutris where id=p_nutri_id for update;
 if not found or n.is_suspended then return; end if;
 update public.billing_orders set status='cancelled',review_reason='checkout_expired'
  where nutri_id=p_nutri_id and status='checkout' and checkout_expires_at<now();
 select * into o from public.billing_orders where nutri_id=p_nutri_id and kind='change' and status='paid' for update;
 if not found or not o.provider_paused then return; end if;
 q:=public.can_generate_report(p_nutri_id);
 if o.mode='exhaustion' and greatest(n.max_reports_month-coalesce((q->>'used')::integer,n.max_reports_month),0)>0 and public.billing_today()<o.cycle_end then return; end if;
 if o.mode='expiry' and public.billing_today()<o.previous_end then return; end if;
 base:=case when o.expiry_policy='extend_expiry' and o.mode='exhaustion' then greatest(public.billing_today(),o.previous_end) when o.mode='expiry' then o.previous_end else public.billing_today() end;
 end_date:=case when (o.target_plan->>'months')::integer=1 then base+30 else (base+interval '6 months')::date end;
 n.subscription_type:=o.target_plan->>'subscription_type';
 update public.nutris set subscription_type=n.subscription_type,subscription_status='active',
  subscription_start=case when o.mode='expiry' then o.previous_end else public.billing_today() end,
  subscription_end=end_date,subscription_next_billing_date=end_date,
  reports_this_month=0,reports_month_reset=case when o.mode='expiry' then o.previous_end else public.billing_today() end,
  max_reports_month=(o.target_plan->>'max_reports_month')::integer,
  max_patients=(o.target_plan->>'max_patients')::integer,
  billing_current_plan=o.target_plan,billing_paid_price=o.amount where id=p_nutri_id;
 update public.billing_orders set status='syncing',activated_on=public.billing_today(),new_end=end_date,provider_synced=false where id=o.id;
end; $$;

create function public.billing_sync_confirmed(p_order_id uuid) returns void
language plpgsql security invoker set search_path='' as $$
declare o public.billing_orders%rowtype;
begin
 select * into o from public.billing_orders where id=p_order_id;
 if not found then raise exception 'Unknown order'; end if;
 perform 1 from public.nutris where id=o.nutri_id for update;
 select * into o from public.billing_orders where id=p_order_id for update;
 if o.status not in ('syncing','scheduled') then return; end if;
 if o.renewal_preapproval_id is null then raise exception 'Replacement authorization missing'; end if;
 if o.status='syncing' then
  if o.payment_id is null then raise exception 'Verified payment missing'; end if;
  update public.nutris set mp_preapproval_id=o.renewal_preapproval_id,
   subscription_cancelled_at=null where id=o.nutri_id and mp_preapproval_id=o.preapproval_id;
  if not found then raise exception 'Current subscription changed'; end if;
 end if;
 update public.billing_orders set provider_synced=true,provider_paused=true,
  status=case when status='syncing' then 'applied' else status end where id=p_order_id;
end; $$;

create or replace function public.increment_report_count(p_nutri_id uuid) returns void
language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype; used integer; credit_id uuid; q jsonb;
begin
 select * into n from public.nutris where id=p_nutri_id for update;
 if not found then raise exception 'Account not found'; end if;
 q:=public.can_generate_report(p_nutri_id);
 if not (q->>'ok')::boolean then raise exception 'Report quota exhausted'; end if;
 used:=(q->>'used')::integer;
 if used>=n.max_reports_month then
  select order_id into credit_id from public.billing_pack_credits
   where nutri_id=p_nutri_id and remaining>0
   order by created_at,order_id limit 1 for update;
  if found then update public.billing_pack_credits set remaining=remaining-1 where order_id=credit_id;
  else
   select request_id into credit_id from public.account_extensions where nutri_id=p_nutri_id and reports_remaining>0
    order by created_at,request_id limit 1 for update;
   if found then update public.account_extensions set reports_remaining=reports_remaining-1 where request_id=credit_id; end if;
  end if;
 end if;
 update public.nutris set reports_this_month=used+1,reports_total=reports_total+1,
  reports_month_reset=public.billing_today() where id=p_nutri_id;
 perform public.billing_tick(p_nutri_id);
end; $$;

-- Mutating RPCs are service-only; no browser can mint a payment or grant credits.
revoke all on function public.billing_create_order(jsonb), public.billing_accept_payment(uuid,text,numeric,text,text,timestamptz),
 public.billing_pause_confirmed(uuid),public.billing_tick(uuid),public.billing_sync_confirmed(uuid) from public,anon,authenticated;
grant execute on function public.billing_create_order(jsonb), public.billing_accept_payment(uuid,text,numeric,text,text,timestamptz),
 public.billing_pause_confirmed(uuid),public.billing_tick(uuid),public.billing_sync_confirmed(uuid) to service_role;
revoke all on function public.billing_cycle_start(public.nutris) from public,anon;
grant execute on function public.billing_cycle_start(public.nutris) to authenticated,service_role;
create table public.billing_sync_locks (
 nutri_id uuid primary key references public.nutris(id), token uuid not null, expires_at timestamptz not null
);
alter table public.billing_sync_locks enable row level security;
revoke all on public.billing_sync_locks from public,anon,authenticated;
grant all on public.billing_sync_locks to service_role;
create function public.billing_claim_sync(p_nutri_id uuid,p_token uuid) returns boolean
language plpgsql security invoker set search_path='' as $$
begin
 insert into public.billing_sync_locks values(p_nutri_id,p_token,now()+interval '5 minutes')
 on conflict(nutri_id) do update set token=excluded.token,expires_at=excluded.expires_at
 where public.billing_sync_locks.expires_at<now();
 return found;
end; $$;
create function public.billing_release_sync(p_nutri_id uuid,p_token uuid) returns void
language sql security invoker set search_path='' as $$
 delete from public.billing_sync_locks where nutri_id=p_nutri_id and token=p_token;
$$;
create table public.billing_renewals (
 payment_id text primary key,nutri_id uuid not null references public.nutris(id),
 amount numeric(12,2) not null,paid_on date not null,new_end date not null
);
alter table public.billing_renewals enable row level security;
revoke all on public.billing_renewals from public,anon,authenticated;
grant all on public.billing_renewals to service_role;
create index billing_renewals_owner on public.billing_renewals(nutri_id);
create function public.billing_renew(p_nutri_id uuid,p_payment_id text,p_amount numeric,
 p_currency text,p_paid_at timestamptz,p_next_date date) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype; paid_on date; expected_end date;
begin
 select * into n from public.nutris where id=p_nutri_id for update;
 if not found then return jsonb_build_object('ok',false,'reason','not_found'); end if;
 if exists(select 1 from public.billing_renewals where payment_id=p_payment_id and nutri_id=p_nutri_id)
 or exists(select 1 from public.billing_receipts where payment_id=p_payment_id and nutri_id=p_nutri_id) then
  return jsonb_build_object('ok',true,'duplicate',true); end if;
 paid_on:=(p_paid_at at time zone 'America/Argentina/Buenos_Aires')::date;
 expected_end:=case when n.subscription_type='monthly' then n.subscription_end+30
  else (n.subscription_end+interval '6 months')::date end;
 if n.billing_current_plan is null or p_currency<>'ARS' or p_amount<>(n.billing_current_plan->>'unit_price')::numeric
  or paid_on is null or paid_on<n.subscription_end or p_next_date is distinct from expected_end then
  return jsonb_build_object('ok',false,'reason','renewal_needs_reconciliation'); end if;
 insert into public.billing_renewals values(p_payment_id,p_nutri_id,p_amount,paid_on,p_next_date);
 update public.nutris set subscription_status='active',subscription_start=n.subscription_end,
  subscription_end=p_next_date,subscription_next_billing_date=p_next_date,billing_paid_price=p_amount,
  reports_this_month=0,reports_month_reset=n.subscription_end where id=p_nutri_id;
 return jsonb_build_object('ok',true,'subscription_end',p_next_date);
end; $$;
revoke all on function public.billing_claim_sync(uuid,uuid),public.billing_release_sync(uuid,uuid),
 public.billing_renew(uuid,text,numeric,text,timestamptz,date) from public,anon,authenticated;
grant execute on function public.billing_claim_sync(uuid,uuid),public.billing_release_sync(uuid,uuid),
 public.billing_renew(uuid,text,numeric,text,timestamptz,date) to service_role;
create function public.billing_create_report(p_report jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare r public.reports%rowtype; rid uuid:=coalesce((p_report->>'id')::uuid,gen_random_uuid());
 owner_id uuid:=(p_report->>'nutri_id')::uuid;
begin
 perform 1 from public.nutris where id=owner_id for update;
 if not exists(select 1 from public.patients where id=(p_report->>'patient_id')::uuid and nutri_id=owner_id) then
  raise exception 'Patient ownership mismatch'; end if;
 select * into r from public.reports where id=rid;
 if found then
  if r.nutri_id<>owner_id or r.patient_id<>(p_report->>'patient_id')::uuid then raise exception 'Report conflict'; end if;
  return to_jsonb(r);
 end if;
 -- Same transaction: failed insertion cannot spend a credit; quota failure cannot leave a report row.
 perform public.increment_report_count(owner_id);
 insert into public.reports(id,patient_id,nutri_id,measurement_date,weight_kg,body_fat_pct,muscle_mass_kg,
 visceral_fat,bmr_kcal,metabolic_age,csv_raw,pdf_storage_path,generation_secs,source,native_measurement_id)
 values(rid,(p_report->>'patient_id')::uuid,owner_id,(p_report->>'measurement_date')::timestamptz,
 (p_report->>'weight_kg')::numeric,(p_report->>'body_fat_pct')::numeric,(p_report->>'muscle_mass_kg')::numeric,
 (p_report->>'visceral_fat')::numeric,(p_report->>'bmr_kcal')::numeric,(p_report->>'metabolic_age')::integer,
 p_report->>'csv_raw',p_report->>'pdf_storage_path',(p_report->>'generation_secs')::numeric,
 coalesce(p_report->>'source','mytanita'),(p_report->>'native_measurement_id')::uuid) returning * into r;
 return to_jsonb(r);
end; $$;
revoke all on function public.billing_create_report(jsonb) from public,anon,authenticated;
grant execute on function public.billing_create_report(jsonb) to service_role;
-- The legacy nutri_own policy permits profile updates. Billing fields must not
-- become writable merely because a user owns the profile row.
create function public.billing_protect_account() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
 if current_user in ('anon','authenticated')
  and not exists(select 1 from public.nutris where id=(select auth.uid()) and role='admin') and
  (to_jsonb(new)-array['full_name','phone','display_signature','updated_at']) is distinct from
  (to_jsonb(old)-array['full_name','phone','display_signature','updated_at']) then
   raise exception 'Billing and account privileges are managed by the server';
 end if;
 return new;
end; $$;
create trigger billing_protect_account before update on public.nutris
 for each row execute function public.billing_protect_account();
revoke all on function public.billing_protect_account() from public,anon,authenticated;
create function public.billing_cancel_order(p_order_id uuid,p_nutri_id uuid) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare o public.billing_orders%rowtype;
begin
 perform 1 from public.nutris where id=p_nutri_id for update;
 select * into o from public.billing_orders where id=p_order_id and nutri_id=p_nutri_id for update;
 if not found then return jsonb_build_object('ok',false,'reason','not_found'); end if;
 if o.status='cancelled' then return jsonb_build_object('ok',true); end if;
 if o.status not in ('checkout','scheduled','cancelling') or o.payment_id is not null or
  (o.status='scheduled' and (o.previous_end<=public.billing_today() or o.provider_before is null)) then
  return jsonb_build_object('ok',false,'reason','Este cambio ya no puede cancelarse automáticamente'); end if;
 update public.billing_orders set status=case when o.status='checkout' then 'cancelled' else 'cancelling' end where id=o.id;
 return jsonb_build_object('ok',true);
end; $$;
revoke all on function public.billing_cancel_order(uuid,uuid) from public,anon,authenticated;
grant execute on function public.billing_cancel_order(uuid,uuid) to service_role;

-- Renewal writes and their receipt commit together. A concurrent notification
-- cannot extend twice or reset quota. Existing manual reconciliations remain final.
create function public.billing_preserve_processed_event() returns trigger
language plpgsql set search_path='' as $$
begin
 if old.processing_status='processed' then return old; end if;
 return new;
end; $$;
create trigger billing_preserve_processed_event before update on public.payment_webhook_events
 for each row execute function public.billing_preserve_processed_event();
revoke all on function public.billing_preserve_processed_event() from public,anon,authenticated;

create function public.billing_apply_legacy_renewal(p_nutri_id uuid,p_preapproval_id text,
 p_invoice_id text,p_expected_end date,p_update jsonb,p_detail jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare n public.nutris%rowtype;
begin
 select * into n from public.nutris where id=p_nutri_id for update;
 if not found or n.mp_preapproval_id is distinct from p_preapproval_id then
  return jsonb_build_object('ok',false,'reason','account_changed'); end if;
 if exists(select 1 from public.payment_webhook_events where provider='mercadopago'
  and event_type='subscription_authorized_payment' and provider_event_id=p_invoice_id and processing_status='processed') then
  return jsonb_build_object('ok',true,'detail','already_processed'); end if;
 if n.subscription_end is distinct from p_expected_end then
  return jsonb_build_object('ok',false,'reason','account_changed_retry'); end if;
 update public.nutris set subscription_status='active',subscription_cancelled_at=null,
  subscription_end=(p_update->>'subscription_end')::date,
  subscription_next_billing_date=coalesce((p_update->>'subscription_next_billing_date')::timestamptz,n.subscription_next_billing_date),
  subscription_type=coalesce((jsonb_populate_record(null::public.nutris,p_update)).subscription_type,n.subscription_type),
  max_reports_month=coalesce((p_update->>'max_reports_month')::integer,n.max_reports_month),
  max_patients=coalesce((p_update->>'max_patients')::integer,n.max_patients) where id=p_nutri_id;
 insert into public.payment_webhook_events(provider,event_type,provider_event_id,preapproval_id,nutri_id,
  processing_status,provider_status,detail,processed_at)
 values('mercadopago','subscription_authorized_payment',p_invoice_id,p_preapproval_id,p_nutri_id,
  'processed','approved',p_detail,now())
 on conflict(provider,event_type,provider_event_id) do update set processing_status='processed',
  provider_status='approved',detail=excluded.detail,processed_at=now(),nutri_id=excluded.nutri_id;
 return jsonb_build_object('ok',true,'nutri_id',p_nutri_id,'detail',p_detail->>'action','subscription_end',p_update->>'subscription_end');
end; $$;
revoke all on function public.billing_apply_legacy_renewal(uuid,text,text,date,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.billing_apply_legacy_renewal(uuid,text,text,date,jsonb,jsonb) to service_role;
commit;
