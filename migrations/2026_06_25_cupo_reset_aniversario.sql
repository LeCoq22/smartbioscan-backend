-- Migration: reset de cupo de reportes por ANIVERSARIO mensual de subscription_start
--
-- Antes: el reset de reports_this_month era por MES CALENDARIO
--   (date_trunc('month', reports_month_reset) < date_trunc('month', current_date)),
--   lo que daba un reset "gratis" el día 1 a quien pagaba a mitad de mes → cupo doble
--   en el primer ciclo. Además reseteaba a free_trial si el trial cruzaba fin de mes.
--
-- Ahora: el ciclo de cupo va de aniversario a aniversario de subscription_start.
--   Solo aplica a monthly/semestral; el resto (free_trial, beta, …) NO resetea
--   (cupo acumulativo para toda la vigencia). El semestral resetea cada mes-aniversario
--   durante sus 6 meses. Ancla estable = subscription_start (no reports_month_reset).
--
-- Transaction-safe (solo CREATE OR REPLACE FUNCTION, sin ALTER TYPE). Idempotente.

-- ── Helper: inicio del ciclo de cupo (aniversario mensual de subscription_start) ──
create or replace function public.cupo_cycle_start(p_start date, p_today date)
returns date language sql immutable as $$
  -- Aniversario mensual de p_start más reciente que sea <= p_today.
  -- El candidato "+1 mes" cubre el día inexistente (31 -> 30/29/28): en meses
  -- cortos hoy cae en el último día clampeado y age() lo cuenta de menos por 1.
  -- La suma de intervalo de Postgres clampea sola (ej. 31-ene + 1 mes = 28-feb).
  with m as (
    select (extract(year  from age(p_today, p_start))::int * 12
          + extract(month from age(p_today, p_start))::int) as n
  )
  select case
    when (p_start + ((n + 1) || ' months')::interval)::date <= p_today
      then (p_start + ((n + 1) || ' months')::interval)::date
    else  (p_start + ( n      || ' months')::interval)::date
  end
  from m;
$$;

comment on function public.cupo_cycle_start
  is 'Inicio del ciclo de cupo: aniversario mensual de subscription_start más reciente <= hoy (con clamp de día 31).';

-- ── Gate: verificar si el Nutri puede generar más reportes ──
create or replace function public.can_generate_report(p_nutri_id uuid)
returns jsonb language plpgsql security definer as $$
declare
    v_nutri     public.nutris%rowtype;
    v_today     date := current_date;
    v_month_cnt integer;
begin
    select * into v_nutri from public.nutris where id = p_nutri_id;

    -- Suscripción activa?
    if v_nutri.subscription_status != 'active' then
        return jsonb_build_object('ok', false, 'reason', 'subscription_inactive');
    end if;

    -- Suscripción expirada?
    if v_nutri.subscription_end is not null and v_nutri.subscription_end < v_today then
        return jsonb_build_object('ok', false, 'reason', 'subscription_expired');
    end if;

    -- Reset por ciclo mensual anclado en subscription_start (aniversario).
    -- Solo monthly/semestral. Otros tipos (free_trial, beta, invitation…) NO
    -- resetean: su cupo es acumulativo para toda la vigencia.
    if v_nutri.subscription_type in ('monthly','semestral')
       and v_nutri.subscription_start is not null
       and (v_nutri.reports_month_reset is null
            or v_nutri.reports_month_reset
               < public.cupo_cycle_start(v_nutri.subscription_start, v_today))
    then
        v_month_cnt := 0;
    else
        v_month_cnt := v_nutri.reports_this_month;
    end if;

    -- Límite mensual
    if v_month_cnt >= v_nutri.max_reports_month then
        return jsonb_build_object(
            'ok', false,
            'reason', 'monthly_limit_reached',
            'used', v_month_cnt,
            'limit', v_nutri.max_reports_month
        );
    end if;

    return jsonb_build_object(
        'ok', true,
        'used', v_month_cnt,
        'limit', v_nutri.max_reports_month,
        'remaining', v_nutri.max_reports_month - v_month_cnt
    );
end;
$$;

-- ── Incrementar contador (persiste el reset al primer reporte del ciclo nuevo) ──
create or replace function public.increment_report_count(p_nutri_id uuid)
returns void language plpgsql security definer as $$
declare
    v_nutri public.nutris%rowtype;
    v_today date := current_date;
begin
    select * into v_nutri from public.nutris where id = p_nutri_id;

    -- Reset por aniversario (misma lógica que can_generate_report). Solo monthly/semestral.
    if v_nutri.subscription_type in ('monthly','semestral')
       and v_nutri.subscription_start is not null
       and (v_nutri.reports_month_reset is null
            or v_nutri.reports_month_reset
               < public.cupo_cycle_start(v_nutri.subscription_start, v_today))
    then
        update public.nutris
        set reports_this_month  = 1,
            reports_total       = reports_total + 1,
            reports_month_reset = v_today
        where id = p_nutri_id;
    else
        update public.nutris
        set reports_this_month  = reports_this_month + 1,
            reports_total       = reports_total + 1
        where id = p_nutri_id;
    end if;
end;
$$;
