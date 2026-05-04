-- Migration 005: campo role en tabla nutris
-- Distingue usuarios normales de admins de SmartBioScan

ALTER TABLE public.nutris
  ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'user'
    CHECK (role IN ('user', 'admin'));

COMMENT ON COLUMN public.nutris.role IS 'Rol interno: user = nutricionista normal, admin = operador SmartBioScan';

-- Marcar a cremon@gmail.com como admin
UPDATE public.nutris
SET    role = 'admin'
WHERE  email = 'cremon@gmail.com';
