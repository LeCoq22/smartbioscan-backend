# Dashboard: cambios de plan y paquetes

Implementación recuperada el 01/11/2026 de los worktrees del 15/10/2026, en la rama `codex/billing-plan-management` de ambos repositorios. No se aplicó esta migración a producción ni se habilitaron compras. No se realizaron cargos ni cambios de contratos reales durante esta recuperación.

## Reglas implementadas

- Básico → Plus: pago único del nuevo plan. Tras confirmación del pago se pausa el débito anterior. Si el cupo base está agotado, activa el cupo completo; si queda cupo base, espera a agotarlo o al fin del ciclo. No exige gastar los créditos comprados ni administrativos: ambos se conservan. Al activar, actualiza importe, frecuencia y siguiente cobro en Mercado Pago; comprueba el resultado con GET. Un fallo conserva el trabajo pendiente para reintentar.
- Plus → Básico: programa la nueva tarifa para el vencimiento de la suscripción pagada; conserva el plan actual hasta esa fecha. La activación exige un cobro aprobado del nuevo período, no sólo autorización de la suscripción. El semestral conserva su vencimiento de seis meses.
- Cambio de periodicidad sin subir de categoría: se programa al vencimiento, sin cobro inmediato.
- Paquetes de 10: pago único, se acreditan una vez y no vencen. Conservan su saldo al renovar y al cambiar de abono; requieren una suscripción activa para utilizarlos y no extienden su vencimiento. Se consumen después del cupo base y antes de los créditos administrativos que no caducan. Se pueden comprar durante una espera de cambio de plan, salvo mientras se sincroniza la activación.
- Precio de paquetes: importe efectivamente pagado / (meses × reportes del cupo mensual) × cantidad. Se redondea el total a dos decimales. No se usa el precio de catálogo para sustituir un comprobante desconocido. En contratos anteriores se consulta el comprobante aprobado en MP; en los nuevos se conserva el precio aplicado.
- Cancelación de un cambio aún no pagado o una bajada programada: restaura la configuración anterior del proveedor antes de cerrar la operación. Los cambios ya pagados no se cancelan automáticamente desde esta pantalla.

## Dos decisiones comerciales pendientes de confirmación

Se preguntaron durante la tarea y no hubo respuesta al preparar esta implementación:

1. Si se activa Plus el día 15, ¿los 30 días se cuentan desde ese día o se suman al vencimiento anterior? Se implementan ambas políticas; `BILLING_EXPIRY_POLICY=activation` es el valor de ejemplo y `extend_expiry` suma al vencimiento anterior. El checkout no se habilita sin configurar explícitamente una política. No activar hasta confirmar.
2. Se mantiene el modelo semestral existente: seis cupos mensuales, con bajada al finalizar los seis meses. Una mejora al agotarse el cupo mensual puede anticipar el fin del contrato anterior bajo la política `activation`; no se ha añadido un prorrateo/reembolso por meses futuros. Confirmar esta regla para semestrales antes de habilitarlos.

Para los nuevos ciclos mensuales se usan 30 días exactos. Los contratos anteriores conservan su aniversario por mes calendario hasta cambiar. Las fechas de negocio se interpretan en America/Argentina/Buenos_Aires; el campo de vencimiento existente tiene precisión de día, no de hora.

## Componentes

Backend:

- `api/billing.py`: resumen autenticado, creación y cancelación de órdenes, verificación de pagos, sincronización de suscripciones.
- `billing_domain.py`: precio decimal, fechas y reglas puras.
- `migrations/20261015130425_dashboard_billing.sql`: órdenes, recibos, paquetes, renovaciones, bloqueo de sincronización, RLS y funciones transaccionales. Archivo creado con `supabase migration new` y movido al directorio de migraciones que usa este repositorio.
- `db.py`: al habilitar el esquema, inserción del reporte y descuento de cuota en una transacción, idempotente por ID del reporte.
- `api/payments.py` y `api/subscriptions.py`: encaminan pagos verificados al nuevo flujo; eventos de autorización no resetean el cupo de una suscripción gestionada. Se evita el checkout antiguo para cambiar una suscripción activa. El router ahora es local a cada registro: evita compartir dependencias de autenticación entre aplicaciones/tests.
- `scripts/reconcile_billing.py`: proceso a ejecutar cada minuto por el entorno de despliegue; recupera notificaciones de compras perdidas de los últimos siete días, reintenta cambios persistidos y concilia renovaciones automáticas sin que el usuario esté conectado. No crea checkouts ni inicia cargos adicionales.

Frontend recuperado en la rama `codex/billing-plan-management`:

- `src/features/dashboard/components/billing-panel.tsx`: saldo, planes, importes, paquetes, confirmación, pendientes y cancelación.
- `src/features/dashboard/index.tsx`: incorpora el panel y usa el saldo calculado por el backend cuando la función está activa.
- `billing-panel.test.tsx`: pruebas de navegador con pagos simulados.



