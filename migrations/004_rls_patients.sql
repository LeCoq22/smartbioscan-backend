-- ============================================================
-- Migration 004: RLS en patients y tanita_credentials
-- Ejecutar en Supabase SQL Editor.
-- Asegura que cada nutri solo accede a sus propios pacientes.
-- ============================================================

-- ── patients ──────────────────────────────────────────────────

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;

CREATE POLICY "nutris_ven_sus_patients"
  ON patients FOR SELECT
  USING (nutri_id = auth.uid());

CREATE POLICY "nutris_insertan_sus_patients"
  ON patients FOR INSERT
  WITH CHECK (nutri_id = auth.uid());

CREATE POLICY "nutris_actualizan_sus_patients"
  ON patients FOR UPDATE
  USING (nutri_id = auth.uid())
  WITH CHECK (nutri_id = auth.uid());

-- Soft delete pasa por función RPC con SECURITY DEFINER, no necesita DELETE policy.
-- Si se quiere borrado directo: descomentar la siguiente línea.
-- CREATE POLICY "nutris_borran_sus_patients" ON patients FOR DELETE USING (nutri_id = auth.uid());


-- ── tanita_credentials ────────────────────────────────────────

ALTER TABLE tanita_credentials ENABLE ROW LEVEL SECURITY;

CREATE POLICY "nutris_ven_sus_credentials"
  ON tanita_credentials FOR SELECT
  USING (
    patient_id IN (
      SELECT id FROM patients WHERE nutri_id = auth.uid()
    )
  );

CREATE POLICY "nutris_insertan_sus_credentials"
  ON tanita_credentials FOR INSERT
  WITH CHECK (
    patient_id IN (
      SELECT id FROM patients WHERE nutri_id = auth.uid()
    )
  );

CREATE POLICY "nutris_actualizan_sus_credentials"
  ON tanita_credentials FOR UPDATE
  USING (
    patient_id IN (
      SELECT id FROM patients WHERE nutri_id = auth.uid()
    )
  );

CREATE POLICY "nutris_borran_sus_credentials"
  ON tanita_credentials FOR DELETE
  USING (
    patient_id IN (
      SELECT id FROM patients WHERE nutri_id = auth.uid()
    )
  );
