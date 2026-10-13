from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.admin_accounts import register_routes


TARGET = '01045c3e-3059-43a3-b75f-31dc76546f81'
ADMIN = '11045c3e-3059-43a3-b75f-31dc76546f81'
BODY = dict(request_id='21045c3e-3059-43a3-b75f-31dc76546f81', days=15,
            extra_reports=0, expected_subscription_end='2026-08-12')


@pytest.fixture
def client(monkeypatch):
    import db
    factory = MagicMock()
    monkeypatch.setattr(db, 'DB', factory)
    app = FastAPI()
    register_routes(app, lambda: ADMIN)
    return TestClient(app), factory


@pytest.mark.parametrize('days,extra', [(15, 0), (30, 7)])
def test_extension_passes_authenticated_admin_and_snapshot_to_atomic_rpc(client, days, extra):
    http, factory = client
    factory.return_value.client.rpc.return_value.execute.return_value.data = {
        'ok': True, 'subscription_end': '2026-10-28', 'extra_reports': extra,
    }
    body = {**BODY, 'days': days, 'extra_reports': extra}
    response = http.post(f'/admin/nutris/{TARGET}/extend-account', json=body)
    assert response.status_code == 200
    factory.return_value.client.rpc.assert_called_once_with('admin_extend_account', {
        'p_admin_id': ADMIN, 'p_nutri_id': TARGET, 'p_request_id': BODY['request_id'],
        'p_days': days, 'p_extra_reports': extra, 'p_expected_end': '2026-08-12',
    })


@pytest.mark.parametrize('patch', [
    {'days': 1}, {'days': 30.0}, {'days': '15'}, {'days': True},
    {'extra_reports': -1}, {'extra_reports': 1.5}, {'extra_reports': True},
    {'extra_reports': 10001}, {'request_id': 'invalid'}, {'expected_subscription_end': None},
])
def test_invalid_request_never_touches_database(client, patch):
    http, factory = client
    assert http.post(f'/admin/nutris/{TARGET}/extend-account', json={**BODY, **patch}).status_code == 422
    factory.assert_not_called()


@pytest.mark.parametrize('reason,status', [('not_found', 404), ('forbidden', 403),
    ('suspended', 409), ('no_expiry', 409), ('changed', 409), ('request_conflict', 409)])
def test_rpc_rejections_have_actionable_http_errors(client, reason, status):
    http, factory = client
    factory.return_value.client.rpc.return_value.execute.return_value.data = {'ok': False, 'reason': reason}
    response = http.post(f'/admin/nutris/{TARGET}/extend-account', json=BODY)
    assert response.status_code == status
    assert response.json()['detail']


def test_admin_dependency_rejects_request_before_database(monkeypatch):
    import db
    factory = MagicMock()
    monkeypatch.setattr(db, 'DB', factory)
    def reject():
        raise HTTPException(403, 'Admin required')
    app = FastAPI()
    register_routes(app, reject)
    assert TestClient(app).post(f'/admin/nutris/{TARGET}/extend-account', json=BODY).status_code == 403
    factory.assert_not_called()
