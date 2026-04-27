"""
Regenera HTML + PDF desde un CSV local sin scraping.
Uso:
    python3 test_report.py [csv_path] [--sex F|M] [--height 174] [--age 48] [--name Nombre] [--doctor Dr.]
"""
import asyncio, argparse, os, sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))

from csv_parser import load_csv
from analysis_engine import PatientInfo, analyze
from pdf_generator_pdfkit import generate_html
from pipeline_v2 import generate_pdf_bytes


def calc_age(dob_str: str) -> int:
    try:
        dob = datetime.strptime(dob_str[:10], "%Y-%m-%d").date()
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except Exception:
        return 0


async def main():
    parser = argparse.ArgumentParser(description="Genera reporte desde CSV local (sin scraping)")
    parser.add_argument("csv", nargs="?",
                        default=os.path.expanduser("~/Downloads/ProjectSmartTanita/20260420_measurements.csv"))
    parser.add_argument("--sex",    default="F", choices=["F", "M"])
    parser.add_argument("--height", type=float, default=174.0)
    parser.add_argument("--age",    type=int,   default=None)
    parser.add_argument("--dob",    default=None, help="YYYY-MM-DD")
    parser.add_argument("--name",   default="Paciente test")
    parser.add_argument("--doctor", default="")
    parser.add_argument("--out",    default="test_output", help="Prefijo de salida (sin extensión)")
    args = parser.parse_args()

    age = args.age
    if age is None:
        age = calc_age(args.dob) if args.dob else 40

    csv_path = os.path.expanduser(args.csv)
    if not os.path.exists(csv_path):
        print(f"✗ CSV no encontrado: {csv_path}")
        sys.exit(1)

    print(f"[test] CSV:     {csv_path}")
    print(f"[test] Paciente: {args.name} | {age}a | {args.sex} | {args.height}cm")

    measurements = load_csv(csv_path)
    print(f"[test] {len(measurements)} mediciones "
          f"({measurements[0].date[:10]} → {measurements[-1].date[:10]})")

    patient = PatientInfo(name=args.name, age=age, sex=args.sex, height_cm=args.height)
    result  = analyze(patient, measurements)
    html    = generate_html(result, doctor_name=args.doctor)

    html_path = f"{args.out}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[test] ✓ HTML: {html_path}")

    print("[test] Generando PDF...")
    pdf_bytes = await generate_pdf_bytes(html)
    pdf_path  = f"{args.out}.pdf"
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"[test] ✓ PDF:  {pdf_path}  ({len(pdf_bytes)//1024} KB)")

    os.system(f"open {html_path}")


if __name__ == "__main__":
    asyncio.run(main())
