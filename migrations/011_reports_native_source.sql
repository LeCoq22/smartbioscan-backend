-- Vincula un reporte con la medición Nativa exacta que lo originó.

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'mytanita';

ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS native_measurement_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'reports_source_chk'
      AND conrelid = 'public.reports'::regclass
  ) THEN
    ALTER TABLE public.reports
      ADD CONSTRAINT reports_source_chk
      CHECK (source IN ('mytanita', 'nativa'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'reports_native_measurement_fk'
      AND conrelid = 'public.reports'::regclass
  ) THEN
    ALTER TABLE public.reports
      ADD CONSTRAINT reports_native_measurement_fk
      FOREIGN KEY (native_measurement_id)
      REFERENCES public.native_measurements(id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS reports_native_measurement_uk
  ON public.reports (native_measurement_id)
  WHERE native_measurement_id IS NOT NULL;
