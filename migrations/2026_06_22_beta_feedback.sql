-- Migration: beta_feedback
--
-- Tabla para registrar la respuesta de un clic de los nutris beta que se
-- inscribieron pero nunca cargaron un paciente. Llegan desde un mail con 4
-- botones; cada botón abre /feedback?r=<reason>&n=<nutri_id> en el frontend,
-- que inserta directo a Supabase (sin endpoint backend).

CREATE TABLE IF NOT EXISTS public.beta_feedback (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    nutri_id    uuid        REFERENCES public.nutris(id) ON DELETE SET NULL,
    reason      text        NOT NULL CHECK (reason IN ('sin_bascula','no_entendi','sin_tiempo','otra')),
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.beta_feedback ENABLE ROW LEVEL SECURITY;

-- Insert abierto a anon: el nutri llega desde el mail sin sesión.
DROP POLICY IF EXISTS "beta_feedback_insert_public" ON public.beta_feedback;
CREATE POLICY "beta_feedback_insert_public"
    ON public.beta_feedback
    FOR INSERT
    TO anon, authenticated
    WITH CHECK (true);

-- Update abierto a anon: la página agrega `detail` al registro recién creado.
DROP POLICY IF EXISTS "beta_feedback_update_public" ON public.beta_feedback;
CREATE POLICY "beta_feedback_update_public"
    ON public.beta_feedback
    FOR UPDATE
    TO anon, authenticated
    USING (true)
    WITH CHECK (true);

-- Lectura sólo admin via is_admin() (patrón confirmado del proyecto).
DROP POLICY IF EXISTS "beta_feedback_admin_select" ON public.beta_feedback;
CREATE POLICY "beta_feedback_admin_select"
    ON public.beta_feedback
    FOR SELECT
    TO authenticated
    USING (public.is_admin());

COMMENT ON TABLE  public.beta_feedback        IS 'Respuestas de un clic de nutris beta inactivos (mail de feedback)';
COMMENT ON COLUMN public.beta_feedback.reason IS 'sin_bascula | no_entendi | sin_tiempo | otra';
COMMENT ON COLUMN public.beta_feedback.detail IS 'Texto libre opcional ampliando la respuesta';
