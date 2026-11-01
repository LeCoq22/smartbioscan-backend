# Resultado de Mercado Pago: 02/11/2026 (Europe/Madrid)

**Bloquea el despliegue de cambios de abono.** La actualización del contrato
existente utilizada por `sync_order` no conserva el calendario solicitado.
Mantener `BILLING_MANAGEMENT_ENABLED=false`; no se aplicó la migración ni se
modificaron suscripciones de clientas durante esta prueba.

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

## Comprobación pendiente

Se preparó un checkout oficial para un nuevo comprador ficticio. Opera abrió
el checkout con la sesión real MAKK. No se seleccionó ningún medio de pago ni
se confirmó una compra. La revisión automática bloqueó abrir el selector de
cuenta; se solicitó al usuario cambiar manualmente al comprador ficticio.

Falta verificar el checkout completo con ese comprador y la conservación de
la fecha después de su autorización. No debe confundirse la creación del link
ni el estado `pending` con una autorización o un pago confirmado.

## Adaptación necesaria

El cambio deberá distinguir el pago inicial del upgrade de la autorización
del nuevo débito automático. La nueva suscripción necesita autorización del
comprador. Los días prepagados y créditos se conservan; el período del upgrade
solo se acredita con pago aprobado. El reemplazo debe coordinar pausa/cierre
del contrato anterior, comprobación de la nueva fecha, reintentos, cancelación,
webhooks desordenados y recuperación de respuestas perdidas sin habilitar dos
cobros ni conceder cupo por una mera autorización.

Esta adaptación todavía no está implementada ni validada de extremo a extremo.
La cuenta de María Ignacia Matteo sigue sin cambios por este trabajo y su
upgrade sigue pendiente de pago.

Referencias: [crear suscripción](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/create-preapproval/post),
[actualizar suscripción](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/update-preapproval/put),
[tarjetas de prueba](https://www.mercadopago.com.ar/developers/es/docs/your-integrations/test/cards).
