-- Agregar columna para trackear si ya se mandó el recordatorio (Email D)
ALTER TABLE public.beta_invites
ADD COLUMN reminder_sent_at TIMESTAMP WITH TIME ZONE NULL;

COMMENT ON COLUMN public.beta_invites.reminder_sent_at IS
  'Cuando se envió el Email D (recordatorio de invite por expirar). NULL = no se envió aún.';

-- Índice parcial para acelerar la query del endpoint de preview
-- (solo los que NO tienen reminder y NO están usados)
CREATE INDEX IF NOT EXISTS idx_beta_invites_pending_reminder
  ON public.beta_invites (expires_at)
  WHERE reminder_sent_at IS NULL AND used_at IS NULL;
