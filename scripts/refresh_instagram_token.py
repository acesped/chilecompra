#!/usr/bin/env python3
"""Cuanto Paga Chile: verifica cada día y renueva un token Instagram Login cada 7 días.

Solo sirve para Instagram Login (graph.instagram.com). No recupera tokens
revocados ni puede asegurar que Meta no invalide una sesión posteriormente.
Dependencias: requests, PyNaCl. Nunca imprime tokens.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from nacl import encoding, public

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'CuantoPagaChile-IG-Lifecycle/3.1'})
TIMEOUT = (8, 22)
GH = 'https://api.github.com'
IG = 'https://graph.instagram.com'
SECRET_NAME = 'INSTAGRAM_ACCESS_TOKEN'


class ApiError(RuntimeError):
    def __init__(self, service, status, code=None, subcode=None):
        self.service, self.status, self.code, self.subcode = service, status, code, subcode
        super().__init__(f'{service} HTTP {status} (code={code}, subcode={subcode})')


def log(message):
    print(message, flush=True)


def summary(message):
    path = os.getenv('GITHUB_STEP_SUMMARY', '')
    if path:
        with open(path, 'a', encoding='utf-8') as file:
            file.write(message + '\n')


def need(key):
    value = os.environ.get(key, '').strip()
    if not value:
        raise RuntimeError('Falta variable o secreto ' + key)
    return value


def request_json(method, url, service, *, headers=None, params=None, payload=None):
    try:
        r = SESSION.request(method, url, headers=headers, params=params, json=payload,
                            timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise RuntimeError(f'{service}: fallo de red ({type(exc).__name__})') from None
    try:
        obj = r.json() if r.content else {}
    except ValueError:
        raise RuntimeError(f'{service}: respuesta no JSON, HTTP {r.status_code}') from None
    if not isinstance(obj, dict):
        raise RuntimeError(f'{service}: respuesta inesperada')
    if not r.ok or obj.get('error'):
        error = obj.get('error')
        error = error if isinstance(error, dict) else {}
        raise ApiError(service, r.status_code, error.get('code'), error.get('error_subcode'))
    return obj


def verify_ig(token, expected_username, expected_id):
    # Instagram Login: id puede diferir del user_id profesional usado al publicar.
    data = request_json('GET', f'{IG}/me', 'Instagram identidad',
                        params={'fields': 'id,user_id,username', 'access_token': token})
    username = str(data.get('username') or '').strip().lstrip('@').lower()
    if username != expected_username:
        raise RuntimeError(f'Identidad incorrecta: esperado @{expected_username}, '
                           f'Instagram devolvió @{username or "ausente"}. No se modifica el secreto.')
    ids = {str(data[k]) for k in ('id', 'user_id') if data.get(k) is not None}
    if expected_id not in ids:
        raise RuntimeError('IG_USER_ID no coincide con id ni user_id de Instagram; '
                           'no se modifica el secreto.')
    return 'user_id' if str(data.get('user_id')) == expected_id else 'id'


def gh_headers(token):
    return {'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'}


def gh_json(method, repo, path, token, *, payload=None):
    return request_json(method, f'{GH}/repos/{repo}{path}', 'GitHub',
                        headers=gh_headers(token), payload=payload)


def parse_utc(value):
    if not value:
        raise RuntimeError('GitHub no informa updated_at para INSTAGRAM_ACCESS_TOKEN')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        raise RuntimeError('Fecha updated_at no válida para INSTAGRAM_ACCESS_TOKEN') from None


def secret_metadata(repo, admin):
    return gh_json('GET', repo, f'/actions/secrets/{SECRET_NAME}', admin)


def save_secret(repo, admin, token):
    root = '/actions/secrets'
    keydata = gh_json('GET', repo, root + '/public-key', admin)
    public_key = public.PublicKey(keydata['key'].encode('ascii'), encoding.Base64Encoder())
    encrypted = base64.b64encode(public.SealedBox(public_key).encrypt(token.encode('utf-8'))).decode('ascii')
    gh_json('PUT', repo, root + '/' + SECRET_NAME, admin,
            payload={'encrypted_value': encrypted, 'key_id': keydata['key_id']})


def main():
    parser = argparse.ArgumentParser(description='Cuanto Paga Chile Instagram token lifecycle')
    parser.add_argument('--force-refresh', action='store_true',
                        help='Ignora período de 7 días, sin saltar validaciones ni reglas de Meta')
    parser.add_argument('--check-only', action='store_true', help='Solo verificar token')
    opts = parser.parse_args()
    if os.getenv('IG_LOGIN_TYPE', 'instagram').lower() != 'instagram':
        raise RuntimeError('Este script requiere IG_LOGIN_TYPE=instagram')
    username = need('IG_EXPECTED_USERNAME').lstrip('@').lower()
    if username != 'cuantopagachile':
        raise RuntimeError('Protección: este renovador solo opera para @cuantopagachile')
    repo = need('GITHUB_REPOSITORY')
    if not re.fullmatch(r'[A-Za-z\d_.-]+/[A-Za-z\d_.-]+', repo):
        raise RuntimeError('GITHUB_REPOSITORY no válido')
    if repo.lower() != 'acesped/chilecompra':
        raise RuntimeError('Protección: este renovador opera exclusivamente en acesped/chilecompra')
    token = need(SECRET_NAME)
    user_id = need('IG_USER_ID')
    publish_enabled = os.getenv('PUBLISH_ENABLED', '').strip().lower()
    if publish_enabled and publish_enabled not in ('true', 'false', '1', '0', 'yes', 'no'):
        raise RuntimeError('PUBLISH_ENABLED debe ser true o false (o 1/0, yes/no)')
    log('Instagram: comprobando token e identidad de @' + username)
    log('Publicacion habilitada: ' + (publish_enabled or 'no configurado') + '; el refresh es independiente')
    try:
        matched = verify_ig(token, username, user_id)
    except ApiError as exc:
        if exc.code == 190:
            summary('❌ Instagram: OAuth 190. Token inválido; reautorizar la cuenta. No se renovó ni modificó ningún secreto.')
            raise RuntimeError('Meta rechazó el token actual (OAuth 190): '
                               'requiere reautorización. El refresh no lo recupera.') from None
        raise
    log(f'Instagram: token válido, identidad correcta (coincide con {matched})')
    if opts.check_only and opts.force_refresh:
        log('AVISO: --check-only tiene prioridad sobre --force-refresh; no se modificaran secretos.')
    if opts.check_only:
        summary('✅ Instagram: token vigente; comprobación sin renovación.')
        return

    # GitHub updated_at es la hora de escritura del secreto, no necesariamente
    # la hora de emisión ni la fecha real de vencimiento del token.
    admin = need('GH_SECRETS_TOKEN')
    before = secret_metadata(repo, admin)
    updated_at = parse_utc(before.get('updated_at'))
    age = datetime.now(timezone.utc) - updated_at
    log(f'GitHub: secreto actualizado hace {max(0, age.total_seconds()/86400):.1f} días')
    if not opts.force_refresh and age < timedelta(days=7):
        log('Renovación no necesaria: próxima evaluación diaria.')
        summary(f'✅ Instagram vigente. Secreto escrito hace {max(0, age.total_seconds()/86400):.1f} días; '
                'renovación semanal aún no corresponde.')
        return
    if age < timedelta(days=1):
        log('Renovación aplazada: el secreto se actualizó hace menos de 24 horas. '
            'Esto no prueba la edad del token, pero evita reintentos prematuros.')
        summary('ℹ️ Token válido; se aplaza renovación preventiva.')
        return

    log('Instagram: solicitando renovación preventiva de token válido')
    try:
        result = request_json('GET', f'{IG}/refresh_access_token', 'Instagram refresh',
                              params={'grant_type': 'ig_refresh_token', 'access_token': token})
    except ApiError as exc:
        summary('⚠️ Token validado, pero Meta rechazó la renovación; secreto sin cambios.')
        raise RuntimeError('Meta no aceptó la renovación (' + str(exc) + '); '
                           'revisar antigüedad, permisos y tipo de token.') from None
    new_token = result.get('access_token')
    expires = result.get('expires_in')
    if not isinstance(new_token, str) or len(new_token) < 30:
        raise RuntimeError('Instagram no devolvió access_token válido')
    if not isinstance(expires, (int, float)) or expires <= 0:
        raise RuntimeError('Instagram no devolvió expires_in positivo')
    # Enmascaramiento adicional para evitar exposición accidental posterior.
    log('::add-mask::' + new_token)
    verify_ig(new_token, username, user_id)
    # Detectar cambios concurrentes. Sin compare-and-swap atómico en Secrets API,
    # la concurrencia común entre workflows es la defensa principal.
    latest = secret_metadata(repo, admin)
    if latest.get('updated_at') != before.get('updated_at'):
        raise RuntimeError('El secreto fue modificado por otro proceso; no sobrescribir.')
    save_secret(repo, admin, new_token)
    after = secret_metadata(repo, admin)
    if not after.get('updated_at') or after.get('updated_at') == before.get('updated_at'):
        raise RuntimeError('GitHub no confirmó un cambio de metadatos tras la actualización')
    log('Instagram: renovación aceptada y escritura de GitHub Secrets completada')
    summary(f'✅ @cuantopagachile: token renovado, validado y escrito en GitHub. '
            f'Vigencia informada por Meta: {int(expires // 86400)} días. '
            'La lectura posterior de GitHub no permite verificar el valor del secreto.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as exc:
        log('ERROR: ' + str(exc))
        summary('❌ Cuanto Paga Chile: revisar el monitor del token y autorización de Meta.')
        sys.exit(1)
