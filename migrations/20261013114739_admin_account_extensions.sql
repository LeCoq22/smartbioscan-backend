begin;

-- One-time credits and an audit trail. Users can read their balance, never grant it.
create table public.account_extensions (
    request_id uuid primary key,
    nutri_id uuid not null references public.nutris(id),
    admin_id uuid not null references public.nutris(id),
    days integer not null check (days in (15, 30)),
    previous_end date not null,
    subscription_end date not null,
    extra_reports integer not null check (extra_reports between 0 and 10000),
    reports_remaining integer not null check (reports_remaining between 0 and extra_reports),
    created_at timestamptz not null default now()
);
create index account_extensions_nutri_credits_idx
    on public.account_extensions(nutri_id, created_at, request_id)
    where reports_remaining > 0;
alter table public.account_extensions enable row level security;
revoke all on public.account_extensions from public, anon, authenticated;
grant select on public.account_extensions to authenticated;
grant all on public.account_extensions to service_role;
create policy account_extensions_read on public.account_extensions
    for select to authenticated
    using (nutri_id = (select auth.uid()) or exists (
        select 1 from public.nutris n where n.id = (select auth.uid()) and n.role = 'admin'
    ));

-- Called only by the backend after JWT admin verification. Invoker privileges;
-- the service role is the only API role allowed to execute this function.
create function public.admin_extend_account(
    p_admin_id uuid, p_nutri_id uuid, p_request_id uuid,
    p_days integer, p_extra_reports integer, p_expected_end date
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    n public.nutris%rowtype;
    e public.account_extensions%rowtype;
    new_end date;
begin
    if not exists (select 1 from public.nutris where id = p_admin_id and role = 'admin') then
        return jsonb_build_object('ok', false, 'reason', 'forbidden');
    end if;
    if p_days is null or p_days not in (15, 30) or p_extra_reports is null
       or p_extra_reports not between 0 and 10000 or p_expected_end is null
       or p_request_id is null then
        return jsonb_build_object('ok', false, 'reason', 'invalid_input');
    end if;
    -- Serialize all retries, including requests accidentally targeting different accounts.
    perform pg_advisory_xact_lock(hashtextextended(p_request_id::text, 0));
    select * into e from public.account_extensions where request_id = p_request_id;
    if found then
        if e.nutri_id != p_nutri_id or e.admin_id != p_admin_id or e.days != p_days
           or e.extra_reports != p_extra_reports or e.previous_end != p_expected_end then
            return jsonb_build_object('ok', false, 'reason', 'request_conflict');
        end if;
        return jsonb_build_object('ok', true, 'subscription_end', e.subscription_end,
            'extra_reports', e.extra_reports, 'already_applied', true);
    end if;
    select * into n from public.nutris where id = p_nutri_id for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;
    if n.role = 'admin' then
        return jsonb_build_object('ok', false, 'reason', 'admin_account');
    end if;
    if n.is_suspended then
        return jsonb_build_object('ok', false, 'reason', 'suspended');
    end if;
    if n.subscription_end is null then
        return jsonb_build_object('ok', false, 'reason', 'no_expiry');
    end if;
    if n.subscription_end != p_expected_end then
        return jsonb_build_object('ok', false, 'reason', 'changed');
    end if;
    new_end := greatest(n.subscription_end, current_date) + p_days;
    update public.nutris set subscription_end = new_end, subscription_status = 'active'
        where id = p_nutri_id;
    insert into public.account_extensions
        (request_id, nutri_id, admin_id, days, previous_end, subscription_end, extra_reports, reports_remaining)
        values (p_request_id, p_nutri_id, p_admin_id, p_days, n.subscription_end, new_end, p_extra_reports, p_extra_reports);
    return jsonb_build_object('ok', true, 'subscription_end', new_end,
        'extra_reports', p_extra_reports, 'already_applied', false);
end;
$$;
revoke all on function public.admin_extend_account(uuid,uuid,uuid,integer,integer,date) from public, anon, authenticated;
grant execute on function public.admin_extend_account(uuid,uuid,uuid,integer,integer,date) to service_role;

-- Preserve the existing monthly anniversary reset, with one-time credits added
-- after the normal allowance. Keep signatures; restrict mutations to the backend.
create or replace function public.can_generate_report(p_nutri_id uuid)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    n public.nutris%rowtype;
    used integer;
    bonus integer;
    remaining integer;
begin
    select * into n from public.nutris where id = p_nutri_id;
    if not found then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;
    if n.is_suspended or n.subscription_status != 'active' then
        return jsonb_build_object('ok', false, 'reason', 'subscription_inactive');
    end if;
    if n.subscription_end is not null and n.subscription_end < current_date then
        return jsonb_build_object('ok', false, 'reason', 'subscription_expired');
    end if;
    used := n.reports_this_month;
    if n.subscription_type in ('monthly','semestral') and n.subscription_start is not null
       and (n.reports_month_reset is null
            or n.reports_month_reset < public.cupo_cycle_start(n.subscription_start, current_date)) then
        used := 0;
    end if;
    select coalesce(sum(reports_remaining), 0)::integer into bonus
        from public.account_extensions where nutri_id = p_nutri_id and reports_remaining > 0;
    remaining := greatest(n.max_reports_month - used, 0) + bonus;
    if remaining = 0 then
        return jsonb_build_object('ok', false, 'reason', 'monthly_limit_reached',
            'used', used, 'limit', n.max_reports_month, 'extra_remaining', 0);
    end if;
    return jsonb_build_object('ok', true, 'used', used, 'limit', used + remaining,
        'remaining', remaining, 'extra_remaining', bonus);
end;
$$;

create or replace function public.increment_report_count(p_nutri_id uuid)
returns void language plpgsql security invoker set search_path = '' as $$
declare
    n public.nutris%rowtype;
    used integer;
    reset_due boolean;
    credit_id uuid;
begin
    select * into n from public.nutris where id = p_nutri_id for update;
    if not found then
        raise no_data_found using message = 'Account not found';
    end if;
    reset_due := n.subscription_type in ('monthly','semestral') and n.subscription_start is not null
        and (n.reports_month_reset is null
             or n.reports_month_reset < public.cupo_cycle_start(n.subscription_start, current_date));
    used := case when reset_due then 0 else n.reports_this_month end;
    if used >= n.max_reports_month then
        select request_id into credit_id from public.account_extensions
            where nutri_id = p_nutri_id and reports_remaining > 0
            order by created_at, request_id limit 1 for update;
        if found then
            update public.account_extensions set reports_remaining = reports_remaining - 1
                where request_id = credit_id;
        end if;
    end if;
    update public.nutris set
        reports_this_month = used + 1,
        reports_total = reports_total + 1,
        reports_month_reset = case when reset_due then current_date else reports_month_reset end
        where id = p_nutri_id;
end;
$$;
revoke execute on function public.increment_report_count(uuid) from public, anon, authenticated;
grant execute on function public.increment_report_count(uuid) to service_role;
revoke execute on function public.can_generate_report(uuid) from public, anon;
grant execute on function public.can_generate_report(uuid) to authenticated, service_role;

commit;
