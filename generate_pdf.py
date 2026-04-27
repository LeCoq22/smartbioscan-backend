"""
Generador de PDF usando Playwright headless.
Funciona en Linux/Docker sin interfaz gráfica.
Genera PDF con todos los fondos, colores y gráficos intactos.

Uso:
    python3 generate_pdf.py <csv> --name "Nombre" --age 48 --sex F
                            --height 158 --doctor "Dr. X" --output reporte.pdf
"""

import argparse, sys, os, asyncio, tempfile
sys.path.insert(0, os.path.dirname(__file__))

from analysis_engine import PatientInfo, analyze
from csv_parser import load_csv
from pdf_generator_pdfkit import generate_html   # reutiliza el HTML ya definido


async def html_to_pdf(html_content: str, output_path: str):
    """Convierte HTML a PDF usando Playwright con todos los fondos activos."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # Escribir HTML en archivo temporal para que las rutas relativas funcionen
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                         encoding='utf-8', delete=False) as f:
            f.write(html_content)
            tmp_path = f.name

        await page.goto(f'file://{tmp_path}')
        await page.wait_for_load_state('networkidle')  # esperar Chart.js / imágenes

        await page.pdf(
            path=output_path,
            format='A4',
            print_background=True,   # ← clave: incluye fondos y colores
            margin={
                'top':    '8mm',
                'right':  '8mm',
                'bottom': '8mm',
                'left':   '8mm',
            }
        )

        await browser.close()
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(
        description='Genera PDF de reporte Tanita con Playwright')
    parser.add_argument('csv')
    parser.add_argument('--name',   required=True)
    parser.add_argument('--age',    required=True, type=int)
    parser.add_argument('--sex',    required=True, choices=['F', 'M'])
    parser.add_argument('--height', required=True, type=float)
    parser.add_argument('--doctor', default='')
    parser.add_argument('--output', default='reporte_tanita.pdf')
    parser.add_argument('--html',   action='store_true',
                        help='Guardar también el HTML intermedio')
    args = parser.parse_args()

    patient = PatientInfo(args.name, args.age, args.sex, args.height)

    print(f'Cargando {args.csv}...')
    measurements = load_csv(args.csv)
    print(f'✓ {len(measurements)} mediciones '
          f'({measurements[0].date[:10]} → {measurements[-1].date[:10]})')

    print('Calculando análisis...')
    result = analyze(patient, measurements)

    print('Generando HTML...')
    html = generate_html(result, args.doctor)

    if args.html:
        hp = args.output.replace('.pdf', '.html')
        open(hp, 'w', encoding='utf-8').write(html)
        print(f'✓ HTML: {hp}')

    print('Generando PDF con Playwright...')
    asyncio.run(html_to_pdf(html, args.output))
    size_kb = os.path.getsize(args.output) // 1024
    print(f'✓ PDF: {args.output} ({size_kb} KB)')


if __name__ == '__main__':
    main()
