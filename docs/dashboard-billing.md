# Dashboard: cambios de plan y paquetes

Implementación recuperada el 01/11/2026 de los worktrees del 15/10/2026, en la rama `codex/billing-plan-management` de ambos repositorios. La migración y los despliegues se publicaron el 02/11/2026, con autorización explícita de Claudio para activar producción. No se generaron cargos reales durante las pruebas.

**Actualización 02/11/2026:** la prueba de sandbox confirmó que Mercado Pago
ignora la nueva frecuencia y fecha al actualizar un contrato existente. Se
reemplazó ese mecanismo por una nueva suscripción pendiente, autorizada por el
comprador en el checkout oficial. La autorización completa conservó importe,
frecuencia y fecha. Ver [evidencia](mp-subscription-sandbox-20261102.md).

## Reglas implementadas

- Básico → Plus: pago único del nuevo plan. Tras confirmación del pago se pausa el débito anterior. Si el cupo base está agotado, activa el cupo completo; si queda cupo base, espera a agotarlo o al fin del ciclo. No exige gastar los créditos comprados ni administrativos: ambos se conservan. Al activar, prepara una nueva autorización de débito con inicio en el nuevo vencimiento. El panel pide autorizarla; el contrato anterior sigue pausado. Solo después de comprobar por GET estado autorizado, importe, moneda, frecuencia y fecha se cancela el anterior y se sustituye su identificador. Un fallo conserva el trabajo pendiente para reintentar sin cobrar nuevamente el upgrade.
- Plus → Básico: programa la nueva tarifa para el vencimiento de la suscripción pagada; conserva el plan actual hasta esa fecha. La clienta autoriza un contrato nuevo con inicio al vencimiento; entonces se pausa el anterior. La activación exige un cobro aprobado del nuevo período, no sólo autorización de la suscripción. Tras ese cobro se cancela el contrato anterior. El semestral conserva su vencimiento de seis meses.
- Cambio de periodicidad sin subir de categoría: se programa al vencimiento, sin cobro inmediato.
- Paquetes de 10: pago único, se acreditan una vez y no vencen. Conservan su saldo al renovar y al cambiar de abono; requieren una suscripción activa para utilizarlos y no extienden su vencimiento. Se consumen después del cupo base y antes de los créditos administrativos que no caducan. Se pueden comprar durante una espera de cambio de plan, salvo mientras se sincroniza la activación.
- Precio de paquetes: importe efectivamente pagado / (meses × reportes del cupo mensual) × cantidad. Se redondea el total a dos decimales. No se usa el precio de catálogo para sustituir un comprobante desconocido. En contratos anteriores se consulta el comprobante aprobado en MP; en los nuevos se conserva el precio aplicado.
- Cancelación de un cambio aún no pagado o una bajada programada: cancela el contrato nuevo y restaura el estado anterior antes de cerrar la operación. Se comprueba que la fecha e importe anteriores no cambiaron. Los cambios ya pagados no se cancelan automáticamente desde esta pantalla.

## Regla comercial confirmada el 01/11/2026

Claudio confirmó conservar los días ya pagados: el nuevo período se suma al vencimiento actual (`extend_expiry`). Si el contrato ya venció, se cuenta desde la activación. El código y la configuración de ejemplo usan esa regla por defecto; el checkout rechaza la configuración anterior `activation` para no descartar días por error.

También se conservan los días pendientes de un contrato semestral. No hay reembolso ni prorrateo: se añade la duración comprada al vencimiento vigente. La cuota mensual continúa renovándose por ciclo aunque el primer contrato resultante sea más largo por los días conservados.

Para los nuevos ciclos mensuales se usan 30 días exactos. Los contratos anteriores conservan su aniversario por mes calendario hasta cambiar. Las fechas de negocio se interpretan en America/Argentina/Buenos_Aires; el campo de vencimiento existente tiene precisión de día, no de hora.

