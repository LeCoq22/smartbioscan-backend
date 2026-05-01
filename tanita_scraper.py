"""
SmartBioScan — Tanita Scraper v6
Soporte multi-perfil: lista todos los perfiles de la cuenta y descarga
el CSV del perfil correcto mediante change-user-profile/{id}.
"""

import asyncio
import io
import sys
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

LOGIN_URL = "https://mytanita.eu/en/login"
MEAS_URL  = "https://mytanita.eu/en/user/measurements"
CSV_URL   = "https://mytanita.eu/en/user/export-csv"


# ─────────────────────────────────────────────
# HELPERS INTERNOS
# ─────────────────────────────────────────────

def _error(msg):
    return {"success": False, "error": msg,
            "csv_content": None, "dataframe": None, "latest": None, "total": 0}


async def _do_login(page, email: str, password: str) -> bool:
    """Login en mytanita.eu. Retorna True si tuvo éxito."""
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
    if "login" not in page.url.lower():
        return True  # ya estaba logueado

    await page.fill('input[name="mail"]', email, timeout=10_000)
    try:
        await page.fill('input[type="password"]', password, timeout=5_000)
    except Exception:
        pass
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle", timeout=20_000)

    # Soporte form de 2 pasos
    if "login" in page.url.lower() or "password" in page.url.lower():
        try:
            await page.fill('input[type="password"]', password, timeout=8_000)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

    return "login" not in page.url.lower()


async def _list_profiles_on_page(page) -> list[dict]:
    """
    Devuelve todos los perfiles de la cuenta.
    Selector: .main-navigation__dropdown-list li a[href*='change-user-profile']

    El perfil activo es el header (no tiene <a>); para obtener su ID
    hay que switchear a otro perfil y releer el dropdown.
    """
    INACTIVE_SEL = (
        '.main-navigation__dropdown ul.main-navigation__dropdown-list '
        'li:not(.main-navigation__dropdown-header) '
        'a[href*="change-user-profile"]'
    )
    ACTIVE_NAME_SEL = '.main-navigation__dropdown-header__name'

    async def _read_links(pg):
        items = []
        els = await pg.query_selector_all(INACTIVE_SEL)
        for a in els:
            href = (await a.get_attribute('href')) or ''
            name = ((await a.inner_text()) or '').strip()
            pid = href.rstrip('/').split('/')[-1]
            if pid.isdigit():
                items.append({'profile_id': pid, 'profile_name': name})
        return items

    async def _active_name(pg) -> str:
        el = await pg.query_selector(ACTIVE_NAME_SEL)
        return ((await el.inner_text()) if el else '').strip()

    inactive = await _read_links(page)
    active_name = await _active_name(page)

    if not inactive:
        # Cuenta de un solo perfil — ID no visible en el DOM directamente
        return [{'profile_id': None, 'profile_name': active_name}]

    # Necesitamos el ID del perfil activo:
    # Switcheamos al primer inactivo → el activo original aparecerá como link
    first_inactive_id = inactive[0]['profile_id']
    await page.goto(
        f'https://mytanita.eu/en/user/change-user-profile/{first_inactive_id}',
        wait_until='networkidle', timeout=15_000
    )

    # Ahora el dropdown expone el perfil que era activo + los demás inactivos
    exposed = await _read_links(page)

    # El perfil al que switcheamos (first_inactive_id) es ahora el header y
    # no aparece en `exposed` → lo añadimos manualmente
    all_profiles = exposed + [inactive[0]]

    # Encontramos el ID del perfil original activo para restaurarlo
    original_active = next(
        (p for p in exposed if p['profile_name'] == active_name), None
    )

    if original_active:
        await page.goto(
            f'https://mytanita.eu/en/user/change-user-profile/{original_active["profile_id"]}',
            wait_until='networkidle', timeout=15_000
        )

    # Deduplicar por profile_id
    seen: set = set()
    result = []
    for p in all_profiles:
        pid = p['profile_id']
        if pid not in seen:
            seen.add(pid)
            result.append(p)

    return result


async def _download_csv_for_profile(page, profile_id: str | None, email: str) -> str:
    """
    Cambia al perfil indicado (si se especifica) y descarga su CSV completo.
    Retorna el contenido del CSV como string.
    """
    if profile_id:
        await page.goto(
            f'https://mytanita.eu/en/user/change-user-profile/{profile_id}',
            wait_until='networkidle', timeout=15_000
        )

    # Navegar a measurements y activar dropdown de export
    await page.goto(MEAS_URL, wait_until="networkidle", timeout=20_000)
    await page.click('#importExport', timeout=10_000)
    await page.wait_for_selector(
        f'a[href="/en/user/export-csv"]',
        state='visible', timeout=8_000
    )

    async with page.expect_download(timeout=25_000) as dl_info:
        await page.click('a[href="/en/user/export-csv"]')

    download = await dl_info.value
    safe_email = email.replace('@', '_').replace('.', '_')
    pid_suffix = f'_{profile_id}' if profile_id else ''
    tmp_path = f'/tmp/tanita_{safe_email}{pid_suffix}.csv'
    await download.save_as(tmp_path)

    with open(tmp_path, 'r', encoding='utf-8') as f:
        return f.read()


