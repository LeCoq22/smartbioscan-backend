-- ============================================================
-- Migration 007: Habilitar RLS en patients y tanita_credentials
-- + reescribir policies con cláusulas verificadas
--
-- CONTEXTO: RLS estaba deshabilitado en ambas tablas.
-- Las policies existentes (patients_own_nutri, credentials_service_only)
-- estaban escritas pero inactivas. Este script habilita RLS y
-- reemplaza las policies con versiones verificadas.
--
-- SEGURO DE CORRER: las políticas se dropean por nombre antes de
-- recrearse, así que no hay conflictos si el script se corre más
-- de una vez (idempotente).
-- ============================================================


-- ── 1. patients ───────────────────────────────────────────────

ALTER TABLE public.patients ENABLE ROW LEVEL SECURITY;

-- Limpiar cualquier policy preexistente (correcta o incorrecta)
DROP POLICY IF EXISTS "patients_own_nutri"               ON public.patients;
DROP POLICY IF EXISTS "nutris_ven_sus_patients"          ON public.patients;
DROP POLICY IF EXISTS "nutris_insertan_sus_patients"     ON public.patients;
DROP POLICY IF EXISTS "nutris_actualizan_sus_patients"   ON public.patients;
DROP POLICY IF EXISTS "nutris_borran_sus_patients"       ON public.patients;

-- SELECT: cada nutri solo ve sus propios pacientes
CREATE POLICY "nutris_ven_sus_patients"
  ON public.patients FOR SELECT
  USING (nutri_id = auth.uid());

-- INSERT: solo puede insertar pacientes propios
CREATE POLICY "nutris_insertan_sus_patients"
  ON public.patients FOR INSERT
  WITH CHECK (nutri_id = auth.uid());

-- UPDATE: solo puede editar sus propios pacientes
CREATE POLICY "nutris_actualizan_sus_patients"
  ON public.patients FOR UPDATE
  USING (nutri_id = auth.uid())
  WITH CHECK (nutri_id = auth.uid());

-- Nota: el soft-delete pasa por la función RPC soft_delete_patient
-- con SECURITY DEFINER, no necesita policy DELETE en el cliente.


-- ── 2. tanita_credentials ────────────────────────────────────

ALTER TABLE public.tanita_credentials ENABLE ROW LEVEL SECURITY;

-- Limpiar cualquier policy preexistente
DROP POLICY IF EXISTS "credentials_service_only"          ON public.tanita_credentials;
DROP POLICY IF EXISTS "nutris_ven_sus_credentials"        ON public.tanita_credentials;
DROP POLICY IF EXISTS "nutris_insertan_sus_credentials"   ON public.tanita_credentials;
DROP POLICY IF EXISTS "nutris_actualizan_sus_credentials" ON public.tanita_credentials;
DROP POLICY IF EXISTS "nutris_borran_sus_credentials"     ON public.tanita_credentials;

-- SELECT: el nutri puede ver las credentials de sus propios pacientes
-- (necesario para mostrar estado Tanita en el dashboard de pacientes)
CREATE POLICY "nutris_ven_sus_credentials"
  ON public.tanita_credentials FOR SELECT
  USING (
    patient_id IN (
      SELECT id FROM public.patients WHERE nutri_id = auth.uid()
    )
  );

-- INSERT: solo puede agregar credentials a sus propios pacientes
CREATE POLICY "nutris_insertan_sus_credentials"
  ON public.tanita_credentials FOR INSERT
  WITH CHECK (
    patient_id IN (
      SELECT id FROM public.patients WHERE nutri_id = auth.uid()
    )
  );

-- UPDATE: solo puede modificar credentials de sus propios pacientes
CREATE POLICY "nutris_actualizan_sus_credentials"
  ON public.tanita_credentials FOR UPDATE
  USING (
    patient_id IN (
      SELECT id FROM public.patients WHERE nutri_id = auth.uid()
    )
  );

-- DELETE: solo puede borrar credentials de sus propios pacientes
CREATE POLICY "nutris_borran_sus_credentials"
  ON public.tanita_credentials FOR DELETE
  USING (
    patient_id IN (
      SELECT id FROM public.patients WHERE nutri_id = auth.uid()
    )
  );


-- ── 3. Verificación post-aplicación ──────────────────────────
-- Correr esto después para confirmar que RLS quedó activa:

-- SELECT relname, relrowsecurity
-- FROM pg_class
-- WHERE relname IN ('patients', 'tanita_credentials');
-- Debe mostrar relrowsecurity = true para ambas.

-- SELECT tablename, policyname, cmd, roles, qual
-- FROM pg_policies
-- WHERE tablename IN ('patients', 'tanita_credentials')
-- ORDER BY tablename, policyname;
-- Debe mostrar 3 policies para patients y 4 para tanita_credentials.
