"""
SmartTanita — Pipeline v2
Flujo completo integrado con Supabase:
  login MyTanita → scrape settings → descarga CSV → análisis
  → PDF → Supabase Storage → registro en BD

Modos de uso:

  1. Con patient_id (producción — el paciente ya existe en la BD):
     python3 pipeline_v2.py --patient-id <uuid>

  2. Con credenciales directas (testing / primer uso):
     python3 pipeline_v2.py --email user@tanita.eu --password secret
       --name "Belén Beltrachini" --age 48 --sex F --height 158
       --doctor "Dra. Diana Rodríguez" --output reporte.pdf

  3. Batch nocturno de todos los pacientes de un Nutri:
     python3 pipeline_v2.py --nutri-id <uuid> --batch
"""

import asyncio, argparse, sys, os, tempfile, time
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from analysis_engine import PatientInfo, analyze
from csv_parser import load_csv
from pdf_generator_v2 import generate_html_v2 as generate_html


# ─────────────────────────────────────────────
# SCRAPER — login, settings, CSV
# ─────────────────────────────────────────────

async def scrape_settings(page) -> dict:
    SETTINGS_URL = "https://mytanita.eu/en/user/settings"
    print("[Scraper] Cargando settings...")
    await page.goto(SETTINGS_URL, wait_until="networkidle", timeout=20_000)

    data = await page.evaluate("""() => {
        const result = {};
        document.querySelectorAll('*').forEach(el => {
            const text = (el.innerText || '').trim();
            const next = el.nextElementSibling;
            if (!next) return;
            const val = (next.innerText || '').trim();
            if (/^Name$/i.test(text))          result.name = val;
            if (/^Date of birth$/i.test(text))  result.dob  = val;
            if (/^Gender$/i.test(text))         result.gender = val;
            if (/^Height$/i.test(text))         result.height = val;
        });
        return result;
    }""")

    if not data.get('name'):
        page_text = await page.inner_text('body')
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]
        for i, line in enumerate(lines):
            if line == 'Name'          and i+1 < len(lines): data['name']   = lines[i+1]
            if line == 'Date of birth' and i+1 < len(lines): data['dob']    = lines[i+1]
            if line == 'Gender'        and i+1 < len(lines): data['gender'] = lines[i+1]
            if line == 'Height'        and i+1 < len(lines): data['height'] = lines[i+1]

    print(f"[Scraper] Settings: {data}")
    return data


def parse_settings(raw: dict) -> dict:
    name = raw.get('name', 'Paciente').strip()

    dob_str = raw.get('dob', '')
    age = 0
    try:
        for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%m/%d/%Y'):
            try:
                dob = datetime.strptime(dob_str.strip(), fmt).date()
                today = date.today()
                age = today.year - dob.year - (
                    (today.month, today.day) < (dob.month, dob.day)
                )
                break
            except:
                continue
    except:
        age = 0

    gender_raw = raw.get('gender', 'F').strip().upper()
    sex = 'F' if gender_raw.startswith('F') else 'M'

    height_str = raw.get('height', '170').strip()
    try:
        height_cm = float(''.join(c for c in height_str if c.isdigit() or c == '.'))
    except:
        height_cm = 170.0

    return {'name': name, 'age': age, 'sex': sex, 'height_cm': height_cm,
            'dob': dob_str}