# ─────────────────────────────────────────────
# API PÚBLICA
# ─────────────────────────────────────────────

async def verify_and_list_profiles(email: str, password: str) -> dict:
    """
    Hace login y devuelve la lista de perfiles de la cuenta.
    Retorna: {ok: bool, profiles: [{profile_id, profile_name}], error: str|None}
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-features=Translate']
        )
        context = await browser.new_context(
            locale='en-US',
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        )
        page = await context.new_page()
        try:
            ok = await _do_login(page, email, password)
            if not ok:
                return {'ok': False, 'profiles': [], 'error': 'login_failed'}

            profiles = await _list_profiles_on_page(page)
            return {'ok': True, 'profiles': profiles, 'error': None}

        except PlaywrightTimeout as e:
            return {'ok': False, 'profiles': [], 'error': f'timeout: {e}'}
        except Exception as e:
            return {'ok': False, 'profiles': [], 'error': str(e)}
        finally:
            await context.close()
            await browser.close()


async def verify_login(email: str, password: str) -> dict:
    """Login-only check (sin listar perfiles). Retorna {ok: bool, error: str|None}"""
    result = await verify_and_list_profiles(email, password)
    return {'ok': result['ok'], 'error': result.get('error')}


async def scrape_profile_csv(
    email: str,
    password: str,
    profile_id: str | None,
) -> dict:
    """
    Hace login, cambia al perfil indicado y descarga su CSV completo.
    Retorna: {success, csv_content, dataframe, latest, total, error}
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-features=Translate']
        )
        context = await browser.new_context(
            locale='en-US',
            accept_downloads=True,
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        )
        page = await context.new_page()
        try:
            ok = await _do_login(page, email, password)
            if not ok:
                return _error('login_failed')

            csv_content = await _download_csv_for_profile(page, profile_id, email)
            df = parse_tanita_csv(csv_content)
            latest = extract_latest(df)
            print(f'[Scraper] Perfil {profile_id}: {len(df)} mediciones, última {latest.get("date","?")}')

            return {
                'success': True,
                'csv_content': csv_content,
                'dataframe': df,
                'latest': latest,
                'total': len(df),
                'error': None,
            }

        except PlaywrightTimeout as e:
            return _error(f'timeout: {e}')
        except Exception as e:
            return _error(f'error: {e}')
        finally:
            try:
                await page.goto(
                    'https://mytanita.eu/en/logout',
                    wait_until='networkidle', timeout=8_000
                )
            except Exception:
                pass
            await context.close()
            await browser.close()


