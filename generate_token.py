#!/usr/bin/env python3
"""
Script para generar token seguro para autenticación interna.
Usar el mismo token en el microservicio y en vega-store-backend.
"""
import secrets


def generate_secure_token(length=32):
    return secrets.token_urlsafe(length)


if __name__ == "__main__":
    token = generate_secure_token()
    print("\n" + "=" * 60)
    print("TOKEN SEGURO GENERADO")
    print("=" * 60)
    print(f"\n{token}\n")
    print("=" * 60)
    print("\nConfigura este token en:")
    print("\n1. Microservicio (Fly.io):")
    print(f'   fly secrets set INTERNAL_AUTH_TOKEN="{token}"')
    print("\n2. vega-store-backend (.env):")
    print(f'   BINANCE_PAYMENT_INTERNAL_TOKEN="{token}"')
    print("\nIMPORTANTE: Guarda este token en un lugar seguro")
    print("=" * 60 + "\n")
