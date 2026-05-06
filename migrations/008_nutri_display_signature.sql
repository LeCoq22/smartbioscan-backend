-- ============================================================
-- Migration 008: Agrega display_signature a nutris
--
-- Campo de texto libre (máx 120 chars) que el nutri puede editar
-- para personalizar la línea que aparece en sus reportes PDF.
-- Sin prefijo fijo — se muestra tal cual.
--
-- NOTA: la tabla nutris NO tiene columna "profesion" (ese dato vive
-- en waitlist). El backfill usa JOIN con waitlist + fallback a notes.
-- ============================================================

ALTER TABLE public.nutris
  ADD COLUMN display_signature text;

ALTER TABLE public.nutris
  ADD CONSTRAINT nutris_display_signature_length
  CHECK (char_length(display_signature) <= 120);


-- ── Backfill paso 1: nutris que vinieron por waitlist ────────
-- JOIN por email para obtener la profesion del form de inscripción.
UPDATE public.nutris n
SET display_signature =
  n.full_name ||
  CASE
    WHEN w.profesion = 'nutricionista' THEN ' - Nutricionista'
    WHEN w.profesion = 'medico'        THEN ' - Médico clínico'
    WHEN w.profesion = 'entrenador'    THEN ' - Entrenador personal'
    WHEN w.profesion = 'otro'          THEN ' - Profesional de salud'
    ELSE ''
  END
FROM public.waitlist w
WHERE n.email = w.email
  AND n.display_signature IS NULL
  AND n.full_name IS NOT NULL;


-- ── Backfill paso 2: nutris sin entrada en waitlist ──────────
-- Intenta parsear desde el campo notes (formato: "Profesión: <valor>").
-- Fallback: solo full_name.
UPDATE public.nutris
SET display_signature =
  CASE
    WHEN notes = 'Profesión: nutricionista' THEN full_name || ' - Nutricionista'
    WHEN notes = 'Profesión: medico'        THEN full_name || ' - Médico clínico'
    WHEN notes = 'Profesión: entrenador'    THEN full_name || ' - Entrenador personal'
    WHEN notes = 'Profesión: otro'          THEN full_name || ' - Profesional de salud'
    ELSE full_name
  END
WHERE display_signature IS NULL
  AND full_name IS NOT NULL;


-- ── Verificación post-aplicación ─────────────────────────────
-- SELECT email, full_name, display_signature FROM nutris LIMIT 10;