async def scrape_all_profiles(email: str, password: str) -> list[dict]:
    """
    Hace login una sola vez y descarga el CSV de TODOS los perfiles.
    Retorna lista de {profile_id, profile_name, csv_content, dataframe, latest, total, error}
    Optimización: un solo login + N switches de perfil.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-features=Translate']
        )
        context = await browser.new_context(
            locale='en-US',
            accept_downloads=True,
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        )
        page = await context.new_page()
        results = []
        try:
            ok = await _do_login(page, email, password)
            if not ok:
                return [{'error': 'login_failed', 'profile_id': None, 'profile_name': None}]

            profiles = await _list_profiles_on_page(page)
            print(f'[Scraper] {len(profiles)} perfil(es) encontrado(s): {[p["profile_name"] for p in profiles]}')

            for prof in profiles:
                pid = prof['profile_id']
                pname = prof['profile_name']
                try:
                    csv_content = await _download_csv_for_profile(page, pid, email)
                    df = parse_tanita_csv(csv_content)
                    latest = extract_latest(df)
                    print(f'[Scraper] {pname} ({pid}): {len(df)} mediciones')
                    results.append({
                        'profile_id': pid,
                        'profile_name': pname,
                        'csv_content': csv_content,
                        'dataframe': df,
                        'latest': latest,
                        'total': len(df),
                        'error': None,
                    })
                except Exception as e:
                    print(f'[Scraper] Error en perfil {pname}: {e}')
                    results.append({
                        'profile_id': pid,
                        'profile_name': pname,
                        'csv_content': None,
                        'dataframe': None,
                        'latest': None,
                        'total': 0,
                        'error': str(e),
                    })

        except Exception as e:
            print(f'[Scraper] Error global: {e}')
        finally:
            try:
                await page.goto('https://mytanita.eu/en/logout',
                                wait_until='networkidle', timeout=8_000)
            except Exception:
                pass
            await context.close()
            await browser.close()

        return results


# Alias para compatibilidad con pipeline_v2.py
async def scrape_patient_data(email: str, password: str, profile_id: str | None = None) -> dict:
    """Wrapper de scrape_profile_csv para compatibilidad."""
    return await scrape_profile_csv(email, password, profile_id)


# ─────────────────────────────────────────────
# PARSERS CSV
# ─────────────────────────────────────────────

def parse_tanita_csv(csv_content: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(csv_content))
    df.columns = [
        c.strip().replace(' ', '_').replace('(', '').replace(')', '')
         .replace('%', 'pct').replace('-', '_')
        for c in df.columns
    ]
    date_cols = [c for c in df.columns if 'date' in c.lower()]
    if date_cols:
        df['_date'] = pd.to_datetime(df[date_cols[0]], errors='coerce')
        df = df.sort_values('_date', ascending=False)
    df = df.replace('-', None).replace('', None)
    return df.reset_index(drop=True)


def csv_row_to_dict(row: pd.Series, cols: list[str]) -> dict:
    """Convierte una fila del DataFrame en dict para guardar en patient_csvs.raw_data."""
    import math

    def _get(*candidates):
        for c in candidates:
            if c in cols:
                v = row.get(c)
                try:
                    f = float(v) if v is not None else None
                    # NaN / Inf no son JSON-compliant → None
                    if f is not None and (math.isnan(f) or math.isinf(f)):
                        return None
                    return f
                except Exception:
                    return None
        return None

    def _get_str(*candidates):
        for c in candidates:
            if c in cols:
                return row.get(c)
        return None

    return {
        'date':              str(_get_str('Date', '_date'))[:10] if _get_str('Date', '_date') else None,
        'weight_kg':         _get('Weight_kg'),
        'bmi':               _get('BMI'),
        'body_fat_pct':      _get('Body_Fat_pct'),
        'visceral_fat':      _get('Visc_Fat'),
        'muscle_mass_kg':    _get('Muscle_Mass_kg'),
        'muscle_quality':    _get('Muscle_Quality'),
        'bone_mass_kg':      _get('Bone_Mass_kg'),
        'bmr_kcal':          _get('BMR_kcal'),
        'metabolic_age':     _get('Metab_Age'),
        'body_water_pct':    _get('Body_Water_pct'),
        'physique_rating':   _get('Physique_Rating'),
        'muscle_right_arm':  _get('Muscle_mass___right_arm'),
        'muscle_left_arm':   _get('Muscle_mass___left_arm'),
        'muscle_right_leg':  _get('Muscle_mass___right_leg'),
        'muscle_left_leg':   _get('Muscle_mass___left_leg'),
        'muscle_trunk':      _get('Muscle_mass___trunk'),
        'quality_right_arm': _get('Muscle_quality___right_arm'),
        'quality_left_arm':  _get('Muscle_quality___left_arm'),
        'quality_right_leg': _get('Muscle_quality___right_leg'),
        'quality_left_leg':  _get('Muscle_quality___left_leg'),
        'quality_trunk':     _get('Muscle_quality___trunk'),
        'fat_pct_right_arm': _get('Body_fat_pct___right_arm'),
        'fat_pct_left_arm':  _get('Body_fat_pct___left_arm'),
        'fat_pct_right_leg': _get('Body_fat_pct___right_leg'),
        'fat_pct_left_leg':  _get('Body_fat_pct___left_leg'),
        'fat_pct_trunk':     _get('Body_fat_pct___trunk'),
        'heart_rate':        _get('Heart_rate'),
    }


def extract_all_measurements(df: pd.DataFrame) -> list[dict]:
    """Devuelve una lista de dicts (uno por fila) para hacer upsert en patient_csvs."""
    cols = list(df.columns)
    return [csv_row_to_dict(df.iloc[i], cols) for i in range(len(df))]


def extract_latest(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    cols = list(df.columns)
    return csv_row_to_dict(df.iloc[0], cols)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def print_result(result: dict):
    if not result.get('success'):
        print(f'\n✗ ERROR: {result.get("error")}')
        return

    print(f'\n✓ ÉXITO — {result["total"]} mediciones\n')
    m = result['latest']
    for label, key in [
        ('Fecha', 'date'), ('Peso', 'weight_kg'), ('% Grasa', 'body_fat_pct'),
        ('Masa muscular', 'muscle_mass_kg'), ('Grasa visceral', 'visceral_fat'),
    ]:
        v = m.get(key)
        if v is not None:
            print(f'  {label:<20} {v}')


if __name__ == '__main__':
    if len(sys.argv) >= 3:
        email, password = sys.argv[1], sys.argv[2]
        profile_id = sys.argv[3] if len(sys.argv) > 3 else None
        print('\nSmartBioScan — Tanita Scraper v6')
        print('=' * 50)
        if profile_id == '--list':
            async def _list():
                result = await verify_and_list_profiles(email, password)
                if result['ok']:
                    print('Perfiles encontrados:')
                    for p in result['profiles']:
                        print(f'  [{p["profile_id"]}] {p["profile_name"]}')
                else:
                    print(f'Error: {result["error"]}')
            asyncio.run(_list())
        else:
            result = asyncio.run(scrape_profile_csv(email, password, profile_id))
            print_result(result)
    else:
        print('Uso: python3 tanita_scraper.py email password [profile_id|--list]')
