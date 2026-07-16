-- ============================================================
-- Migration 009: Mediciones nativas (balanza Tanita RD-545 vía app móvil)
--
-- Recibe capturas que la app móvil (nativa_bioscan_mobile) sube por su
-- outbox local-first. La app manda el frame B010 crudo en hex + los campos
-- decodificados + un snapshot del perfil usado para la medición. El backend
-- corre con service_role (bypassa RLS); las policies y la FK compuesta son la
-- red de seguridad si un cliente authenticated toca la tabla directamente.
--
-- IDEMPOTENTE / ADITIVA: segura de correr más de una vez.
--   - CREATE TABLE / INDEX IF NOT EXISTS
--   - constraints agregadas vía DO blocks que chequean pg_constraint
--   - policies dropeadas por nombre antes de recrearse
--   - grants son idempotentes
--
-- Ejecutar en el SQL Editor de Supabase.
-- ============================================================


-- ── 1. patients: unique (id, nutri_id) ───────────────────────
-- Requisito para la FK compuesta de native_measurements. `id` ya es PK
-- (único), así que (id, nutri_id) es trivialmente único: la constraint no
-- puede fallar por duplicados. Sirve como target válido de la FK.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'patients_id_nutri_uk'
      AND conrelid = 'public.patients'::regclass
  ) THEN
    ALTER TABLE public.patients
      ADD CONSTRAINT patients_id_nutri_uk UNIQUE (id, nutri_id);
  END IF;
END $$;


-- ── 2. Tabla native_measurements ─────────────────────────────
-- Los CHECK usan solo funciones IMMUTABLE (regex, length, octet_length,
-- jsonb_typeof, cast jsonb->text). El límite superior de captured_at
-- (now + 1 día) NO se expresa acá porque now() no es IMMUTABLE y Postgres lo
-- rechaza en un CHECK; ese tope se valida en la capa de aplicación
-- (api/native_measurements.py). El piso 2015 sí es un check inmutable.
--
-- received_at es el instante en que el backend recibió/persistió la captura
-- (default now()), independiente de captured_at (instante de la medición en la
-- balanza, provisto por la app). Sirve para auditoría y orden de arribo.

CREATE TABLE IF NOT EXISTS public.native_measurements (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    nutri_id              uuid        NOT NULL,
    patient_id            uuid        NOT NULL,
    idempotency_key       uuid        NOT NULL,
    captured_at           timestamptz NOT NULL,
    source                text        NOT NULL,
    scale_installation_id text        NOT NULL,
    raw_b010_hex          text        NOT NULL,
    decoded_fields        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    parser_version        text        NOT NULL,
    profile_snapshot      jsonb       NOT NULL,
    received_at           timestamptz NOT NULL DEFAULT now(),
    created_at            timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT native_measurements_source_chk
        CHECK (source = 'tanita_rd545'),

    CONSTRAINT native_measurements_parser_chk
        CHECK (parser_version = 'b010-tanita-tags-v2'),

    CONSTRAINT native_measurements_hex_chk
        CHECK (
            raw_b010_hex ~ '^[0-9a-fA-F]+$'
            AND length(raw_b010_hex) % 2 = 0
            AND length(raw_b010_hex) BETWEEN 2 AND 8192
        ),

    CONSTRAINT native_measurements_decoded_object_chk
        CHECK (jsonb_typeof(decoded_fields) = 'object'),

    CONSTRAINT native_measurements_decoded_size_chk
        CHECK (octet_length(decoded_fields::text) <= 65536),

    CONSTRAINT native_measurements_profile_object_chk
        CHECK (jsonb_typeof(profile_snapshot) = 'object'),

    CONSTRAINT native_measurements_profile_size_chk
        CHECK (octet_length(profile_snapshot::text) <= 8192),

    CONSTRAINT native_measurements_captured_at_chk
        CHECK (captured_at >= timestamptz '2015-01-01 00:00:00+00'),

    CONSTRAINT native_measurements_nutri_idem_uk
        UNIQUE (nutri_id, idempotency_key)
);