## Componentes

Backend:

- `api/billing.py`: resumen autenticado, creación y cancelación de órdenes, verificación de pagos, sincronización de suscripciones.
- `billing_domain.py`: precio decimal, fechas y reglas puras.
- `migrations/20261015130425_dashboard_billing.sql`: órdenes, recibos, paquetes, renovaciones, bloqueo de sincronización, RLS y funciones transaccionales. Archivo creado con `supabase migration new` y movido al directorio de migraciones que usa este repositorio.
- `db.py`: al habilitar el esquema, inserción del reporte y descuento de cuota en una transacción, idempotente por ID del reporte.
- `api/payments.py` y `api/subscriptions.py`: encaminan pagos verificados al nuevo flujo; eventos de autorización no resetean el cupo de una suscripción gestionada. Se evita el checkout antiguo para cambiar una suscripción activa. El router ahora es local a cada registro: evita compartir dependencias de autenticación entre aplicaciones/tests.
- `scripts/reconcile_billing.py`: proceso ejecutable por separado o cada minuto dentro del servicio con `BILLING_RECONCILIATION_ENABLED=true`; recupera notificaciones de compras perdidas de los últimos siete días, reintenta cambios persistidos y concilia renovaciones automáticas sin que el usuario esté conectado. Puede preparar autorizaciones pendientes, pero nunca autoriza al comprador ni inicia cargos. También busca facturas de reemplazos aún no asociados como contrato actual. El bucle trabaja fuera del procesamiento de solicitudes y registra/reintenta errores.

Frontend recuperado en la rama `codex/billing-plan-management`:

- `src/features/dashboard/components/billing-panel.tsx`: saldo, planes, importes, paquetes, confirmación, pendientes y cancelación.
- `src/features/dashboard/index.tsx`: incorpora el panel y usa el saldo calculado por el backend cuando la función está activa.
- `billing-panel.test.tsx`: pruebas de navegador con pagos simulados.



## Integridad y errores

Los clientes no envían precios, dueño del pago ni créditos a conceder. El JWT determina la cuenta. Los importes y condiciones se guardan con la orden. Pagos duplicados no acreditan saldo dos veces. Un cobro duplicado real, importe incorrecto, pago fuera del período o un reverso se registra para revisión; no se intenta un reembolso real automáticamente. Los paquetes revocados pierden sólo el saldo no consumido. Devolver un segundo cobro duplicado no revoca los créditos de la compra original; un aviso pendiente atrasado no degrada un recibo ya aprobado. Un upgrade sin pago confirmado conserva el cupo y vencimiento anteriores. Las tablas de órdenes y créditos tienen RLS de lectura por propietario; mutaciones y recibos son exclusivas del backend.

Un bloqueo por cuenta serializa las actualizaciones en Mercado Pago. Inserción y consumo de un reporte son atómicos. Una protección de cuenta impide a un usuario corriente editar campos financieros o privilegios a través de la antigua política de perfil; conserva las operaciones de administradores y edición de nombre/teléfono/firma.

Un fallo de sincronización debe alertar al operador (el worker devuelve código distinto de cero). Las compras nuevas quedan controladas por `BILLING_MANAGEMENT_ENABLED`; `BILLING_SCHEMA_READY` debe mantenerse activo mientras existan órdenes para que los webhooks no vuelvan al manejo anterior. Los cobros de la programación antigua que coincidan con la transición requieren conciliación: no se aplican como una segunda renovación.

## Corrección de renovaciones recuperada el 01/11/2026

Los avisos `payment` de débitos recurrentes se resuelven por ID de pago a la factura de suscripción; no necesitan el `metadata.plan_id` del checkout antiguo. Una factura todavía no disponible o un error temporal del proveedor devuelve error para permitir reintentos. La migración añade una operación atómica de renovación y auditoría; un aviso atrasado no puede sobrescribir un recibo ya procesado ni una conciliación manual.

