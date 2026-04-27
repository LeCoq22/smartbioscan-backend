"""
SmartBMI — Tanita Scraper v5
- Selector corregido: name="mail" (no "email")
- Captura el token CSRF antes de hacer login
- Maneja el form de 2 pasos si aplica
"""

import asyncio
import io
import sys
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

LOGIN_URL = "https://mytanita.eu/en/login"
MEAS_URL  = "https://mytanita.eu/en/user/measurements"


async def scrape_patient_data(email: str, password: str) -> dict:
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
            # 1. CARGAR LOGIN
            print(f"[Tanita] Cargando login...")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
            print(f"[Tanita] URL: {page.url}")

            if "login" in page.url.lower():

                # Llenar email con el selector correcto: name="mail"
                await page.fill('input[name="mail"]', email, timeout=10_000)
                print(f"[Tanita] Email ingresado.")

                # Llenar password — puede ser type="password" o name="password"
                # En el form inspeccionado solo se ve el campo mail y token.
                # Si el password aparece en el mismo form, probamos ambos:
                try:
                    await page.fill('input[type="password"]', password, timeout=5_000)
                    print(f"[Tanita] Password ingresado (type=password).")
                except:
                    try:
                        await page.fill('input[name="password"]', password, timeout=5_000)
                        print(f"[Tanita] Password ingresado (name=password).")
                    except:
                        print(f"[Tanita] No se encontró campo password visible — puede ser form de 2 pasos.")

                # Hacer submit
                await page.keyboard.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=20_000)
                print(f"[Tanita] URL tras submit: {page.url}")

                # Si hay un segundo paso (solo email primero), buscar password ahora
                if "login" in page.url.lower() or "password" in page.url.lower():
                    try:
                        await page.fill('input[type="password"]', password, timeout=8_000)
                        print(f"[Tanita] Password en paso 2.")
                        await page.keyboard.press("Enter")
                        await page.wait_for_load_state("networkidle", timeout=20_000)
                        print(f"[Tanita] URL tras paso 2: {page.url}")
                    except:
                        pass

            if "login" in page.url.lower():
                # Imprimir todos los inputs para diagnóstico
                inputs = await page.eval_on_selector_all(
                    "input",
                    "els => els.map(e => ({type:e.type, name:e.name, id:e.id, placeholder:e.placeholder}))"
                )
                print(f"[Tanita] Inputs actuales:")
                for i in inputs:
                    print(f"  {i}")
                return _error("Login fallido — revisar credenciales o flujo de login.")

            print(f"[Tanita] Sesión activa.")

            # 2. MEASUREMENTS
            await page.goto(MEAS_URL, wait_until="networkidle", timeout=20_000)
            print("[Tanita] Mediciones cargadas.")

            # 3. DROPDOWN
            await page.click('#importExport', timeout=10_000)
            await page.wait_for_selector(
                'a[href="/en/user/export-csv"]',
                state="visible", timeout=8_000
            )
            print("[Tanita] Dropdown abierto.")

            # 4. DESCARGAR CSV
            async with page.expect_download(timeout=20_000) as dl_info:
                await page.click('a[href="/en/user/export-csv"]')

            download = await dl_info.value
            tmp_path = f"/tmp/tanita_{email.replace('@','_').replace('.','_')}.csv"
            await download.save_as(tmp_path)

            with open(tmp_path, "r", encoding="utf-8") as f:
                csv_content = f.read()

            print(f"[Tanita] CSV — {len(csv_content)} bytes")

            df     = parse_tanita_csv(csv_content)
            latest = extract_latest(df)
            print(f"[Tanita] {len(df)} mediciones. Última: {latest.get('date','?')}")

            return {
                "success": True, "csv_content": csv_content,
                "dataframe": df, "latest": latest,
                "total": len(df), "error": None,
            }

        except PlaywrightTimeout as e:
            return _error(f"Timeout: {e}")
        except Exception as e:
            return _error(f"Error: {e}")
        finally:
            await context.close()
            await browser.close()


