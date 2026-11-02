# Resultado de Mercado Pago: 02/11/2026 (Europe/Madrid)

La actualización de contratos existentes no conserva el calendario solicitado.
Se sustituyó por creación de un nuevo contrato y autorización del comprador.
La autorización completa en checkout oficial pasó las comprobaciones descritas
abajo. No se modificaron suscripciones de clientas durante estas pruebas.

Se verificó por `/users/me` que la credencial correspondía al vendedor ficticio
3808774368 y tenía la etiqueta `test_user`. Se utilizaron compradores ficticios
y la tarjeta de prueba oficial de Mercado Pago. La credencial quedó únicamente
en memoria; no se guardó en este repositorio, archivos de entorno ni resultados.

## Contrato existente

Se creó una suscripción autorizada de prueba de ARS 28.500 cada mes, se pausó y
se intentó reactivar con ARS 65.000 cada 30 días y próximo cobro el 16/12/2026.
El PUT devolvió éxito, pero el GET posterior confirmó:

| Campo | Solicitado | Observado |
| --- | --- | --- |
| Estado | authorized | authorized |
| Importe | 65000 ARS | 65000 ARS |
| Frecuencia | 30 days | 1 months |
| Próximo cobro | 16/12/2026 | 03/11/2026 |

No basta con comprobar el éxito HTTP. La prueba de regresión reproduce esta
respuesta y verifica que no se marca la sincronización como completada. Esto
por sí solo no hace seguro habilitar compras: el proveedor ya habría cambiado
el importe sin aplazar el débito. El flujo debe reemplazarse antes de habilitarlo.

La suscripción `15f29247084d450a94218a47f4a94b32` quedó cancelada y verificada.

## Nuevo contrato

Crear un contrato nuevo con `auto_recurring.frequency=30`,
`frequency_type=days` y `start_date=2026-12-16T12:00:00-03:00` sí conservó la
frecuencia y la fecha de cobro solicitadas. Mercado Pago asignó otra hora al
primer débito; solo se ha confirmado la coincidencia del día. La implementación
debe interpretar las fechas en Argentina, no comparar prefijos de timestamps
con distintas zonas horarias.

La suscripción autorizada `54bae00550734a439cec6598996fc4e5` quedó cancelada y
verificada. También se creó un contrato `pending` que conservó esos parámetros.
Intentar autorizarlo desde la credencial del vendedor devolvió HTTP 400:
`You cannot authorize a preapproval, only the payer can`.
Esa suscripción `0739692195b14b94b0f92748ae5a5963` también quedó cancelada.

## Checkout oficial completado

El usuario cambió manualmente a la cuenta ficticia y completó los campos de la
tarjeta de prueba. Se confirmó el checkout para el comprador 3804949794. La
consulta posterior del vendedor verificó el contrato
`a583ceb2b2cd4cf48a6ed01a558aa1a3`:

- Estado `authorized`.
- ARS 65000 cada 30 días.
- Próximo débito `2026-12-16T19:07:50.000-04:00`: día 16/12 en Argentina.
- Coincidencia de importe, frecuencia, fecha y vendedor ficticio.

Después se canceló ese contrato y se verificó `cancelled`. Los cuatro contratos
de prueba quedaron cancelados. No quedaron débitos de prueba programados.

## Adaptación implementada

El upgrade separa pago inicial y autorización de futuras renovaciones. Primero
se verifica el pago y se pausa el contrato anterior. Al activar el nuevo cupo,
se prepara una autorización con inicio en el vencimiento extendido. Cuando el
comprador la autoriza y el GET confirma todas las condiciones, se cancela el
contrato anterior y se cambia el identificador vigente.

La bajada o cambio de periodicidad pide autorización para el vencimiento. Solo
entonces se pausa el contrato anterior; el nuevo plan no se acredita hasta un
cobro aprobado. Cancelar la programación cancela primero el reemplazo y luego
restaura el contrato anterior. Un calendario incorrecto se pausa para revisión.

Pruebas locales: 68 Python, escenarios SQL de permisos/cuotas/idempotencia y
6 pruebas de navegador. Compilación frontend correcta. Se comprobó la migración
contra el esquema remoto en una transacción revertida. Estas pruebas no
simulan el transcurso de los 45 días hasta un débito futuro real del proveedor;
la recepción y repetición de facturas se verifican con escenarios controlados.

La cuenta de María Ignacia Matteo no se cambió por este trabajo; su upgrade
requiere pago aprobado.

Referencias: [crear suscripción](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/create-preapproval/post),
[actualizar suscripción](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/update-preapproval/put),
[tarjetas de prueba](https://www.mercadopago.com.ar/developers/es/docs/your-integrations/test/cards).

## Consulta de facturas

La primera conciliación productiva devolvió HTTP 400 al enviar `limit=100`.
Se reprodujo en sandbox: tanto `limit=100` como `limit=20` devuelven
`Invalid value for limit`. Omitir `limit`, manteniendo `offset=0`, devuelve
HTTP 200 con tamaño predeterminado de 12. La recuperación usa ese tamaño y
avanza `offset` hasta completar las páginas; filtra siempre el contrato exacto.
La prueba de regresión verifica paginación y descarta facturas de otra suscripción.
