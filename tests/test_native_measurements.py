"""
Tests unitarios para api/native_measurements.py (receptor de mediciones nativas).

Correr:
    cd /path/to/smartbioscan-backend
    pytest tests/test_native_measurements.py -v

Nota: conftest.py mockea el módulo 'db' como MagicMock antes de importar esto.
native_measurements.py hace `from db import DB` DENTRO del handler, así que
parcheamos "db.DB" (donde se resuelve DB) para inyectar un FakeDB determinista
que simula la constraint unique (nutri_id, idempotency_key).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


# ── Constantes de test ──────────────────────────────────────────────────────
NUTRI   = "nutri-jwt-abc"
PATIENT = "22222222-2222-4222-8222-222222222222"
IDEM    = "11111111-1111-4111-8111-111111111111"
IDEM_2  = "99999999-9999-4999-8999-999999999999"


# ── Fake DB (simula la unique constraint) ───────────────────────────────────

class _FakeUniqueViolation(Exception):
    """Imita la excepción del cliente Postgres ante conflicto unique."""
    code = "23505"


class FakeDB:
    def __init__(self, patients=None):
        # patients: dict patient_id -> {"nutri_id": str, "is_active": bool}
        self.patients = patients or {}
        self.measurements = {}       # (nutri_id, idempotency_key) -> id
        self.inserted_rows = []
        self._counter = 0
        self.listed_rows = []
        self.report_rows = []

    def get_owned_patient_status(self, patient_id, nutri_id):
        p = self.patients.get(patient_id)
        if not p or p["nutri_id"] != nutri_id:
            return None
        return {"id": patient_id, "is_active": p["is_active"]}

    def insert_native_measurement(self, row):
        key = (row["nutri_id"], row["idempotency_key"])
        if key in self.measurements:
            raise _FakeUniqueViolation(
                "duplicate key value violates unique constraint "
                '"native_measurements_nutri_idem_uk"'
            )
        self._counter += 1
        mid = f"meas-{self._counter}"
        self.measurements[key] = mid
        self.inserted_rows.append(row)
        return {"id": mid}

    def get_native_measurement_by_idempotency(self, nutri_id, idem):
        mid = self.measurements.get((nutri_id, idem))
        return {"id": mid} if mid else None

    def list_owned_native_measurements(self, patient_id, nutri_id, limit, offset):
        assert patient_id == PATIENT
        assert nutri_id == NUTRI
        return self.listed_rows[offset:offset + limit]

    def get_reports_by_native_measurements(self, measurement_ids, nutri_id):
        assert nutri_id == NUTRI
        return [
            row for row in self.report_rows
            if row["native_measurement_id"] in measurement_ids
        ]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_app(nutri_id=NUTRI):
    from api.native_measurements import register_routes

    app = FastAPI()

    async def fake_get_current_nutri():
        return nutri_id

    register_routes(app, fake_get_current_nutri)
    return app


def _client(nutri_id=NUTRI):
    return TestClient(_make_app(nutri_id))


def _payload(**over):
    p = {
        "idempotency_key": IDEM,
        "patient_id": PATIENT,
        "captured_at": "2026-01-15T10:30:00Z",
        "source": "tanita_rd545",
        "scale_installation_id": "scale-install-1",
        "raw_b010_hex": "0a1b2c3d",
        "decoded_fields": {"weight_kg": 72.5, "body_fat_pct": 18.2},
        "parser_version": "b010-tanita-tags-v2",
        "profile_snapshot": {
            "birth_date": "1990-05-20",
            "sex": "male",
            "height_cm": 178.0,
            "figure": "normal",
            "activity": "moderate",
            "scale_label": "GUEST-1",
        },
    }
    p.update(over)
    return p


def _headers(idem=IDEM):
    return {"Idempotency-Key": idem}


def _active_patient_db():
    return FakeDB(patients={PATIENT: {"nutri_id": NUTRI, "is_active": True}})


def _post(client, db, payload=None, headers=None):
    with patch("db.DB", return_value=db):
        return client.post(
            "/api/native-measurements",
            json=payload if payload is not None else _payload(),
            headers=headers if headers is not None else _headers(),
        )


def _get(client, db, query=""):
    with patch("db.DB", return_value=db):
        return client.get(f"/api/native-measurements?patient_id={PATIENT}{query}")


def test_list_measurements_includes_report_link_without_raw_b010():
    db = _active_patient_db()
    db.listed_rows = [
        {
            "id": IDEM,
            "patient_id": PATIENT,
            "captured_at": "2026-07-20T10:30:00+00:00",
            "decoded_fields": {"Weight": 76.8, "BodyFat": 27.4},
            "parser_version": "b010-tanita-tags-v3",
            "profile_snapshot": {"height_cm": 182.0},
            "raw_b010_hex": "should-not-leave-server",
        }
    ]
    db.report_rows = [{"id": IDEM_2, "native_measurement_id": IDEM}]

    resp = _get(_client(), db)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["items"][0]["report_id"] == IDEM_2
    assert body["items"][0]["decoded_fields"]["Weight"] == 76.8
    assert "raw_b010_hex" not in body["items"][0]


def test_list_measurements_applies_bounded_pagination():
    db = _active_patient_db()
    db.listed_rows = [
        {
            "id": IDEM,
            "patient_id": PATIENT,
            "captured_at": "2026-07-20T10:30:00+00:00",
            "decoded_fields": {},
            "parser_version": "b010-tanita-tags-v3",
            "profile_snapshot": {},
        },
        {
            "id": IDEM_2,
            "patient_id": PATIENT,
            "captured_at": "2026-07-19T10:30:00+00:00",
            "decoded_fields": {},
            "parser_version": "b010-tanita-tags-v3",
            "profile_snapshot": {},
        },
    ]

    resp = _get(_client(), db, "&limit=1&offset=1")

    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()["items"]] == [IDEM_2]


def test_list_measurements_hides_foreign_or_missing_patient():
    db = FakeDB(patients={PATIENT: {"nutri_id": "other", "is_active": True}})
    resp = _get(_client(), db)
    assert resp.status_code == 404


# ── 1. Creación exitosa ─────────────────────────────────────────────────────

def test_create_ok():
    db = _active_patient_db()
    resp = _post(_client(), db)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["created"] is True
    assert body["measurement_id"] == "meas-1"
    # No debe filtrar raw ni datos clínicos.
    assert set(body.keys()) == {"ok", "measurement_id", "created"}


# ── 2. El nutri se deriva del JWT, no del body ──────────────────────────────

def test_nutri_id_comes_from_jwt_dependency():
    db = _active_patient_db()
    # El body NO tiene nutri_id; aún así la fila persistida usa el nutri del dep.
    resp = _post(_client(nutri_id=NUTRI), db)
    assert resp.status_code == 200, resp.text
    assert len(db.inserted_rows) == 1
    assert db.inserted_rows[0]["nutri_id"] == NUTRI
    # idempotency_key persistido normalizado como UUID.
    assert db.inserted_rows[0]["idempotency_key"] == IDEM


# ── 3. Retry (conflicto unique) devuelve el mismo id con created=False ──────

def test_retry_conflict_returns_same_id_created_false():
    db = _active_patient_db()
    client = _client()

    first = _post(client, db)
    assert first.status_code == 200
    assert first.json()["created"] is True
    first_id = first.json()["measurement_id"]

    second = _post(client, db)  # mismo payload + mismo Idempotency-Key
    assert second.status_code == 200
    body = second.json()
    assert body["created"] is False
    assert body["measurement_id"] == first_id
    # No se insertó una segunda fila.
    assert len(db.inserted_rows) == 1


# ── 4. Paciente ajeno / inexistente → 404 ───────────────────────────────────

def test_foreign_patient_404():
    # Paciente pertenece a OTRO nutri.
    db = FakeDB(patients={PATIENT: {"nutri_id": "otro-nutri", "is_active": True}})
    resp = _post(_client(nutri_id=NUTRI), db)
    assert resp.status_code == 404


def test_missing_patient_404():
    db = FakeDB(patients={})  # no existe
    resp = _post(_client(), db)
    assert resp.status_code == 404


# ── 5. Paciente inactivo → 409 ──────────────────────────────────────────────

def test_inactive_patient_409():
    db = FakeDB(patients={PATIENT: {"nutri_id": NUTRI, "is_active": False}})
    resp = _post(_client(), db)
    assert resp.status_code == 409


# ── 6. Idempotency-Key obligatorio / UUID / coincidencia con body ───────────

def test_missing_idempotency_header_400():
    db = _active_patient_db()
    resp = _post(_client(), db, headers={})
    assert resp.status_code == 400


def test_idempotency_header_not_uuid_422():
    db = _active_patient_db()
    resp = _post(_client(), db, headers={"Idempotency-Key": "no-es-uuid"})
    assert resp.status_code == 422


def test_header_body_mismatch_400():
    db = _active_patient_db()
    # header válido pero distinto del body.idempotency_key
    resp = _post(_client(), db, headers=_headers(idem=IDEM_2))
    assert resp.status_code == 400


# ── 7. Hex inválido / impar ─────────────────────────────────────────────────

def test_hex_non_hex_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(raw_b010_hex="zzzz"))
    assert resp.status_code == 422


def test_hex_odd_length_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(raw_b010_hex="0a1b2"))
    assert resp.status_code == 422


def test_hex_empty_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(raw_b010_hex=""))
    assert resp.status_code == 422


# ── 8. Exceso de raw hex ────────────────────────────────────────────────────

def test_hex_too_long_422():
    db = _active_patient_db()
    too_long = "ab" * 4097  # 8194 chars > 8192
    resp = _post(_client(), db, payload=_payload(raw_b010_hex=too_long))
    assert resp.status_code == 422


# ── 9. Exceso de decoded_fields ─────────────────────────────────────────────

def test_decoded_too_many_keys_422():
    db = _active_patient_db()
    big = {f"k{i}": float(i) for i in range(201)}  # 201 > 200
    resp = _post(_client(), db, payload=_payload(decoded_fields=big))
    assert resp.status_code == 422


def test_decoded_non_numeric_value_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(decoded_fields={"weight_kg": "heavy"}))
    assert resp.status_code == 422


# ── 10. source / parser_version inválidos ───────────────────────────────────

def test_invalid_source_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(source="omron"))
    assert resp.status_code == 422


def test_invalid_parser_version_422():
    db = _active_patient_db()
    # b010-v1 NO es aceptado; la app real manda b010-tanita-tags-v2.
    resp = _post(_client(), db, payload=_payload(parser_version="b010-v1"))
    assert resp.status_code == 422


def test_parser_v3_accepted():
    db = _active_patient_db()
    resp = _post(
        _client(), db,
        payload=_payload(parser_version="b010-tanita-tags-v3"),
    )
    assert resp.status_code == 200, resp.text
    assert db.inserted_rows[0]["parser_version"] == "b010-tanita-tags-v3"


# ── 11. captured_at inválido / futuro ───────────────────────────────────────

def test_captured_at_invalid_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(captured_at="not-a-date"))
    assert resp.status_code == 422


def test_captured_at_future_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(captured_at="2999-01-01T00:00:00Z"))
    assert resp.status_code == 422


def test_captured_at_before_2015_422():
    db = _active_patient_db()
    resp = _post(_client(), db, payload=_payload(captured_at="2010-06-01T00:00:00Z"))
    assert resp.status_code == 422


# ── 12. profile_snapshot allowlists / scale_label ───────────────────────────

def test_invalid_sex_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], sex="other")
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


def test_scale_label_too_long_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], scale_label="X" * 11)
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


# ── 13. La dependencia de auth (401) se propaga ─────────────────────────────

def test_auth_dependency_401():
    # Si get_current_nutri rechaza el token, el handler nunca corre y se
    # devuelve 401 (no 422/500), incluso con un payload válido.
    from api.native_measurements import register_routes
    from fastapi import HTTPException

    app = FastAPI()

    async def failing_nutri():
        raise HTTPException(status_code=401, detail="No autenticado")

    register_routes(app, failing_nutri)
    client = TestClient(app)
    db = _active_patient_db()
    with patch("db.DB", return_value=db):
        resp = client.post(
            "/api/native-measurements",
            json=_payload(),
            headers=_headers(),
        )
    assert resp.status_code == 401
    # No se debe haber tocado la DB.
    assert db.inserted_rows == []


# ── 14. decoded_fields > 64 KiB aunque tenga <= 200 claves ──────────────────

def test_decoded_over_64kib_within_key_limit_422():
    db = _active_patient_db()
    # 200 claves (no excede el límite de claves) pero > 64 KiB al serializar,
    # gracias a nombres de clave largos. Debe rechazarse por tamaño en bytes.
    big = {("k" * 350) + str(i): float(i) for i in range(200)}
    assert len(big) <= 200
    resp = _post(_client(), db, payload=_payload(decoded_fields=big))
    assert resp.status_code == 422


# ── 15. captured_at sin zona horaria explícita ──────────────────────────────

def test_captured_at_naive_no_timezone_422():
    db = _active_patient_db()
    # ISO-8601 válido pero SIN offset ni 'Z' → debe exigirse zona explícita.
    resp = _post(_client(), db, payload=_payload(captured_at="2026-01-15T10:30:00"))
    assert resp.status_code == 422


def test_captured_at_date_only_no_timezone_422():
    db = _active_patient_db()
    # Fecha sola (YYYY-MM-DD), sin hora ni zona → debe exigirse zona explícita.
    resp = _post(_client(), db, payload=_payload(captured_at="2026-01-15"))
    assert resp.status_code == 422


# ── 16. birth_date fuera de rango (1900..hoy) ───────────────────────────────

def test_birth_date_before_1900_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], birth_date="1899-12-31")
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


def test_birth_date_future_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], birth_date="2999-01-01")
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


# ── 17. height_cm fuera de rango (80..250) ──────────────────────────────────

def test_height_too_low_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], height_cm=50.0)
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


def test_height_too_high_422():
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], height_cm=300.0)
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


# ── 18. nutri_id (u otro campo extra) en el body es rechazado ───────────────

def test_extra_nutri_id_in_body_rejected_422():
    # extra="forbid": el nutri_id se deriva del JWT; inyectarlo en el body
    # debe fallar con 422 en vez de ignorarse silenciosamente.
    db = _active_patient_db()
    payload = _payload()
    payload["nutri_id"] = "attacker-nutri"
    resp = _post(_client(), db, payload=payload)
    assert resp.status_code == 422
    assert db.inserted_rows == []


def test_extra_field_in_profile_snapshot_rejected_422():
    # extra="forbid" también en profile_snapshot: un campo no declarado
    # (p.ej. un nutri_id inyectado) debe rechazarse con 422.
    db = _active_patient_db()
    snap = dict(_payload()["profile_snapshot"], nutri_id="attacker-nutri")
    resp = _post(_client(), db, payload=_payload(profile_snapshot=snap))
    assert resp.status_code == 422


# ── 19. Error genérico de DB → 500 sin filtrar el detalle interno ───────────

class _FakeGenericDBError(Exception):
    """Error de DB NO relacionado con la constraint unique."""


class ExplodingDB(FakeDB):
    SECRET = "SECRET-DSN-postgres://user:pass@host:5432/prod"

    def insert_native_measurement(self, row):
        raise _FakeGenericDBError(self.SECRET)


def test_generic_db_error_500_without_leaking_detail():
    db = ExplodingDB(patients={PATIENT: {"nutri_id": NUTRI, "is_active": True}})
    resp = _post(_client(), db)
    assert resp.status_code == 500
    # No debe filtrar el mensaje interno de la excepción / DSN.
    assert ExplodingDB.SECRET not in resp.text
    assert resp.json()["detail"] == "No se pudo guardar la medición"
