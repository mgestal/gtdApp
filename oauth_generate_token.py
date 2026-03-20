#!/usr/bin/env python3
"""
Script para generar token de Google COMPLETAMENTE MANUAL - sin servidor local.
No se cuelga. Requiere copiar código manual.
"""

import os
import sys
import json
from pathlib import Path
import requests
from google.oauth2.credentials import Credentials

def main():
    credentials_path = Path("instance/gmail_credentials.json")
    token_path = Path("instance/gmail_token.json")
    
    if not credentials_path.exists():
        print(f"❌ Error: {credentials_path} no encontrado")
        sys.exit(1)
    
    # Lee el archivo de credenciales
    with open(credentials_path, 'r') as f:
        client_config = json.load(f)
    
    client_id = client_config['installed']['client_id']
    client_secret = client_config['installed']['client_secret']
    
    print("\n" + "="*80)
    print("GENERADOR DE TOKEN GOOGLE - COMPLETAMENTE MANUAL")
    print("="*80)
    
    # Paso 1: Genera la URL de autenticación
    auth_params = {
        'client_id': client_id,
        'scope': 'https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly',
        'response_type': 'code',
        'access_type': 'offline',
        'prompt': 'consent',
        'redirect_uri': 'http://localhost:9999/',
    }
    
    auth_url = 'https://accounts.google.com/o/oauth2/auth?' + '&'.join(
        f'{k}={v.replace(" ", "%20") if isinstance(v, str) else v}'
        for k, v in auth_params.items()
    )
    
    print("\n📋 PASO 1: Copia esta URL en tu navegador")
    print("-" * 80)
    print(auth_url)
    print("-" * 80)
    print("\n📝 PASO 2: Inicia sesión con gtdapp.inbox@gmail.com")
    print("📝 PASO 3: Haz clic en 'Aceptar'")
    print("❌ PASO 4: El navegador mostrará error (eso es NORMAL)")
    print("📋 PASO 5: En la barra del navegador verás: http://localhost:9999/?code=ABC123...")
    print("          Extrae SOLO lo que viene después de 'code='\n")
    
    auth_code = input("Pega aquí SOLO EL CÓDIGO (sin la URL completa): ").strip()
    
    if not auth_code:
        print("❌ No se proporcionó código")
        sys.exit(1)
    
    # Limpia el código en caso de que haya pegado toda la URL
    if 'code=' in auth_code:
        auth_code = auth_code.split('code=')[1].split('&')[0]
    
    print(f"\n✓ Código capturado: {auth_code[:30]}...")
    
    # Paso 2: Intercambia el código por el token
    print("\n⏳ Intercambiando código por token de acceso...")
    
    token_request = {
        'code': auth_code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': 'http://localhost:9999/',
        'grant_type': 'authorization_code',
    }
    
    try:
        response = requests.post(
            'https://oauth2.googleapis.com/token',
            data=token_request,
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"❌ Error del servidor de Google: {response.status_code}")
            print(f"   Respuesta: {response.text}")
            sys.exit(1)
        
        token_data = response.json()
        
        if 'error' in token_data:
            print(f"❌ Error de autenticación: {token_data.get('error_description', token_data['error'])}")
            sys.exit(1)
        
        # Guarda el token en el formato que espera google-auth
        token_path.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(token_path, 0o600)
        
        print(f"\n✅ Token generado exitosamente en {token_path}")
        print(f"   - Access token: {token_data.get('access_token', 'N/A')[:20]}...")
        print(f"   - Refresh token: {'✓ Presente' if 'refresh_token' in token_data else '✗ No presente'}")
        print(f"   - Expires in: {token_data.get('expires_in')} segundos")
        print(f"   - Scopes: {token_data.get('scope')}")
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error de conexión: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error inesperado: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
