"""Admin-only account extensions; the RPC applies dates and credits atomically."""
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, field_validator


class ExtendAccountRequest(BaseModel):
    request_id: UUID
    days: Annotated[int, Field(strict=True)]
    extra_reports: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    expected_subscription_end: date

    @field_validator('days')
    @classmethod
    def validate_days(cls, value):
        if value not in (15, 30):
            raise ValueError('Elegí 15 o 30 días')
        return value


def register_routes(app, get_admin_nutri):
    @app.post('/admin/nutris/{nutri_id}/extend-account')
    def extend_account(
        nutri_id: UUID,
        body: ExtendAccountRequest,
        admin_id: str = Depends(get_admin_nutri),
    ):
        from db import DB

        result = DB().client.rpc('admin_extend_account', {
            'p_admin_id': admin_id,
            'p_nutri_id': str(nutri_id),
            'p_request_id': str(body.request_id),
            'p_days': body.days,
            'p_extra_reports': body.extra_reports,
            'p_expected_end': body.expected_subscription_end.isoformat(),
        }).execute().data
        if not result.get('ok'):
            errors = {
                'not_found': (404, 'Cuenta no encontrada.'),
                'forbidden': (403, 'Acceso de administrador requerido.'),
                'suspended': (409, 'La cuenta está suspendida. Revisá su suspensión antes de extenderla.'),
                'admin_account': (409, 'No se puede extender una cuenta administradora.'),
                'no_expiry': (409, 'Esta cuenta ya tiene acceso sin vencimiento.'),
                'changed': (409, 'La cuenta cambió. Volvé a buscarla y revisá el vencimiento.'),
                'request_conflict': (409, 'Esta confirmación ya se usó para otra operación. Volvé a seleccionar la cuenta.'),
                'invalid_input': (422, 'Elegí 15 o 30 días y una cantidad entera de 0 a 10000 reportes.'),
            }
            status, detail = errors.get(result.get('reason'), (500, 'No se pudo extender la cuenta.'))
            raise HTTPException(status_code=status, detail=detail)
        return result
