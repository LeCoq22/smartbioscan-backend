-- ════════════════════════════════════════════════════════════════════
-- Tabla: frontend_errors
-- Captura excepciones JS no manejadas del frontend para debug.
-- Pensada para escribirse desde un endpoint público del backend
-- (sin auth) porque los errores pueden ocurrir antes del login.
-- ════════════════════════════════════════════════════════════════════

create table if not exists public.frontend_errors (
  id          uuid        primary key default gen_random_uuid(),
  created_at  timestamptz not null    default now(),

  -- Identidad (opcional — puede ser null si el error fue pre-login)
  nutri_id    uuid        null references public.nutris(id) on delete set null,

  -- Contexto del error
  url         text        null,         -- Página donde ocurrió
  user_agent  text        null,         -- Navegador / OS
  message     text        not null,     -- Mensaje del error
  stack       text        null,         -- Stack trace (puede ser largo)
  source      text        null,         -- 'onerror' / 'unhandledrejection' / manual

  -- Contexto adicional opcional (JSON libre)
  context     jsonb       null
);

-- Índice por fecha para query rápida de errores recientes
create index if not exists idx_frontend_errors_created_at
  on public.frontend_errors (created_at desc);

-- Índice por nutri para filtrar errores de un usuario específico
create index if not exists idx_frontend_errors_nutri_id
  on public.frontend_errors (nutri_id, created_at desc);

-- ───────────────────────────────────────────────────────────────────
-- RLS: solo admin puede leer. Inserciones vienen del backend (service role).
-- ───────────────────────────────────────────────────────────────────
alter table public.frontend_errors enable row level security;

drop policy if exists "Admin can read frontend errors" on public.frontend_errors;
create policy "Admin can read frontend errors"
  on public.frontend_errors
  for select
  using (
    exists (
      select 1 from public.nutris
      where nutris.id = auth.uid() and nutris.role = 'admin'
    )
  );

-- Inserciones bloqueadas vía RLS (solo el backend con service role).
