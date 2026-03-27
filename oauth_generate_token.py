#!/usr/bin/env python3
"""
Script para generar token de Google con servidor local de callback.
Levanta un servidor HTTP temporal que captura el código OAuth automáticamente.
"""

import os
import sys
import json
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import requests
from google.oauth2.credentials import Credentials


def _find_free_port():
    """Devuelve un puerto local libre que no esté en uso."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        return s.getsockname()[1]


def _make_callback_handler(result):
    """Crea un handler HTTP que captura el código OAuth."""
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if 'code' in params:
                result['code'] = params['code'][0]
                body = b'<html><body><h2>&#10003; Autorizado. Puedes cerrar esta ventana y volver al terminal.</h2></body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif 'error' in params:
                result['error'] = params['error'][0]
                body = b'<html><body><h2>&#10007; Acceso denegado. Cierra esta ventana.</h2></body></html>'
                self.send_response(400)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(204)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # Silencia los logs de acceso

    return OAuthCallbackHandler


def main():
    credentials_path = Path("instance/gmail_credentials.json")
    token_path = Path("instance/gmail_token.json")

    if not credentials_path.exists():
        print(f"❌ Error: {credentials_path} no encontrado")
        sys.exit(1)

    with open(credentials_path, 'r') as f:
        client_config = json.load(f)

    client_id = client_config['installed']['client_id']
    client_secret = client_config['installed']['client_secret']

    redirect_port = 8081
    redirect_uri = 'http://localhost:8081/'

    print("\n" + "="*80)
    print("GENERADOR DE TOKEN GOOGLE - CON CAPTURA AUTOMÁTICA")
    print("="*80)
    print(f"\n🔌 Puerto para callback: {redirect_port}")

    auth_params = {
        'client_id': client_id,
        'scope': 'https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly',
        'response_type': 'code',
        'access_type': 'offline',
        'prompt': 'consent',
        'redirect_uri': redirect_uri,
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
    print("📝 PASO 3: Acepta los permisos solicitados")
    print("✅ El script capturará el código automáticamente al redirigir\n")
    print("⏳ Esperando callback del navegador (timeout 3 minutos)...")

    # Levanta el servidor y espera el callback
    result = {}
    server = HTTPServer(('localhost', redirect_port), _make_callback_handler(result))
    server.timeout = 180  # 3 minutos
    server.handle_request()

    if result.get('error'):
        print(f"❌ Acceso denegado por el usuario: {result['error']}")
        sys.exit(1)

    if 'code' not in result:
        print("❌ No se recibió el código de autorización (timeout o error inesperado)")
        sys.exit(1)

    auth_code = result['code']
    print(f"\n✓ Código capturado automáticamente: {auth_code[:30]}...")
    
    # Intercambia el código por el token
    print("\n⏳ Intercambiando código por token de acceso...")

    
    token_request = {
        'code': auth_code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
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

        # Guarda el token en el formato que espera google-auth (Credentials.from_authorized_user_file).
        # La respuesta bruta de Google usa 'access_token', pero google-auth espera 'token' + client_id/secret/token_uri.
        google_auth_token = {
            "token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": auth_params["scope"].split(" "),
        }
        token_path.write_text(json.dumps(google_auth_token, indent=2), encoding="utf-8")
        # Ajusta propietario y permisos para que Apache (www-data) pueda leerlo
        try:
            import pwd, grp
            www_uid = pwd.getpwnam("www-data").pw_uid
            www_gid = grp.getgrnam("www-data").gr_gid
            os.chown(token_path, www_uid, www_gid)
            os.chmod(token_path, 0o640)
            print("   - Propietario: www-data:www-data, permisos: 640")
        except Exception as perm_err:
            os.chmod(token_path, 0o644)
            print(f"   ⚠ No se pudo ajustar propietario a www-data ({perm_err}). Permisos: 644")

        print(f"\n✅ Token generado exitosamente en {token_path}")
        print(f"   - Access token: {token_data.get('access_token', 'N/A')[:20]}...")
        print(f"   - Refresh token: {'✓ Presente' if 'refresh_token' in token_data else '✗ No presente (¡necesario!)'}")
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
