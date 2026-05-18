-- ============================================================
-- Migración: MercadoPago Subscriptions
-- Ejecutar en el SQL Editor de Supabase.
-- ============================================================

-- Agregar 'semestral' al enum (idempotente gracias a IF NOT EXISTS)
ALTER TYPE subscription_type ADD VALUE IF NOT EXISTS 'semestral';

-- Nuevas columnas en nutris para Subscriptions/Pre-aprobados de MP
ALTER TABLE public.nutris
    ADD COLUMN IF NOT EXISTS mp_preapproval_id              text UNIQUE,
    ADD COLUMN IF NOT EXISTS mp_payer_id                    text,
    ADD COLUMN IF NOT EXISTS subscription_cancelled_at      timestamptz,
    ADD COLUMN IF NOT EXISTS subscription_next_billing_date timestamptz;

COMMENT ON COLUMN public.nutris.mp_preapproval_id              IS 'ID del preapproval activo en MercadoPago';
COMMENT ON COLUMN public.nutris.mp_payer_id                    IS 'payer_id del comprador en MercadoPago';
COMMENT ON COLUMN public.nutris.subscription_cancelled_at      IS 'Fecha de cancelación (el acceso sigue hasta subscription_end)';
COMMENT ON COLUMN public.nutris.subscription_next_billing_date IS 'Fecha del próximo cobro automático según MP';
