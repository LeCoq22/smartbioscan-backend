-- Migration 006: waitlist + beta_invites + nuevos valores de enum
--
-- ⚠️  ALTER TYPE ... ADD VALUE no puede ejecutarse dentro de una transacción.
--     Ejecutá este bloque ANTES de iniciar cualquier BEGIN/COMMIT,
--     o correlo directo en el SQL Editor de Supabase (que ejecuta sin transacción).
-- ────────────────────────────────────────────────────────────────────────────

-- Nuevos valores de enum (idempotentes)
ALTER TYPE subscription_type ADD VALUE IF NOT EXISTS 'beta';
ALTER TYPE nutri_origin      ADD VALUE IF NOT EXISTS 'waitlist';
ALTER TYPE nutri_origin      ADD VALUE IF NOT EXISTS 'manual';

-- ── waitlist ─────────────────────────────────────────────────────────────────
-- Puede que ya exista desde el flujo anterior (insert directo a Supabase).
-- Usamos IF NOT EXISTS + ADD COLUMN IF NOT EXISTS para idempotencia.

CREATE TABLE IF NOT EXISTS public.waitlist (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    nombre      text        NOT NULL,
    email       text        NOT NULL UNIQUE,
    profesion   text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.waitlist ADD COLUMN IF NOT EXISTS status       text        NOT NULL DEFAULT 'pending';
ALTER TABLE public.waitlist ADD COLUMN IF NOT EXISTS approved_at  timestamptz;
ALTER TABLE public.waitlist ADD COLUMN IF NOT EXISTS approved_by  uuid REFERENCES public.nutris(id);
ALTER TABLE public.waitlist ADD COLUMN IF NOT EXISTS rejected_at  timestamptz;
ALTER TABLE public.waitlist ADD COLUMN IF NOT EXISTS rejected_reason text;

-- Constraint de status (idempotente: drop antes de add)
ALTER TABLE public.waitlist DROP CONSTRAINT IF EXISTS waitlist_status_check;
ALTER TABLE public.waitlist ADD CONSTRAINT waitlist_status_check
    CHECK (status IN ('pending', 'approved', 'rejected'));

-- RLS: sólo permite inserción anónima; lecturas/updates sólo via service key
ALTER TABLE public.waitlist ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert" ON public.waitlist;
CREATE POLICY "anon_insert" ON public.waitlist
    FOR INSERT TO anon WITH CHECK (true);

-- ── beta_invites ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.beta_invites (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    nutri_id    uuid        NOT NULL REFERENCES public.nutris(id) ON DELETE CASCADE,
    token       text        NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_beta_invites_token ON public.beta_invites(token);
CREATE INDEX IF NOT EXISTS idx_beta_invites_nutri ON public.beta_invites(nutri_id);

ALTER TABLE public.beta_invites ENABLE ROW LEVEL SECURITY;
-- Sin policies = sólo service key puede acceder

-- ── comentarios ──────────────────────────────────────────────────────────────
COMMENT ON TABLE  public.waitlist                IS 'Solicitudes de acceso desde la landing';
COMMENT ON COLUMN public.waitlist.status         IS 'pending | approved | rejected';
COMMENT ON TABLE  public.beta_invites            IS 'Tokens de activación de cuenta para beta testers';
COMMENT ON COLUMN public.beta_invites.used_at    IS 'NULL = link disponible; NOT NULL = ya usado';
