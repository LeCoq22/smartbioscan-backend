-- ============================================================
-- Migration 003: Agregar mytanita_profile_id a patients
-- Ejecutar en Supabase SQL Editor.
-- ============================================================


ALTER TABLE patients
  ADD COLUMN IF NOT EXISTS mytanita_profile_id text;

CREATE INDEX IF NOT EXISTS patients_mytanita_profile_id_idx
  ON patients(mytanita_profile_id);

COMMENT ON COLUMN patients.mytanita_profile_id IS
  'ID numérico del perfil en mytanita.eu (ej: "289049"). '
  'Permite hacer switch al perfil correcto en cuentas multi-usuario.';
