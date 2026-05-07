"""
SmartTanita — db.py
Módulo de acceso a Supabase para el pipeline.

Requiere variables de entorno:
    SUPABASE_URL        — https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY — service_role key (nunca la anon key en el backend)
    ENCRYPTION_KEY      — clave AES-256 para credenciales Tanita (32 bytes en hex)

Uso:
    from db import DB
    db = DB()
    patient = db.get_patient(patient_id)
    creds   = db.get_tanita_credentials(patient_id)
"""

import os
import hashlib
import base64
from datetime import datetime, timezone
from typing import Optional
from supabase import create_client, Client


# ── Encriptación simple AES-256 via Fernet (cryptography) ────────────────────
# Usamos Fernet porque es más simple que raw AES y suficiente para este caso.
# La clave se deriva del ENCRYPTION_KEY env var.

def _get_fernet():
    from cryptography.fernet import Fernet
    key_hex = os.environ.get('ENCRYPTION_KEY', '')
    if not key_hex:
        raise ValueError("ENCRYPTION_KEY no está configurada")
    # Derivar 32 bytes desde la clave hex y encodear en base64url para Fernet
    raw = bytes.fromhex(key_hex) if len(key_hex) == 64 else key_hex.encode()
    key_bytes = hashlib.sha256(raw).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_password(plaintext: str) -> str:
    """Encripta una password con Fernet (AES-128 en modo CBC + HMAC)."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Desencripta una password previamente encriptada."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


# ── Cliente Supabase ──────────────────────────────────────────────────────────

class DB:
    def __init__(self):
        url = os.environ.get('SUPABASE_URL')
        key = os.environ.get('SUPABASE_SERVICE_KEY')
        if not url or not key:
            raise ValueError("SUPABASE_URL y SUPABASE_SERVICE_KEY son obligatorias")
        self.client: Client = create_client(url, key)

    # ── Nutris ────────────────────────────────────────────────────────────────

    def get_nutri(self, nutri_id: str) -> Optional[dict]:
        res = self.client.table('nutris').select('*').eq('id', nutri_id).single().execute()
        return res.data

    def can_generate_report(self, nutri_id: str) -> dict:
        """
        Llama a la función SQL can_generate_report.
        Retorna {'ok': True/False, 'reason': ..., 'remaining': N}
        """
        res = self.client.rpc('can_generate_report', {'p_nutri_id': nutri_id}).execute()
        return res.data

    def increment_report_count(self, nutri_id: str):
        """Incrementa el contador de reportes del Nutri."""
        self.client.rpc('increment_report_count', {'p_nutri_id': nutri_id}).execute()

    # ── Patients ──────────────────────────────────────────────────────────────

    def get_patient(self, patient_id: str) -> Optional[dict]:
        res = (self.client.table('patients')
               .select('*')
               .eq('id', patient_id)
               .single()
               .execute())
        return res.data

    def get_patients_for_nutri(self, nutri_id: str, active_only: bool = True) -> list:
        q = self.client.table('patients').select('*').eq('nutri_id', nutri_id)
        if active_only:
            q = q.eq('is_active', True)
        return q.order('full_name').execute().data

    def count_active_patients(self, nutri_id: str) -> int:
        res = (self.client.table('patients')
               .select('id', count='exact')
               .eq('nutri_id', nutri_id)
               .eq('is_active', True)
               .execute())
        return res.count or 0

    def create_patient(self, nutri_id: str, data: dict) -> dict:
        """
        Crea un nuevo paciente.
        data: {full_name, date_of_birth, sex, height_cm, phone_whatsapp}
        """
        # Verificar límite de pacientes
        nutri = self.get_nutri(nutri_id)
        active_count = self.count_active_patients(nutri_id)
        if active_count >= nutri['max_patients']:
            raise ValueError(
                f"Límite de pacientes alcanzado ({active_count}/{nutri['max_patients']}). "
                "Actualizá tu plan para agregar más."
            )
        payload = {**data, 'nutri_id': nutri_id}
        res = self.client.table('patients').insert(payload).execute()
        return res.data[0]

    # ── Tanita Credentials ────────────────────────────────────────────────────

    def get_tanita_credentials(self, patient_id: str) -> Optional[dict]:
        """Retorna credenciales con password ya desencriptada."""
        res = (self.client.table('tanita_credentials')
               .select('*')
               .eq('patient_id', patient_id)
               .single()
               .execute())
        if not res.data:
            return None
        creds = res.data
        creds['tanita_password'] = decrypt_password(creds['tanita_password_enc'])
        return creds

    def upsert_tanita_credentials(self, patient_id: str,
                                   email: str, password: str) -> dict:
        """Guarda o actualiza credenciales encriptadas."""
        payload = {
            'patient_id':          patient_id,
            'tanita_email':        email,
            'tanita_password_enc': encrypt_password(password),
            'last_scrape_status':  'pending',
        }
        res = (self.client.table('tanita_credentials')
               .upsert(payload, on_conflict='patient_id')
               .execute())
        return res.data[0]

    def update_scrape_status(self, patient_id: str,
                              status: str, error_msg: str = None):
        """Actualiza el resultado del último scraping."""
        payload = {
            'last_scrape_status': status,
            'last_scraped_at':    datetime.now(timezone.utc).isoformat(),
            'last_error_msg':     error_msg,
        }
        self.client.table('tanita_credentials').update(payload).eq(
            'patient_id', patient_id
        ).execute()

    def get_patients_pending_scrape(self, nutri_id: str) -> list:
        """
        Retorna pacientes con credenciales configuradas,
        ordenados por antigüedad del último scrape (los más viejos primero).
        Útil para el batch nocturno.
        """
        res = (self.client.table('patients')
               .select('*, tanita_credentials(*)')
               .eq('nutri_id', nutri_id)
               .eq('is_active', True)
               .not_.is_('tanita_credentials', 'null')
               .order('tanita_credentials.last_scraped_at', nullsfirst=True)
               .execute())
        return [p for p in res.data if p.get('tanita_credentials')]

    # ── Reports ───────────────────────────────────────────────────────────────

    def create_report(self, patient_id: str, nutri_id: str,
                      measurement: dict, csv_raw: str,
                      pdf_path: str, generation_secs: float,
                      report_id: str = None) -> dict:
        """
        Registra un nuevo reporte en la BD.
        measurement: dict con los campos clave de la medición.
        report_id: UUID a usar como PK — debe coincidir con el path de Storage.
        """
        payload = {
            'patient_id':         patient_id,
            'nutri_id':           nutri_id,
            'measurement_date':   measurement.get('date'),
            'weight_kg':          measurement.get('weight_kg'),
            'body_fat_pct':       measurement.get('body_fat_pct'),
            'muscle_mass_kg':     measurement.get('muscle_mass_kg'),
            'visceral_fat':       measurement.get('visceral_fat'),
            'bmr_kcal':           measurement.get('bmr_kcal'),
            'metabolic_age':      measurement.get('metabolic_age'),
            'csv_raw':            csv_raw,
            'pdf_storage_path':   pdf_path,
            'generation_secs':    generation_secs,
        }
        if report_id:
            payload['id'] = report_id
        res = self.client.table('reports').insert(payload).execute()
        report = res.data[0]

        # Incrementar contador del Nutri
        self.increment_report_count(nutri_id)

        return report

    def get_reports_for_patient(self, patient_id: str, limit: int = 20) -> list:
        res = (self.client.table('reports')
               .select('*')
               .eq('patient_id', patient_id)
               .order('measurement_date', desc=True)
               .limit(limit)
               .execute())
        return res.data

    def get_last_measurement_date(self, patient_id: str) -> Optional[str]:
        """Retorna el measurement_date del reporte más reciente del paciente, o None."""
        res = (self.client.table('reports')
               .select('measurement_date')
               .eq('patient_id', patient_id)
               .order('measurement_date', desc=True)
               .limit(1)
               .execute())
        return res.data[0]['measurement_date'] if res.data else None

    # ── PDF Storage ───────────────────────────────────────────────────────────

    def upload_pdf(self, nutri_id: str, report_id: str,
                   pdf_bytes: bytes) -> str:
        """
        Sube el PDF a Supabase Storage.
        Retorna el path relativo: reports/{nutri_id}/{report_id}.pdf
        """
        path = f"{nutri_id}/{report_id}.pdf"
        self.client.storage.from_('reports').upload(
            path=path,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )
        return path

    def upload_html(self, nutri_id: str, report_id: str, html_content: str) -> str:
        """
        Sube el HTML a Supabase Storage.
        Retorna el path relativo: {nutri_id}/{report_id}.html
        """
        path = f"{nutri_id}/{report_id}.html"
        self.client.storage.from_('reports').upload(
            path=path,
            file=html_content.encode('utf-8'),
            file_options={"content-type": "text/html; charset=utf-8", "upsert": "true"}
        )
        return path

    def soft_delete_patient(self, patient_id: str, deleted_by: str) -> dict:
        """
        Soft delete atómico vía RPC Postgres.
        Requiere migration 001_shadow_tables.sql aplicada en Supabase.
        """
        res = self.client.rpc('soft_delete_patient', {
            'p_patient_id': patient_id,
            'p_deleted_by':  deleted_by,
        }).execute()
        return res.data or {}

    def update_patient(self, patient_id: str, data: dict) -> dict:
        """Actualiza campos editables de un paciente."""
        allowed = {'full_name', 'sex', 'height_cm', 'date_of_birth', 'phone_whatsapp'}
        payload = {k: v for k, v in data.items() if k in allowed}
        res = (self.client.table('patients')
               .update(payload)
               .eq('id', patient_id)
               .execute())
        return res.data[0] if res.data else {}

    def count_reports_for_patient(self, patient_id: str) -> int:
        res = (self.client.table('reports')
               .select('id', count='exact')
               .eq('patient_id', patient_id)
               .execute())
        return res.count or 0

    def get_pdf_url(self, storage_path: str, expires_in: int = 3600) -> str:
        """Genera una URL firmada temporal para descargar el PDF."""
        res = self.client.storage.from_('reports').create_signed_url(
            path=storage_path,
            expires_in=expires_in
        )
        return res['signedURL']

    # ── Report Deliveries ─────────────────────────────────────────────────────

    def create_delivery(self, report_id: str, channel: str,
                         recipient: str) -> dict:
        payload = {
            'report_id': report_id,
            'channel':   channel,
            'recipient': recipient,
            'status':    'pending',
        }
        res = self.client.table('report_deliveries').insert(payload).execute()
        return res.data[0]

    def update_delivery_status(self, delivery_id: str, status: str,
                                error_msg: str = None):
        payload = {'status': status}
        if status == 'sent':
            payload['sent_at'] = datetime.now(timezone.utc).isoformat()
        if status == 'delivered':
            payload['delivered_at'] = datetime.now(timezone.utc).isoformat()
        if error_msg:
            payload['error_msg'] = error_msg
        self.client.table('report_deliveries').update(payload).eq(
            'id', delivery_id
        ).execute()

    def sync_patient_settings(self, patient_id: str, settings: dict) -> dict:
        """
        Actualiza los datos del paciente desde MyTanita settings.
        Llamado al primer scrape o cuando el Nutri presiona
        "Actualizar datos del paciente" en la UI.

        settings: {full_name, date_of_birth, sex, height_cm}
        """
        from datetime import datetime, date

        dob_str = settings.get('dob', '')
        dob = None
        if dob_str:
            for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%m/%d/%Y'):
                try:
                    dob = datetime.strptime(dob_str.strip(), fmt).date().isoformat()
                    break
                except:
                    continue

        gender_raw = settings.get('gender', 'F').strip().upper()
        sex = 'F' if gender_raw.startswith('F') else 'M'

        height_str = settings.get('height', '170').strip()
        try:
            height_cm = float(''.join(c for c in height_str if c.isdigit() or c == '.'))
        except:
            height_cm = 170.0

        payload = {
            'full_name':      settings.get('name', '').strip(),
            'date_of_birth':  dob,
            'sex':            sex,
            'height_cm':      height_cm,
        }
        # Eliminar None para no sobreescribir con vacíos
        payload = {k: v for k, v in payload.items() if v is not None}

        res = (self.client.table('patients')
               .update(payload)
               .eq('id', patient_id)
               .execute())
        return res.data[0] if res.data else {}

    # ── Patient CSVs ──────────────────────────────────────────────────────────

    def upsert_patient_csvs(self, patient_id: str, nutri_id: str,
                             measurements: list) -> int:
        """
        Upsert de mediciones en patient_csvs (una fila por fecha).
        measurements: lista de dicts de csv_row_to_dict (con campo 'date').
        Retorna el número de filas procesadas.
        """
        rows = []
        for m in measurements:
            if not m.get('date'):
                continue
            rows.append({
                'patient_id':       patient_id,
                'nutri_id':         nutri_id,
                'measurement_date': m['date'],
                'raw_data':         {k: v for k, v in m.items() if k != 'date' and v is not None},
            })
        if not rows:
            return 0
        self.client.table('patient_csvs').upsert(
            rows, on_conflict='patient_id,measurement_date'
        ).execute()
        return len(rows)

    def get_patient_csvs(self, patient_id: str, limit: int = 50) -> list:
        """Retorna las mediciones del paciente ordenadas por fecha desc."""
        res = (self.client.table('patient_csvs')
               .select('*')
               .eq('patient_id', patient_id)
               .order('measurement_date', desc=True)
               .limit(limit)
               .execute())
        return res.data

    def mark_csv_report_generated(self, patient_id: str,
                                   measurement_date: str, report_id: str):
        """Marca una medición como con reporte PDF generado."""
        self.client.table('patient_csvs').update({
            'report_generated': True,
            'report_id':        report_id,
        }).eq('patient_id', patient_id).eq('measurement_date', measurement_date).execute()

    def patient_has_settings(self, patient_id: str) -> bool:
        """
        Retorna True si el paciente ya tiene datos completos en BD.
        En ese caso el pipeline puede saltarse el scrape de settings.
        """
        res = (self.client.table('patients')
               .select('height_cm, date_of_birth, sex')
               .eq('id', patient_id)
               .single()
               .execute())
        if not res.data:
            return False
        d = res.data
        return bool(d.get('height_cm') and d.get('date_of_birth') and d.get('sex'))
