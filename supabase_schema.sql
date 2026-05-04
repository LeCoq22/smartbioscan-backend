-- ============================================================
-- SmartTanita — Supabase Schema v1
-- ============================================================
-- Ejecutar en el SQL Editor de Supabase en este orden.
-- Requiere: extensión pgcrypto (viene habilitada por defecto).
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- EXTENSIONES
-- ────────────────────────────────────────────────────────────

create extension if not exists "pgcrypto";


-- ────────────────────────────────────────────────────────────
-- ENUM TYPES
-- ────────────────────────────────────────────────────────────

create type nutri_origin as enum (
    'smartbmi_hardware',   -- compró la balanza o el kit SmartBMI
    'smartbmi_protocol',   -- compró solo el protocolo post-venta
    'organic',             -- llegó solo, sin campaña
    'campaign'             -- llegó por campaña de marketing
);

create type subscription_type as enum (
    'free_trial',
    'invitation',          -- invitado por SmartBMI (beta testers, partners)
    'monthly',
    'quarterly',
    'annual'
);

create type subscription_status as enum (
    'active',
    'expired',
    'cancelled',
    'pending_payment'
);

create type delivery_channel as enum ('whatsapp', 'email', 'manual');
create type delivery_status  as enum ('pending', 'sent', 'failed', 'delivered');
create type scrape_status    as enum ('ok', 'login_failed', 'timeout', 'no_data', 'pending');


-- ────────────────────────────────────────────────────────────
-- TABLA: nutris
-- El Nutri es el cliente de SmartTanita.
-- Se autentica con Supabase Auth (auth.users).
-- ────────────────────────────────────────────────────────────

