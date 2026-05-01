-- ============================================================
-- Migration 002: Tabla patient_csvs
-- Una fila por fecha de medición por paciente.
-- Ejecutar en Supabase SQL Editor.
-- ============================================================

CREATE TABLE IF NOT EXISTS patient_csvs (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id       uuid        NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  nutri_id         uuid        NOT NULL,
  measurement_date date        NOT NULL,
  raw_data         jsonb       NOT NULL DEFAULT '{}',
  report_generated boolean     NOT NULL DEFAULT false,
  report_id        uuid        REFERENCES reports(id) ON DELETE SET NULL,
  scraped_at       timestamptz NOT NULL DEFAULT now(),

  UNIQUE(patient_id, measurement_date)
);

CREATE INDEX IF NOT EXISTS patient_csvs_patient_date_idx ON patient_csvs(patient_id, measurement_date DESC);
CREATE INDEX IF NOT EXISTS patient_csvs_nutri_idx        ON patient_csvs(nutri_id);
CREATE INDEX IF NOT EXISTS patient_csvs_report_gen_idx   ON patient_csvs(patient_id, report_generated);
