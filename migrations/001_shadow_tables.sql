-- ============================================================
-- Migration 001: Shadow tables para soft delete
-- Ejecutar en Supabase SQL Editor
-- ============================================================

-- Tabla espejo de patients
CREATE TABLE IF NOT EXISTS pacientes_eliminados (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  original_id     uuid        NOT NULL,
  nutri_id        uuid        NOT NULL,
  full_name       text,
  sex             char(1),
  height_cm       numeric,
  phone_whatsapp  text,
  is_active       boolean,
  date_of_birth   date,
  mytanita_profile_id text,
  notes           text,
  created_at      timestamptz,
  updated_at      timestamptz,
  deleted_at      timestamptz NOT NULL DEFAULT now(),
  deleted_by      uuid        NOT NULL  -- nutri_id del que dio de baja
);

-- Tabla espejo de reports
CREATE TABLE IF NOT EXISTS reportes_eliminados (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  original_id         uuid        NOT NULL,
  patient_id          uuid        NOT NULL,
  nutri_id            uuid        NOT NULL,
  measurement_date    date,
  weight_kg           numeric,
  body_fat_pct        numeric,
  muscle_mass_kg      numeric,
  visceral_fat        numeric,
  bmr_kcal            numeric,
  metabolic_age       integer,
  csv_raw             text,
  pdf_storage_path    text,
  generation_secs     numeric,
  generated_at        timestamptz,
  deleted_at          timestamptz NOT NULL DEFAULT now(),
  deleted_by          uuid        NOT NULL
);

-- Índices para consultas futuras (pantalla de Papelera)
CREATE INDEX IF NOT EXISTS pacientes_eliminados_nutri_idx ON pacientes_eliminados(nutri_id, deleted_at DESC);
CREATE INDEX IF NOT EXISTS reportes_eliminados_nutri_idx  ON reportes_eliminados(nutri_id, deleted_at DESC);
CREATE INDEX IF NOT EXISTS reportes_eliminados_patient_idx ON reportes_eliminados(patient_id);

-- ============================================================
-- Función atómica de soft delete
-- Copia paciente + reportes a shadow tables y borra originales.
-- Toda la operación corre en una sola transacción implícita de PL/pgSQL.
-- ============================================================
CREATE OR REPLACE FUNCTION soft_delete_patient(
  p_patient_id uuid,
  p_deleted_by  uuid
) RETURNS jsonb AS $$
DECLARE
  v_report_count integer;
BEGIN
  -- Verificar que el paciente existe
  IF NOT EXISTS (SELECT 1 FROM patients WHERE id = p_patient_id) THEN
    RAISE EXCEPTION 'Paciente % no encontrado', p_patient_id;
  END IF;

  -- 1. Copiar el paciente a la shadow table
  INSERT INTO pacientes_eliminados (
    original_id, nutri_id, full_name, sex, height_cm,
    phone_whatsapp, is_active, date_of_birth, mytanita_profile_id,
    notes, created_at, updated_at, deleted_by
  )
  SELECT id, nutri_id, full_name, sex, height_cm,
         phone_whatsapp, is_active, date_of_birth, mytanita_profile_id,
         notes, created_at, updated_at, p_deleted_by
  FROM patients
  WHERE id = p_patient_id;

  -- 2. Copiar los reportes del paciente a la shadow table
  INSERT INTO reportes_eliminados (
    original_id, patient_id, nutri_id, measurement_date, weight_kg,
    body_fat_pct, muscle_mass_kg, visceral_fat, bmr_kcal, metabolic_age,
    csv_raw, pdf_storage_path, generation_secs, generated_at, deleted_by
  )
  SELECT id, patient_id, nutri_id, measurement_date, weight_kg,
         body_fat_pct, muscle_mass_kg, visceral_fat, bmr_kcal, metabolic_age,
         csv_raw, pdf_storage_path, generation_secs, generated_at, p_deleted_by
  FROM reports
  WHERE patient_id = p_patient_id;

  GET DIAGNOSTICS v_report_count = ROW_COUNT;

  -- 3. Borrar los reportes originales
  DELETE FROM reports WHERE patient_id = p_patient_id;

  -- 4. Borrar las credenciales Tanita del paciente
  DELETE FROM tanita_credentials WHERE patient_id = p_patient_id;

  -- 5. Borrar el paciente original
  DELETE FROM patients WHERE id = p_patient_id;

  RETURN jsonb_build_object(
    'ok', true,
    'reports_deleted', v_report_count
  );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