## Verificación realizada el 02/11/2026

- 68 pruebas Python dirigidas de billing, suscripciones y administración pasan.
- Escenarios SQL sobre PostgreSQL embebido PGlite: cupos, duplicados, precio incorrecto, saldo sin vencimiento, pago confirmado después del cierre del checkout, mejora inmediata/diferida, bajada, devolución de paquetes, cancelación, renovación idempotente, bloqueo de worker, propiedad/RLS y creación atómica de reportes.
- 6 pruebas de navegador del panel pasan; compilación TypeScript/Vite correcta.
- La revisión visual del 15/10 fue de la versión anterior; no se reutiliza como validación de esta recuperación.
- Suite `tests/` antes de incorporar la regla confirmada: 126 pasan y 1 falla en el refresco MyTanita. Se reprodujo el mismo fallo en el commit base sin estos cambios.
- La suite global desde la raíz incluye scripts de integración que requieren dependencias y credenciales adicionales; no se afirma que esos scripts hayan pasado.

## Despliegue

1. Regla comercial: `BILLING_EXPIRY_POLICY=extend_expiry`.
2. Migración probada en PGlite y contra el esquema remoto dentro de una transacción revertida. No hay una rama remota de staging disponible. La prueba remota confirmó que sólo `service_role` puede acreditar pagos; `anon` y `authenticated` no pueden hacerlo.
3. Aplicar migración y verificar asesores/permisos; desplegar backend y frontend coordinadamente. Habilitar `BILLING_SCHEMA_READY=true` antes de procesar órdenes.
4. `BILLING_RECONCILIATION_ENABLED=true` activa la conciliación cada minuto en la API. El servicio debe permanecer activo (Serverless desactivado). Los errores aparecen en logs y se reintentan.
5. Sólo después de comprobar los servicios, habilitar `BILLING_MANAGEMENT_ENABLED=true`. Para detener nuevas compras basta desactivar esta última; mantener esquema y conciliación mientras existan operaciones.
6. Verificar panel autenticado, importe y plan vigente. No se modifica María Ignacia Matteo sin su pago aprobado.

Estado 02/11/2026: migración aplicada, backend `91676a7` y frontend `ee931bc` publicados en `main`; los cuatro indicadores de activación fueron aplicados en Railway. La comprobación posterior encontró cero tablas billing sin RLS y cero tablas billing escribibles por clientes. Los asesores de seguridad conservan las 13 observaciones previas, sin observaciones nuevas. Salud HTTP 200 y nuevo endpoint presentes. Se añadió `a4c2318` para emitir inmediatamente los resultados de conciliación en logs. La primera pasada detectó que Mercado Pago rechaza el parámetro `limit` en búsquedas de facturas. Se reprodujo en sandbox y se ajustó la recuperación para usar el tamaño predeterminado con paginación por `offset`. La versión `95e8d64` quedó activa y se observaron dos pasadas consecutivas sin errores: la primera comprobó 4 suscripciones vencidas y procesó 3 facturas; la segunda comprobó 2 y procesó 1 (el contador incluye respuestas idempotentes). Salud HTTP 200. Frontend `ee931bc` activo. Los cuatro indicadores permanecen configurados. La sesión de administración en Chrome confirmó a Matteo en Básico mensual, activo hasta 01/12/2026; no tiene un upgrade concedido por estas pruebas. Las cuentas admin redirigen a administración, por lo que la revisión visual autenticada fue del panel admin; la pantalla de compra se validó en las 6 pruebas de navegador, no con una sesión real de clienta.

Fuentes del proveedor consultadas: [gestión de suscripciones](https://www.mercadopago.com.ar/developers/es/docs/subscriptions/subscription-management), [actualización de preapproval](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/update-preapproval/put), [preferencias de Checkout Pro](https://www.mercadopago.com.ar/developers/en/reference/online-payments/checkout-pro-preferences/create-preference/post).