create table public.nutris (
    id                  uuid primary key references auth.users(id) on delete cascade,
    full_name           text not null,
    email               text not null unique,
    phone               text,

    -- Origen del cliente
    origin              nutri_origin not null default 'organic',
    campaign_source     text,                         -- utm_source / nombre campaña

    -- Suscripción
    subscription_type   subscription_type not null default 'free_trial',
    subscription_status subscription_status not null default 'active',
    max_patients        integer not null default 5,   -- límite de pacientes activos
    max_reports_month   integer not null default 10,  -- límite de reportes por mes
    subscription_start  date,
    subscription_end    date,                         -- null = sin expiración (invitation)

    -- Contadores (se actualizan vía trigger/función)
    reports_this_month  integer not null default 0,
    reports_total       integer not null default 0,
    reports_month_reset date,                         -- fecha del último reset mensual

    -- Metadata
    notes               text,                         -- notas internas SmartBMI
    role                text not null default 'user' check (role in ('user', 'admin')),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- Comentarios
comment on column public.nutris.origin is 'Cómo llegó el Nutri a SmartTanita';
comment on column public.nutris.max_patients is 'Límite de pacientes activos según plan';
comment on column public.nutris.max_reports_month is 'Reportes incluidos por mes según plan';
comment on column public.nutris.reports_this_month is 'Contador reset el 1ro de cada mes';


-- ────────────────────────────────────────────────────────────
-- TABLA: patients
-- Cada paciente pertenece a un Nutri.
-- ────────────────────────────────────────────────────────────

create table public.patients (
    id              uuid primary key default gen_random_uuid(),
    nutri_id        uuid not null references public.nutris(id) on delete cascade,
    full_name       text not null,
    date_of_birth   date,
    sex             char(1) check (sex in ('F', 'M')),
    height_cm       numeric(5,1),
    phone_whatsapp  text,                             -- destino de envío del reporte
    is_active       boolean not null default true,
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on column public.patients.is_active is 'Pacientes inactivos no cuentan contra el límite del plan';


-- ────────────────────────────────────────────────────────────
-- TABLA: tanita_credentials
-- Credenciales MyTanita por paciente.
-- Password encriptada con AES-256 via pgcrypto.
-- La clave de encriptación (ENCRYPTION_KEY) es una variable
-- de entorno en el servidor — nunca en Supabase.
-- ────────────────────────────────────────────────────────────

create table public.tanita_credentials (
    id                  uuid primary key default gen_random_uuid(),
    patient_id          uuid not null unique references public.patients(id) on delete cascade,
    tanita_email        text not null,
    tanita_password_enc text not null,                -- AES-256 encriptado en el backend
    last_scrape_status  scrape_status not null default 'pending',
    last_scraped_at     timestamptz,
    last_error_msg      text,                         -- mensaje de error del último intento
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

comment on column public.tanita_credentials.tanita_password_enc
    is 'AES-256 encriptado por el backend. La clave nunca está en Supabase.';


-- ────────────────────────────────────────────────────────────
-- TABLA: reports
-- Un reporte = una medición analizada + PDF generado.
-- ────────────────────────────────────────────────────────────

create table public.reports (
    id               uuid primary key default gen_random_uuid(),
    patient_id       uuid not null references public.patients(id) on delete cascade,
    nutri_id         uuid not null references public.nutris(id),   -- desnormalizado para RLS simple

    -- Datos de la medición (para búsquedas y resúmenes sin reparsear el CSV)
    measurement_date timestamptz not null,
    weight_kg        numeric(5,2),
    body_fat_pct     numeric(4,1),
    muscle_mass_kg   numeric(5,2),
    visceral_fat     numeric(4,1),
    bmr_kcal         numeric(6,1),
    metabolic_age    integer,

    -- Datos crudos y archivos
    csv_raw          text,                            -- CSV completo descargado de MyTanita
    pdf_storage_path text,                            -- path en Supabase Storage: reports/{nutri_id}/{report_id}.pdf

    -- Metadata de generación
    generated_at     timestamptz not null default now(),
    generation_secs  numeric(5,2)                     -- tiempo que tardó el pipeline
);

comment on column public.reports.nutri_id
    is 'Desnormalizado desde patient para simplificar las políticas RLS';
comment on column public.reports.csv_raw
    is 'CSV completo de MyTanita. Permite regenerar el reporte sin re-scraping.';
comment on column public.reports.pdf_storage_path
    is 'Path relativo en Supabase Storage bucket "reports"';


-- ────────────────────────────────────────────────────────────
-- TABLA: report_deliveries
-- Registro de envíos de cada reporte.
-- ────────────────────────────────────────────────────────────

create table public.report_deliveries (
    id          uuid primary key default gen_random_uuid(),
    report_id   uuid not null references public.reports(id) on delete cascade,
    channel     delivery_channel not null default 'whatsapp',
    recipient   text not null,                        -- número WA o email
    status      delivery_status not null default 'pending',
    error_msg   text,
    sent_at     timestamptz,
    delivered_at timestamptz,
    created_at  timestamptz not null default now()
);


-- ────────────────────────────────────────────────────────────
-- ÍNDICES
-- ────────────────────────────────────────────────────────────

create index idx_patients_nutri         on public.patients(nutri_id);
create index idx_patients_active        on public.patients(nutri_id, is_active);
create index idx_reports_patient        on public.reports(patient_id);
create index idx_reports_nutri          on public.reports(nutri_id);
create index idx_reports_date           on public.reports(measurement_date desc);
create index idx_deliveries_report      on public.report_deliveries(report_id);
create index idx_credentials_patient    on public.tanita_credentials(patient_id);


-- ────────────────────────────────────────────────────────────
-- TRIGGER: updated_at automático
-- ────────────────────────────────────────────────────────────

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger trg_nutris_updated_at
    before update on public.nutris
    for each row execute function public.set_updated_at();

create trigger trg_patients_updated_at
    before update on public.patients
    for each row execute function public.set_updated_at();

create trigger trg_credentials_updated_at
    before update on public.tanita_credentials
    for each row execute function public.set_updated_at();


-- ────────────────────────────────────────────────────────────
-- FUNCIÓN: incrementar contador de reportes
-- Llamada desde el backend Python después de generar un PDF.
-- ────────────────────────────────────────────────────────────

create or replace function public.increment_report_count(p_nutri_id uuid)
returns void language plpgsql security definer as $$
declare
    v_nutri public.nutris%rowtype;
    v_today date := current_date;
begin
    select * into v_nutri from public.nutris where id = p_nutri_id;

    -- Reset mensual si cambiamos de mes
    if v_nutri.reports_month_reset is null
       or date_trunc('month', v_nutri.reports_month_reset) < date_trunc('month', v_today)
    then
        update public.nutris
        set reports_this_month = 1,
            reports_total      = reports_total + 1,
            reports_month_reset = v_today
        where id = p_nutri_id;
    else
        update public.nutris
        set reports_this_month = reports_this_month + 1,
            reports_total      = reports_total + 1
        where id = p_nutri_id;
    end if;
end;
$$;

comment on function public.increment_report_count
    is 'Incrementa contadores y hace reset mensual automático. Llamar tras generar cada PDF.';


-- ────────────────────────────────────────────────────────────
-- FUNCIÓN: verificar si el Nutri puede generar más reportes
-- ────────────────────────────────────────────────────────────

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

    -- Reset mensual
    if v_nutri.reports_month_reset is null
       or date_trunc('month', v_nutri.reports_month_reset) < date_trunc('month', v_today)
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


-- ────────────────────────────────────────────────────────────
-- ROW LEVEL SECURITY
-- ────────────────────────────────────────────────────────────

alter table public.nutris              enable row level security;
alter table public.patients            enable row level security;
alter table public.tanita_credentials  enable row level security;
alter table public.reports             enable row level security;
alter table public.report_deliveries   enable row level security;

-- nutris: cada Nutri solo ve su propio registro
create policy "nutri_own" on public.nutris
    for all using (auth.uid() = id);

-- patients: solo el Nutri dueño
create policy "patients_own_nutri" on public.patients
    for all using (
        nutri_id = auth.uid()
    );

-- tanita_credentials: solo accesible via service_role (el backend)
-- Los Nutris NO ven ni editan credenciales directamente desde el frontend
create policy "credentials_service_only" on public.tanita_credentials
    for all using (
        current_setting('role') = 'service_role'
    );

-- reports: el Nutri ve todos los reportes de sus pacientes
create policy "reports_own_nutri" on public.reports
    for all using (
        nutri_id = auth.uid()
    );

-- report_deliveries: via reports del Nutri
create policy "deliveries_own_nutri" on public.report_deliveries
    for all using (
        report_id in (
            select id from public.reports where nutri_id = auth.uid()
        )
    );


-- ────────────────────────────────────────────────────────────
-- STORAGE BUCKET: reports
-- ────────────────────────────────────────────────────────────
-- Ejecutar en el dashboard de Supabase → Storage → New bucket
-- O via SQL:

insert into storage.buckets (id, name, public)
values ('reports', 'reports', false)
on conflict do nothing;

-- Política: el Nutri solo accede a sus propios PDFs
-- (path esperado: reports/{nutri_id}/{report_id}.pdf)
create policy "reports_storage_own"
    on storage.objects for all
    using (
        bucket_id = 'reports'
        and (storage.foldername(name))[1] = auth.uid()::text
    );


-- ────────────────────────────────────────────────────────────
-- DATOS DE EJEMPLO (planes)
-- Referencia para configurar planes al crear Nutris
-- ────────────────────────────────────────────────────────────

/*
Plan         | max_patients | max_reports_month | subscription_type
-------------|--------------|-------------------|------------------
Free Trial   |      3       |         5         | free_trial
Invitación   |     10       |        30         | invitation
Basic        |     15       |        30         | monthly/quarterly/annual
Pro          |     40       |       100         | monthly/quarterly/annual
Clinic       |    150       |       500         | monthly/annual
*/