async def verify_login(email: str, password: str) -> dict:
    """Login-only check — no scraping. Returns {ok: bool, error: str|None}"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-features=Translate"]
        )
        context = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            if "login" in page.url.lower():
                await page.fill('input[name="mail"]', email, timeout=10_000)
                try:
                    await page.fill('input[type="password"]', password, timeout=5_000)
                except Exception:
                    try:
                        await page.fill('input[name="password"]', password, timeout=5_000)
                    except Exception:
                        pass
                await page.keyboard.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=20_000)

                if "login" in page.url.lower() or "password" in page.url.lower():
                    try:
                        await page.fill('input[type="password"]', password, timeout=8_000)
                        await page.keyboard.press("Enter")
                        await page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        pass

            if "login" in page.url.lower():
                return {"ok": False, "error": "login_failed"}
            return {"ok": True}
        except PlaywrightTimeout:
            return {"ok": False, "error": "timeout"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await context.close()
            await browser.close()


def _error(msg):
    return {"success": False, "error": msg,
            "csv_content": None, "dataframe": None, "latest": None, "total": 0}


def parse_tanita_csv(csv_content: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(csv_content))
    df.columns = [
        c.strip().replace(" ","_").replace("(","").replace(")","")
         .replace("%","pct").replace("-","_")
        for c in df.columns
    ]
    date_cols = [c for c in df.columns if "date" in c.lower()]
    if date_cols:
        df["_date"] = pd.to_datetime(df[date_cols[0]], errors="coerce")
        df = df.sort_values("_date", ascending=False)
    df = df.replace("-", None).replace("", None)
    return df.reset_index(drop=True)


def extract_latest(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    row  = df.iloc[0]
    cols = list(df.columns)

    def get(*candidates):
        for c in candidates:
            if c in cols:
                v = row.get(c)
                try:    return float(v) if v is not None else None
                except: return None
        return None

    def get_str(*candidates):
        for c in candidates:
            if c in cols: return row.get(c)
        return None

    return {
        "date":              get_str("Date","_date"),
        "weight_kg":         get("Weight_kg"),
        "bmi":               get("BMI"),
        "body_fat_pct":      get("Body_Fat_pct"),
        "visceral_fat":      get("Visc_Fat"),
        "muscle_mass_kg":    get("Muscle_Mass_kg"),
        "muscle_quality":    get("Muscle_Quality"),
        "bone_mass_kg":      get("Bone_Mass_kg"),
        "bmr_kcal":          get("BMR_kcal"),
        "metabolic_age":     get("Metab_Age"),
        "body_water_pct":    get("Body_Water_pct"),
        "physique_rating":   get("Physique_Rating"),
        "muscle_right_arm":  get("Muscle_mass___right_arm"),
        "muscle_left_arm":   get("Muscle_mass___left_arm"),
        "muscle_right_leg":  get("Muscle_mass___right_leg"),
        "muscle_left_leg":   get("Muscle_mass___left_leg"),
        "muscle_trunk":      get("Muscle_mass___trunk"),
        "quality_right_arm": get("Muscle_quality___right_arm"),
        "quality_left_arm":  get("Muscle_quality___left_arm"),
        "quality_right_leg": get("Muscle_quality___right_leg"),
        "quality_left_leg":  get("Muscle_quality___left_leg"),
        "quality_trunk":     get("Muscle_quality___trunk"),
        "fat_pct_right_arm": get("Body_fat_pct___right_arm"),
        "fat_pct_left_arm":  get("Body_fat_pct___left_arm"),
        "fat_pct_right_leg": get("Body_fat_pct___right_leg"),
        "fat_pct_left_leg":  get("Body_fat_pct___left_leg"),
        "fat_pct_trunk":     get("Body_fat_pct___trunk"),
        "heart_rate":        get("Heart_rate"),
    }


def print_result(result: dict):
    if not result["success"]:
        print(f"\n✗ ERROR: {result['error']}")
        return

    print(f"\n✓ ÉXITO — {result['total']} mediciones\n")
    m = result["latest"]

    for label, key in [
        ("Fecha","date"), ("Peso","weight_kg"), ("% Grasa","body_fat_pct"),
        ("Masa muscular","muscle_mass_kg"), ("Agua corporal","body_water_pct"),
        ("Masa ósea","bone_mass_kg"), ("TMB","bmr_kcal"),
        ("Grasa visceral","visceral_fat"), ("Edad metabólica","metabolic_age"),
    ]:
        v = m.get(key)
        if v is not None: print(f"  {label:<20} {v}")

    print("\n  Segmental muscular:")
    for label, mk, qk in [
        ("Tronco","muscle_trunk","quality_trunk"),
        ("Brazo izq","muscle_left_arm","quality_left_arm"),
        ("Brazo der","muscle_right_arm","quality_right_arm"),
        ("Pierna izq","muscle_left_leg","quality_left_leg"),
        ("Pierna der","muscle_right_leg","quality_right_leg"),
    ]:
        mv = m.get(mk); qv = m.get(qk)
        if mv: print(f"    {label:<12} {mv} kg  score {qv}")

    print("\n  Segmental grasa:")
    for label, fk in [
        ("Tronco","fat_pct_trunk"), ("Brazo izq","fat_pct_left_arm"),
        ("Brazo der","fat_pct_right_arm"), ("Pierna izq","fat_pct_left_leg"),
        ("Pierna der","fat_pct_right_leg"),
    ]:
        fv = m.get(fk)
        if fv: print(f"    {label:<12} {fv} %")

    print("\n  Historial:")
    df = result["dataframe"]
    dc = [c for c in df.columns if "date" in c.lower() and c != "_date"][0]
    wc = [c for c in df.columns if "weight" in c.lower()][0]
    mc = [c for c in df.columns if "muscle_mass" in c.lower()
          and not any(x in c.lower() for x in ["right","left","trunk","arm","leg"])][0]
    fc = [c for c in df.columns if "body_fat" in c.lower()
          and not any(x in c.lower() for x in ["right","left","trunk","arm","leg"])][0]
    for _, r in df.iterrows():
        print(f"    {str(r[dc])[:10]}  {r[wc]} kg  músculo {r[mc]} kg  grasa {r[fc]}%")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        print("\nSmartBMI — Tanita Scraper v5")
        print("=" * 50)
        result = asyncio.run(scrape_patient_data(sys.argv[1], sys.argv[2]))
        print_result(result)
    else:
        print("Uso: python3 tanita_scraper.py email password")
