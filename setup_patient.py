"""
SmartTanita — setup_patient.py
Crea un paciente en Supabase con sus credenciales MyTanita encriptadas.
Usar para el alta manual de pacientes hasta tener el frontend.

Uso:
    python3 setup_patient.py \
        --nutri-id e1883327-d219-4ba5-a305-0135efb2ab57 \
        --name "Stella Calderon" \
        --tanita-email Calderonstellas@gmail.com \
        --tanita-password @Dianita288 \
        --phone "+5491155551234"
"""

import argparse, sys, os
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from db import DB


def main():
    parser = argparse.ArgumentParser(
        description='Alta manual de paciente en SmartTanita'
    )
    parser.add_argument('--nutri-id',        required=True)
    parser.add_argument('--name',            required=True)
    parser.add_argument('--tanita-email',    required=True)
    parser.add_argument('--tanita-password', required=True)
    parser.add_argument('--phone',           default=None,
                        help='WhatsApp del paciente (opcional)')
    args = parser.parse_args()

    db = DB()

    # Verificar que el Nutri existe
    nutri = db.get_nutri(args.nutri_id)
    if not nutri:
        print(f"✗ Nutri no encontrado: {args.nutri_id}")
        sys.exit(1)
    print(f"✓ Nutri: {nutri['full_name']}")

    # Verificar quota de pacientes
    active = db.count_active_patients(args.nutri_id)
    max_p  = nutri['max_patients']
    if active >= max_p:
        print(f"✗ Límite de pacientes alcanzado ({active}/{max_p})")
        sys.exit(1)
    print(f"  Pacientes activos: {active}/{max_p}")

    # Crear paciente (sin datos de altura/edad aún — los leerá del primer scrape)
    patient = db.create_patient(args.nutri_id, {
        'full_name':      args.name,
        'phone_whatsapp': args.phone,
    })
    patient_id = patient['id']
    print(f"✓ Paciente creado: {patient_id}")

    # Guardar credenciales encriptadas
    db.upsert_tanita_credentials(
        patient_id = patient_id,
        email      = args.tanita_email,
        password   = args.tanita_password,
    )
    print(f"✓ Credenciales guardadas (encriptadas)")

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Paciente listo para generar reporte

  python3 pipeline_v2.py \\
    --patient-id {patient_id} \\
    --nutri-id {args.nutri_id} \\
    --doctor "{nutri['full_name']}" \\
    --output reporte_{args.name.split()[0].lower()}.pdf
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


if __name__ == '__main__':
    main()
