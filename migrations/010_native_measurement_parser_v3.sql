-- Acepta el parser v3 sin invalidar las mediciones v2 ya persistidas.
-- v3 corrige los scores Tanita sign-magnitude y agrega PhysiqueRating derivado.

ALTER TABLE public.native_measurements
  DROP CONSTRAINT IF EXISTS native_measurements_parser_chk;

ALTER TABLE public.native_measurements
  ADD CONSTRAINT native_measurements_parser_chk
  CHECK (parser_version IN ('b010-tanita-tags-v2', 'b010-tanita-tags-v3'));
