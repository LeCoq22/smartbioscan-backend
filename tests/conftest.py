"""
Configuración de pytest: mockea módulos pesados que no están disponibles
en el entorno de tests (mercadopago, db, playwright, etc.).
"""
import sys
from unittest.mock import MagicMock

# Módulos que no están instalados en el entorno de tests
_MOCK_MODULES = [
    "mercadopago",
    "db",
    "playwright",
    "playwright.async_api",
    "pandas",
    "matplotlib",
    "matplotlib.pyplot",
    "cryptography",
    "cryptography.fernet",
    "resend",
]

for _mod in _MOCK_MODULES:
    sys.modules.setdefault(_mod, MagicMock())
