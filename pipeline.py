"""
SmartTanita — Pipeline completo
scraper → settings → análisis → PDF

Uso:
    python3 pipeline.py --email user@mail.com --password secret \
                        --doctor "Dra. Diana Rodríguez" \
                        --output reporte.pdf
"""

import asyncio, argparse, sys, os, tempfile
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(__file__))
from analysis_engine import PatientInfo, analyze, TanitaMeasurement
from csv_parser import load_csv
from pdf_generator_pdfkit import generate_html


# ─────────────────────────────────────────────
# SCRAPER SETTINGS — extrae nombre, DOB, sexo, altura
# ─────────────────────────────────────────────

async def scrape_settings(page) -> dict:
    """
    Extrae datos del paciente desde /en/user/settings.
    Retorna dict con name, dob, sex, height_cm.
    """
    SETTINGS_URL = "https://mytanita.eu/en/user/settings"
    print("[Pipeline] Cargando settings del paciente...")
    await page.goto(SETTINGS_URL, wait_until="networkidle", timeout=20_000)

    def get_text(selector):
        try:
            el = page.locator(selector).first
            return asyncio.get_event_loop().run_until_complete(el.inner_text())
        except:
            return None

    # Extraer via JavaScript — más robusto que selectores CSS
    data = await page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('tr, .field-row, [class*="row"]'));
        const result = {};

        // Buscar todos los pares label-valor en la página
        document.querySelectorAll('*').forEach(el => {
            const text = el.innerText || '';
            const next = el.nextElementSibling;
            if (!next) return;
            const val = (next.innerText || '').trim();

            if (/^Name$/i.test(text.trim()))          result.name = val;
            if (/^Date of birth$/i.test(text.trim()))  result.dob = val;
            if (/^Gender$/i.test(text.trim()))         result.gender = val;
            if (/^Height$/i.test(text.trim()))         result.height = val;
        });
        return result;
    }""")

    # Fallback: leer texto de la página completa y parsear
    if not data.get('name'):
        page_text = await page.inner_text('body')
        lines = [l.strip() for l in page_text.split('\n') if l.strip()]
        for i, line in enumerate(lines):
            if line == 'Name' and i+1 < len(lines):
                data['name'] = lines[i+1]
            if line == 'Date of birth' and i+1 < len(lines):
                data['dob'] = lines[i+1]
            if line == 'Gender' and i+1 < len(lines):
                data['gender'] = lines[i+1]
            if line == 'Height' and i+1 < len(lines):
                data['height'] = lines[i+1]

    print(f"[Pipeline] Settings raw: {data}")
    return data


def parse_settings(raw: dict) -> dict:
    """Convierte los datos crudos de settings a tipos útiles."""
    name = raw.get('name', 'Paciente').strip()

    # DOB: puede venir como "08.09.1972" o "1972-09-08"
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

    # Sexo: "F", "M", "Female", "Male"
    gender_raw = raw.get('gender', 'F').strip().upper()
    sex = 'F' if gender_raw.startswith('F') else 'M'

    # Altura: "175 cm" → 175.0
    height_str = raw.get('height', '170').strip()
    try:
        height_cm = float(''.join(c for c in height_str if c.isdigit() or c == '.'))
    except:
        height_cm = 170.0

    return {
        'name': name,
        'age': age,
        'sex': sex,
        'height_cm': height_cm,
    }


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

async def run_pipeline(email: str, password: str,
                       doctor: str, output_path: str,
                       save_html: bool = False,
                       name_override: str = None,
                       age_override: int = None,
                       sex_override: str = None,
                       height_override: float = None):

    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    LOGIN_URL = "https://mytanita.eu/en/login"
    MEAS_URL  = "https://mytanita.eu/en/user/measurements"

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
            # ── 1. LOGIN ──────────────────────────────
            print(f"[Pipeline] Login en MyTanita...")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            await page.fill('input[name="mail"]', email, timeout=10_000)
            try:
                await page.fill('input[type="password"]', password, timeout=5_000)
            except:
                pass
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=20_000)

            if "login" in page.url.lower():
                try:
                    await page.fill('input[type="password"]', password, timeout=8_000)
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except:
                    pass

            if "login" in page.url.lower():
                print("[Pipeline] ✗ Login fallido")
                return False

            print(f"[Pipeline] ✓ Sesión activa")

            # ── 2. SETTINGS ───────────────────────────
            raw_settings = await scrape_settings(page)
            patient_data = parse_settings(raw_settings)

            # Aplicar overrides si se especificaron
            if name_override:   patient_data['name']       = name_override
            if age_override:    patient_data['age']        = age_override
            if sex_override:    patient_data['sex']        = sex_override
            if height_override: patient_data['height_cm']  = height_override

            print(f"[Pipeline] Paciente: {patient_data['name']} | "
                  f"{patient_data['age']}a | {patient_data['sex']} | "
                  f"{patient_data['height_cm']}cm")

            # ── 3. DESCARGAR CSV ──────────────────────
            print("[Pipeline] Descargando mediciones...")
            await page.goto(MEAS_URL, wait_until="networkidle", timeout=20_000)
            await page.click('#importExport', timeout=10_000)
            await page.wait_for_selector(
                'a[href="/en/user/export-csv"]',
                state="visible", timeout=8_000
            )
            async with page.expect_download(timeout=20_000) as dl_info:
                await page.click('a[href="/en/user/export-csv"]')

            download = await dl_info.value
            tmp_csv = f"/tmp/tanita_{email.replace('@','_').replace('.','_')}.csv"
            await download.save_as(tmp_csv)
            print(f"[Pipeline] ✓ CSV descargado: {tmp_csv}")

        except PWTimeout as e:
            print(f"[Pipeline] ✗ Timeout: {e}")
            return False
        except Exception as e:
            print(f"[Pipeline] ✗ Error scraping: {e}")
            return False
        finally:
            await context.close()
            await browser.close()

        # ── 4. ANÁLISIS ───────────────────────────────
        print("[Pipeline] Calculando análisis...")
        patient = PatientInfo(
            name=patient_data['name'],
            age=patient_data['age'],
            sex=patient_data['sex'],
            height_cm=patient_data['height_cm'],
        )
        measurements = load_csv(tmp_csv)
        print(f"[Pipeline] ✓ {len(measurements)} mediciones "
              f"({measurements[0].date[:10]} → {measurements[-1].date[:10]})")

        result = analyze(patient, measurements)

        # ── 5. GENERAR HTML ───────────────────────────
        print("[Pipeline] Generando HTML...")
        html = generate_html(result, doctor_name=doctor)

        if save_html:
            hp = output_path.replace('.pdf', '.html')
            open(hp, 'w', encoding='utf-8').write(html)
            print(f"[Pipeline] ✓ HTML: {hp}")

        # ── 6. GENERAR PDF CON PLAYWRIGHT ─────────────
        print("[Pipeline] Generando PDF...")
        from playwright.async_api import async_playwright as apw2

        async with apw2() as p2:
            browser2 = await p2.chromium.launch()
            page2 = await browser2.new_page()

            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.html', encoding='utf-8', delete=False
            ) as f:
                f.write(html)
                tmp_html = f.name

            await page2.goto(f'file://{tmp_html}')
            await page2.wait_for_load_state('networkidle')
            await page2.pdf(
                path=output_path,
                format='A4',
                print_background=True,
                margin={'top':'8mm','right':'8mm','bottom':'8mm','left':'8mm'}
            )
            await browser2.close()
            os.unlink(tmp_html)

        size_kb = os.path.getsize(output_path) // 1024
        name = patient_data['name']
        print(f"[Pipeline] ✓ PDF generado: {output_path} ({size_kb} KB)")
        print(f"[Pipeline] ✓ Reporte listo para {name}")
        return True


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='SmartTanita — Pipeline completo: scraping → análisis → PDF'
    )
    parser.add_argument('--email',    required=True,  help='Email MyTanita del paciente')
    parser.add_argument('--password', required=True,  help='Password MyTanita del paciente')
    parser.add_argument('--doctor',   default='',     help='Nombre del médico tratante')
    parser.add_argument('--output',   default='reporte_tanita.pdf', help='Archivo PDF de salida')
    parser.add_argument('--html',     action='store_true', help='Guardar también el HTML')

    # Overrides opcionales (si settings no tiene los datos)
    parser.add_argument('--name',     help='Override: nombre del paciente')
    parser.add_argument('--age',      type=int, help='Override: edad')
    parser.add_argument('--sex',      choices=['F','M'], help='Override: sexo')
    parser.add_argument('--height',   type=float, help='Override: altura en cm')

    args = parser.parse_args()

    t0 = datetime.now()
    ok = asyncio.run(run_pipeline(
        email=args.email,
        password=args.password,
        doctor=args.doctor,
        output_path=args.output,
        save_html=args.html,
        name_override=args.name,
        age_override=args.age,
        sex_override=args.sex,
        height_override=args.height,
    ))
    elapsed = (datetime.now() - t0).seconds
    if ok:
        print(f"\n✓ Pipeline completado en {elapsed}s")
    else:
        print(f"\n✗ Pipeline fallido")
        sys.exit(1)


if __name__ == '__main__':
    main()
