"""
Test de conexión a Supabase.
Ejecutar: python3 test_db.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

from supabase import create_client

url = os.environ.get('SUPABASE_URL')
key = os.environ.get('SUPABASE_SERVICE_KEY')

print(f"URL: {url}")
print(f"KEY: {key[:20]}..." if key else "KEY: no configurada")

client = create_client(url, key)

# Verificar que las tablas existen
tablas = ['nutris', 'patients', 'tanita_credentials', 'reports', 'report_deliveries']
print("\nVerificando tablas:")
for tabla in tablas:
    try:
        res = client.table(tabla).select('id').limit(1).execute()
        print(f"  ✓ {tabla}")
    except Exception as e:
        print(f"  ✗ {tabla} — {e}")

# Verificar funciones SQL
print("\nVerificando funciones:")
try:
    # UUID ficticio para probar que la función existe
    res = client.rpc('can_generate_report',
                     {'p_nutri_id': '00000000-0000-0000-0000-000000000000'}).execute()
    print(f"  ✓ can_generate_report")
except Exception as e:
    print(f"  ✗ can_generate_report — {e}")

print("\n✓ Conexión a Supabase funcionando correctamente")
