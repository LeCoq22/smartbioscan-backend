"""
Genera solo el HTML del reporte (sin PDF).
El PDF se hace desde Chrome con Cmd+P → Guardar como PDF.

Uso:
    python3 generate_html_only.py 20260420_measurements.csv \
      --name "Belén Beltrachini" --age 48 --sex F --height 158 \
      --doctor "Dra. Diana Rodríguez" \
      --output reporte_belen.html
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from analysis_engine import PatientInfo, analyze
from csv_parser import load_csv
from pdf_generator_pdfkit import generate_html   # reutiliza el mismo HTML

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--name",   required=True)
    parser.add_argument("--age",    required=True, type=int)
    parser.add_argument("--sex",    required=True, choices=["F","M"])
    parser.add_argument("--height", required=True, type=float)
    parser.add_argument("--doctor", default="")
    parser.add_argument("--output", default="reporte_tanita.html")
    args = parser.parse_args()

    patient = PatientInfo(args.name, args.age, args.sex, args.height)
    measurements = load_csv(args.csv)
    print(f"✓ {len(measurements)} mediciones ({measurements[0].date[:10]} → {measurements[-1].date[:10]})")

    result = analyze(patient, measurements)
    html = generate_html(result, args.doctor)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ HTML guardado: {args.output}")
    print(f"  Abrí http://localhost:9999/{args.output} en Chrome")
    print(f"  Luego Cmd+P → Guardar como PDF → Sin márgenes → A4")

if __name__ == "__main__":
    main()