-- ── 2b. Columnas / checks aditivos para tablas ya existentes ──
-- CREATE TABLE IF NOT EXISTS no agrega columnas ni constraints si la tabla ya
-- existe. Estos bloques (idempotentes, filtrados por conrelid) garantizan que
-- una instalación previa reciba received_at y los nuevos checks de objeto/tamaño
-- JSON sin fallar al re-ejecutar la migración.

ALTER TABLE public.native_measurements
    ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'native_measurements_decoded_object_chk'
      AND conrelid = 'public.native_measurements'::regclass
  ) THEN
    ALTER TABLE public.native_measurements
      ADD CONSTRAINT native_measurements_decoded_object_chk
      CHECK (jsonb_typeof(decoded_fields) = 'object');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'native_measurements_profile_object_chk'
      AND conrelid = 'public.native_measurements'::regclass
  ) THEN
    ALTER TABLE public.native_measurements
      ADD CONSTRAINT native_measurements_profile_object_chk
      CHECK (jsonb_typeof(profile_snapshot) = 'object');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'native_measurements_profile_size_chk'
      AND conrelid = 'public.native_measurements'::regclass
  ) THEN
    ALTER TABLE public.native_measurements
      ADD CONSTRAINT native_measurements_profile_size_chk
      CHECK (octet_length(profile_snapshot::text) <= 8192);
  END IF;
END $$;


-- ── 3. FK compuesta (patient_id, nutri_id) → patients ────────
-- Garantiza que el paciente pertenece al nutri: imposible cruzar una medición
-- al paciente de otro nutri, incluso ante una carrera con el chequeo de la app.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'native_measurements_patient_fk'
      AND conrelid = 'public.native_measurements'::regclass
  ) THEN
    ALTER TABLE public.native_measurements
      ADD CONSTRAINT native_measurements_patient_fk
      FOREIGN KEY (patient_id, nutri_id)
      REFERENCES public.patients (id, nutri_id)
      ON DELETE CASCADE;
  END IF;
END $$;


-- ── 4. Índices ───────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_native_measurements_patient
    ON public.native_measurements (patient_id);

CREATE INDEX IF NOT EXISTS idx_native_measurements_nutri_captured
    ON public.native_measurements (nutri_id, captured_at DESC);

-- Historial por paciente ordenado por instante de medición (más reciente
-- primero): sirve las consultas de evolución/timeline de un paciente.
CREATE INDEX IF NOT EXISTS idx_native_measurements_patient_captured
    ON public.native_measurements (patient_id, captured_at DESC);


-- ── 5. RLS + policies ────────────────────────────────────────
-- Sin auth.role(): TO authenticated + auth.uid(). Sin policy DELETE.
-- auth.uid() se envuelve en (select auth.uid()) para que Postgres lo evalúe
-- una sola vez por query (initplan) en vez de por fila — best practice de RLS
-- de Supabase.

ALTER TABLE public.native_measurements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "native_measurements_select_own" ON public.native_measurements;
CREATE POLICY "native_measurements_select_own"
  ON public.native_measurements
  FOR SELECT
  TO authenticated
  USING (nutri_id = (select auth.uid()));

DROP POLICY IF EXISTS "native_measurements_insert_own" ON public.native_measurements;
CREATE POLICY "native_measurements_insert_own"
  ON public.native_measurements
  FOR INSERT
  TO authenticated
  WITH CHECK (nutri_id = (select auth.uid()));

DROP POLICY IF EXISTS "native_measurements_update_own" ON public.native_measurements;
CREATE POLICY "native_measurements_update_own"
  ON public.native_measurements
  FOR UPDATE
  TO authenticated
  USING (nutri_id = (select auth.uid()))
  WITH CHECK (nutri_id = (select auth.uid()));


-- ── 6. Grants mínimos ────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE ON public.native_measurements TO authenticated;


-- ── 7. Verificación post-aplicación (opcional) ───────────────
-- SELECT relname, relrowsecurity FROM pg_class WHERE relname = 'native_measurements';
-- SELECT tablename, policyname, cmd, roles FROM pg_policies
--   WHERE tablename = 'native_measurements' ORDER BY policyname;