async def do_scrape(email: str, password: str, skip_settings: bool = False,
                    profile_id: str | None = None) -> dict:
    """
    Login, switch to patient's profile if profile_id given, extract settings, download CSV.
    Returns: {error, patient_data, csv_path, csv_content}
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from tanita_scraper import _do_login, _download_csv_for_profile

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-features=Translate"]
        )
        context = await browser.new_context(
            locale="en-US",
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            print("[Scraper] Login...")
            ok = await _do_login(page, email, password)
            if not ok:
                return {'error': 'login_failed', 'patient_data': None,
                        'csv_path': None, 'csv_content': None}
            print("[Scraper] ✓ Login OK")

            # Switch to patient's profile (multi-profile accounts)
            if profile_id:
                print(f"[Scraper] Cambiando a perfil {profile_id}...")
                await page.goto(
                    f"https://mytanita.eu/en/user/change-user-profile/{profile_id}",
                    wait_until="networkidle", timeout=15_000
                )

            # Settings — skip if already in DB
            if skip_settings:
                print("[Scraper] Settings en BD — skip")
                patient_data = {'name': '', 'age': 0, 'sex': 'F', 'height_cm': 170, 'dob': ''}
            else:
                raw_settings = await scrape_settings(page)
                patient_data = parse_settings(raw_settings)

            # CSV (profile already active — pass None to avoid double-switch)
            print("[Scraper] Descargando CSV...")
            csv_content = await _download_csv_for_profile(page, None, email)

            tmp_csv = f"/tmp/tanita_{email.replace('@', '_').replace('.', '_')}.csv"
            with open(tmp_csv, 'w', encoding='utf-8') as f:
                f.write(csv_content)

            print(f"[Scraper] ✓ CSV descargado ({len(csv_content)} bytes)")
            return {
                'error': None,
                'patient_data': patient_data,
                'csv_path': tmp_csv,
                'csv_content': csv_content,
            }

        except PWTimeout as e:
            return {'error': f'timeout: {e}', 'patient_data': None,
                    'csv_path': None, 'csv_content': None}
        except Exception as e:
            return {'error': str(e), 'patient_data': None,
                    'csv_path': None, 'csv_content': None}
        finally:
            try:
                await page.goto("https://mytanita.eu/en/logout",
                                wait_until="networkidle", timeout=8_000)
                print("[Scraper] ✓ Logout OK")
            except Exception:
                print("[Scraper] ~ Logout falló (no crítico)")
            await context.close()
            await browser.close()


# ─────────────────────────────────────────────
# PDF — genera y retorna bytes
# ─────────────────────────────────────────────

async def generate_pdf_bytes(html_content: str) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.html', encoding='utf-8', delete=False
        ) as f:
            f.write(html_content)
            tmp_html = f.name

        await page.goto(f'file://{tmp_html}')
        await page.wait_for_load_state('networkidle')
        pdf_bytes = await page.pdf(
            format='A4',
            print_background=True,
            margin={'top':'8mm','right':'8mm','bottom':'8mm','left':'8mm'}
        )
        await browser.close()
        os.unlink(tmp_html)
        return pdf_bytes


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

async def run_pipeline(
    email: str,
    password: str,
    doctor: str = '',
    output_path: str = None,
    save_html: bool = False,
    # Overrides manuales
    name_override: str = None,
    age_override: int = None,
    sex_override: str = None,
    height_override: float = None,
    # Supabase
    patient_id: str = None,
    nutri_id: str = None,
    use_db: bool = True,
    measurement_date: str = None,  # YYYY-MM-DD; si se da, genera reporte para esa fecha
) -> dict:
    """
    Pipeline completo. Retorna dict con resultado y metadatos.
    """
    t_start = time.time()
    result = {'ok': False, 'error': None, 'report_id': None,
              'pdf_path': None, 'pdf_bytes': None, 'skipped': False}

    # ── Supabase (opcional) ───────────────────
    db = None
    if use_db and os.environ.get('SUPABASE_URL'):
        try:
            from db import DB
            db = DB()
            print("[Pipeline] ✓ Conectado a Supabase")
        except Exception as e:
            print(f"[Pipeline] ~ Supabase no disponible: {e}")

    # ── Verificar quota si tenemos nutri_id ───
    if db and nutri_id:
        quota = db.can_generate_report(nutri_id)
        if not quota.get('ok'):
            reason = quota.get('reason', 'unknown')
            print(f"[Pipeline] ✗ Sin quota: {reason}")
            result['error'] = f"quota_{reason}"
            return result
        remaining = quota.get('remaining', '?')
        print(f"[Pipeline] Quota OK — {remaining} reportes restantes este mes")

    # ── Obtener credenciales y datos desde BD si hay patient_id ──
    settings_in_db = False
    profile_id = None
    if db and patient_id:
        creds = db.get_tanita_credentials(patient_id)
        if not creds:
            result['error'] = 'no_credentials'
            return result
        if not email:
            email    = creds['tanita_email']
            password = creds['tanita_password']

        patient_row = db.get_patient(patient_id)
        if patient_row:
            nutri_id   = nutri_id or patient_row['nutri_id']
            doctor     = doctor or _get_nutri_name(db, nutri_id)
            profile_id = patient_row.get('mytanita_profile_id')
            # Si ya tenemos datos completos en BD, los usamos
            # y saltamos el scrape de settings (~3s menos por reporte)
            if db.patient_has_settings(patient_id):
                settings_in_db  = True
                name_override   = name_override   or patient_row['full_name']
                age_override    = age_override    or _calc_age(patient_row.get('date_of_birth'))
                sex_override    = sex_override    or patient_row.get('sex', 'F')
                height_override = height_override or float(patient_row.get('height_cm', 170))
                print(f"[Pipeline] Datos del paciente desde BD (sin re-scrapear settings)")

    # ── Scraping ──────────────────────────────
    scrape = await do_scrape(email, password,
                             skip_settings=settings_in_db,
                             profile_id=profile_id)

    if scrape['error']:
        print(f"[Pipeline] ✗ Scraping fallido: {scrape['error']}")
        if db and patient_id:
            db.update_scrape_status(patient_id, 'login_failed'
                                    if 'login' in scrape['error'] else 'timeout',
                                    scrape['error'])
        result['error'] = scrape['error']
        return result

    if db and patient_id:
        db.update_scrape_status(patient_id, 'ok')
        # Primer scrape: guardar settings en BD para no re-scrapear la próxima vez
        if not settings_in_db and scrape['patient_data'].get('name'):
            try:
                db.sync_patient_settings(patient_id, {
                    'name':   scrape['patient_data']['name'],
                    'dob':    scrape['patient_data'].get('dob', ''),
                    'gender': scrape['patient_data'].get('sex', 'F'),
                    'height': str(scrape['patient_data'].get('height_cm', 170)),
                })
                print("[Pipeline] ✓ Settings guardados en BD")
            except Exception as e:
                print(f"[Pipeline] ~ No se pudieron guardar settings: {e}")

        # Upsert ALL measurements into patient_csvs for the reports screen
        if nutri_id:
            try:
                from tanita_scraper import parse_tanita_csv, extract_all_measurements
                df = parse_tanita_csv(scrape['csv_content'])
                meas_list = extract_all_measurements(df)
                count = db.upsert_patient_csvs(patient_id, nutri_id, meas_list)
                print(f"[Pipeline] ✓ {count} mediciones sincronizadas en patient_csvs")
            except Exception as e:
                print(f"[Pipeline] ~ No se pudo sincronizar patient_csvs: {e}")

    # ── Datos del paciente ────────────────────
    pd = scrape['patient_data']
    patient = PatientInfo(
        name      = name_override   or pd['name'],
        age       = age_override    or pd['age'],
        sex       = sex_override    or pd['sex'],
        height_cm = height_override or pd['height_cm'],
    )
    print(f"[Pipeline] Paciente: {patient.name} | {patient.age}a | "
          f"{patient.sex} | {patient.height_cm}cm")

    # ── Análisis ──────────────────────────────
    print("[Pipeline] Calculando análisis...")
    measurements = load_csv(scrape['csv_path'])
    print(f"[Pipeline] {len(measurements)} mediciones "
          f"({measurements[0].date[:10]} → {measurements[-1].date[:10]})")

    # Si se pide una fecha específica, filtramos hasta esa fecha
    target_date = measurement_date[:10] if measurement_date else None
    if target_date:
        measurements = [m for m in measurements if m.date[:10] <= target_date]
        if not measurements:
            result['error'] = f'no_measurement_for_date:{target_date}'
            return result
        print(f"[Pipeline] Filtrado a fecha {target_date} → {len(measurements)} mediciones")

    # Dedup: no generar si ya existe un reporte para esta medición exacta
    check_date = target_date or measurements[-1].date[:10]
    if db and patient_id:
        last_date = db.get_last_measurement_date(patient_id)
        if last_date and last_date[:10] == check_date:
            print(f"[Pipeline] ~ Ya existe reporte para {check_date} — skip")
            result['ok'] = True
            result['skipped'] = True
            return result

    analysis = analyze(patient, measurements)

    # ── HTML ──────────────────────────────────
    print("[Pipeline] Generando HTML...")
    html = generate_html(analysis, doctor_name=doctor)

    if save_html and output_path:
        hp = output_path.replace('.pdf', '.html')
        open(hp, 'w', encoding='utf-8').write(html)
        print(f"[Pipeline] ✓ HTML: {hp}")

    # ── PDF ───────────────────────────────────
    print("[Pipeline] Generando PDF...")
    pdf_bytes = await generate_pdf_bytes(html)
    size_kb = len(pdf_bytes) // 1024
    print(f"[Pipeline] ✓ PDF generado ({size_kb} KB)")

    # Guardar localmente si se especificó output
    if output_path:
        with open(output_path, 'wb') as f:
            f.write(pdf_bytes)
        print(f"[Pipeline] ✓ PDF guardado: {output_path}")

    result['pdf_bytes'] = pdf_bytes
    result['pdf_path']  = output_path

    # ── Supabase: guardar reporte ─────────────
    if db and patient_id and nutri_id:
        print("[Pipeline] Guardando en Supabase...")
        latest = measurements[-1]
        report_id = None

        try:
            # Subir PDF y HTML a Storage
            import uuid
            report_id = str(uuid.uuid4())
            storage_path = db.upload_pdf(nutri_id, report_id, pdf_bytes)
            print(f"[Pipeline] ✓ PDF en Storage: {storage_path}")
            db.upload_html(nutri_id, report_id, html)
            print(f"[Pipeline] ✓ HTML en Storage: {nutri_id}/{report_id}.html")

            # Registrar reporte
            elapsed = round(time.time() - t_start, 2)
            report = db.create_report(
                patient_id    = patient_id,
                nutri_id      = nutri_id,
                measurement   = {
                    'date':           latest.date,
                    'weight_kg':      latest.weight_kg,
                    'body_fat_pct':   latest.body_fat_pct,
                    'muscle_mass_kg': latest.muscle_mass_kg,
                    'visceral_fat':   latest.visceral_fat,
                    'bmr_kcal':       latest.bmr_kcal,
                    'metabolic_age':  latest.metabolic_age,
                },
                csv_raw        = scrape['csv_content'],
                pdf_path       = storage_path,
                generation_secs = elapsed,
            )
            result['report_id'] = report['id']
            print(f"[Pipeline] ✓ Reporte registrado: {report['id']}")

            # Link report to the patient_csvs row
            try:
                db.mark_csv_report_generated(
                    patient_id, latest.date[:10], report['id']
                )
            except Exception:
                pass

        except Exception as e:
            print(f"[Pipeline] ~ Error Supabase (PDF generado igual): {e}")

    elapsed = round(time.time() - t_start, 1)
    print(f"\n[Pipeline] ✓ Completado en {elapsed}s")
    result['ok'] = True
    return result


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _calc_age(dob_str) -> int:
    if not dob_str:
        return 0
    try:
        dob = datetime.strptime(str(dob_str)[:10], '%Y-%m-%d').date()
        today = date.today()
        return today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day)
        )
    except:
        return 0


def _get_nutri_name(db, nutri_id) -> str:
    try:
        nutri = db.get_nutri(nutri_id)
        if not nutri:
            return ''
        return nutri.get('display_signature') or nutri.get('full_name') or ''
    except:
        return ''


# ─────────────────────────────────────────────
# BATCH NOCTURNO
# ─────────────────────────────────────────────

async def run_batch(nutri_id: str, output_dir: str = '/tmp'):
    """
    Genera reportes para todos los pacientes activos de un Nutri.
    Respeta el rate limiting: 60s entre cada paciente.
    """
    from db import DB
    db = DB()

    patients = db.get_patients_pending_scrape(nutri_id)
    print(f"\n[Batch] {len(patients)} pacientes a procesar para nutri {nutri_id}")

    results = []
    for i, patient in enumerate(patients):
        creds = patient.get('tanita_credentials', {})
        if not creds:
            continue

        print(f"\n[Batch] Paciente {i+1}/{len(patients)}: {patient['full_name']}")

        output_path = os.path.join(
            output_dir,
            f"reporte_{patient['id'][:8]}_{datetime.now().strftime('%Y%m%d')}.pdf"
        )

        res = await run_pipeline(
            email       = creds['tanita_email'],
            password    = '',          # se obtiene internamente desde BD
            patient_id  = patient['id'],
            nutri_id    = nutri_id,
            output_path = output_path,
        )
        results.append({'patient': patient['full_name'], **res})

        # Rate limiting: esperar 60s entre pacientes (excepto el último)
        if i < len(patients) - 1:
            print(f"[Batch] Esperando 60s antes del siguiente...")
            await asyncio.sleep(60)

    ok = sum(1 for r in results if r['ok'])
    print(f"\n[Batch] ✓ Completado: {ok}/{len(results)} exitosos")
    return results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='SmartTanita — Pipeline integrado con Supabase'
    )

    # Modo 1: credenciales directas
    parser.add_argument('--email',    help='Email MyTanita del paciente')
    parser.add_argument('--password', help='Password MyTanita del paciente')
    parser.add_argument('--doctor',   default='', help='Nombre del médico tratante')
    parser.add_argument('--output',   default='reporte_tanita.pdf')
    parser.add_argument('--html',     action='store_true')

    # Overrides
    parser.add_argument('--name',   help='Override nombre')
    parser.add_argument('--age',    type=int, help='Override edad')
    parser.add_argument('--sex',    choices=['F','M'], help='Override sexo')
    parser.add_argument('--height', type=float, help='Override altura cm')

    # Modo 2: desde Supabase
    parser.add_argument('--patient-id', help='UUID del paciente en Supabase')
    parser.add_argument('--nutri-id',   help='UUID del Nutri en Supabase')

    # Modo 3: batch
    parser.add_argument('--batch', action='store_true',
                        help='Modo batch: procesar todos los pacientes del Nutri')

    # Sin BD
    parser.add_argument('--no-db', action='store_true',
                        help='No usar Supabase (modo standalone)')

    args = parser.parse_args()

    if args.batch:
        if not args.nutri_id:
            print("✗ --nutri-id requerido para modo batch")
            sys.exit(1)
        asyncio.run(run_batch(args.nutri_id))
        return

    if not args.email and not args.patient_id:
        print("✗ Se requiere --email o --patient-id")
        parser.print_help()
        sys.exit(1)

    res = asyncio.run(run_pipeline(
        email           = args.email or '',
        password        = args.password or '',
        doctor          = args.doctor,
        output_path     = args.output,
        save_html       = args.html,
        name_override   = args.name,
        age_override    = args.age,
        sex_override    = args.sex,
        height_override = args.height,
        patient_id      = args.patient_id,
        nutri_id        = args.nutri_id,
        use_db          = not args.no_db,
    ))

    if res['ok']:
        print(f"\n✓ Reporte listo: {args.output}")
        if res.get('report_id'):
            print(f"  BD report_id: {res['report_id']}")
    else:
        print(f"\n✗ Pipeline fallido: {res.get('error')}")
        sys.exit(1)


if __name__ == '__main__':
    main()


# ─────────────────────────────────────────────
# NOTAS UI — Semana 5
# ─────────────────────────────────────────────
# Botón "Actualizar datos del paciente" en el dashboard:
# - Llama a sync_patient_settings(patient_id) desde el backend
# - Re-scrapea /en/user/settings de MyTanita
# - Actualiza full_name, date_of_birth, sex, height_cm en patients
# - Muestra confirmación con los campos que cambiaron
#
# Flujo normal (sin el botón):
# - Si patients.height_cm IS NOT NULL → usar datos de BD, no scrapear settings
# - Si patients.height_cm IS NULL → scrapear settings y guardar en BD