## Integridad y errores

Los clientes no envían precios, dueño del pago ni créditos a conceder. El JWT determina la cuenta. Los importes y condiciones se guardan con la orden. Pagos duplicados no acreditan saldo dos veces. Un cobro duplicado real, importe incorrecto, pago fuera del período o un reverso se registra para revisión; no se intenta un reembolso real automáticamente. Los paquetes revocados pierden sólo el saldo no consumido. Las tablas de órdenes y créditos tienen RLS de lectura por propietario; mutaciones y recibos son exclusivas del backend.

Un bloqueo por cuenta serializa las actualizaciones en Mercado Pago. Inserción y consumo de un reporte son atómicos. Una protección de cuenta impide a un usuario corriente editar campos financieros o privilegios a través de la antigua política de perfil; conserva las operaciones de administradores y edición de nombre/teléfono/firma.

Un fallo de sincronización debe alertar al operador (el worker devuelve código distinto de cero). Las compras nuevas quedan controladas por `BILLING_MANAGEMENT_ENABLED`; `BILLING_SCHEMA_READY` debe mantenerse activo mientras existan órdenes para que los webhooks no vuelvan al manejo anterior. Los cobros de la programación antigua que coincidan con la transición requieren conciliación: no se aplican como una segunda renovación.

## Corrección de renovaciones recuperada el 01/11/2026

Los avisos `payment` de débitos recurrentes se resuelven por ID de pago a la factura de suscripción; no necesitan el `metadata.plan_id` del checkout antiguo. Una factura todavía no disponible o un error temporal del proveedor devuelve error para permitir reintentos. La migración añade una operación atómica de renovación y auditoría; un aviso atrasado no puede sobrescribir un recibo ya procesado ni una conciliación manual.

## Verificación realizada el 01/11/2026

- 56 pruebas Python dirigidas de billing, suscripciones y administración pasan.
- Escenarios SQL sobre PostgreSQL embebido PGlite: cupos, duplicados, precio incorrecto, saldo sin vencimiento, pago confirmado después del cierre del checkout, mejora inmediata/diferida, bajada, devolución de paquetes, cancelación, renovación idempotente, bloqueo de worker, propiedad/RLS y creación atómica de reportes.
- 4 pruebas de navegador del panel pasan; compilación TypeScript/Vite correcta.
- La revisión visual del 15/10 fue de la versión anterior; no se reutiliza como validación de esta recuperación.
- Suite `tests/`: 126 pasan y 1 falla en el refresco MyTanita. Se reprodujo el mismo fallo en el commit base sin estos cambios.
- La suite global desde la raíz incluye scripts de integración que requieren dependencias y credenciales adicionales; no se afirma que esos scripts hayan pasado.

## Antes de habilitar

1. Confirmar las dos decisiones comerciales anteriores y revisar el tratamiento de cuotas futuras semestrales.
2. Aplicar la migración primero en staging. Se probaron funciones y RLS en PostgreSQL embebido, no se ejecutaron asesores sobre una instancia Supabase remota.
3. Configurar firma del webhook, URLs, `BILLING_SCHEMA_READY=true` y la política de vencimiento. Mantener `BILLING_MANAGEMENT_ENABLED=false` hasta completar la prueba de proveedor.
4. Probar con cuentas de prueba de Mercado Pago: checkout, pausa/reactivación, modificación de frecuencia a 30 días/6 meses y **aceptación de `next_payment_date` en la actualización**. La documentación pública muestra esta fecha en las respuestas; su actualización en este flujo todavía no fue verificada contra MP real. El código exige comprobarla con GET y no marca la sincronización como completa si no coincide. Si la API no permite la reprogramación exacta, debe adaptarse el mecanismo antes de habilitar compras.
5. Instalar el worker cada minuto, con exclusión/reintentos ya soportados y alerta ante fallo. Verificar llegada tardía, repetición y desorden de webhooks, y conciliación de cobros del calendario anterior.
6. Probar migración y despliegue coordinado con una cuenta de staging; recién entonces habilitar la función. La consola de Railway confirmó que la firma del webhook y las URLs existen, pero no están configurados los indicadores BILLING ni MP_TEST_ACCESS_TOKEN. No se imprimieron claves. Aún no se desplegó esta rama.

Fuentes del proveedor consultadas: [gestión de suscripciones](https://www.mercadopago.com.ar/developers/es/docs/subscriptions/subscription-management), [actualización de preapproval](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/update-preapproval/put), [preferencias de Checkout Pro](https://www.mercadopago.com.ar/developers/en/reference/online-payments/checkout-pro-preferences/create-preference/post).
