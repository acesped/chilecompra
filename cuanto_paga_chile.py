
#!/usr/bin/env python3
# CUANTO PAGA CHILE? - GitHub Actions + Instagram API con Instagram Login
# Sin ticket de ChileCompra ni IA: datos abiertos OCDS publicados por OCP.
# Publica solo diferencias estadisticas entre adjudicaciones del archivo parcial,
# NO compara con precios actuales de mercado.

import os, re, sys, json, zlib, math, random, time, zipfile, hashlib, statistics, argparse, subprocess, base64
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests
from PIL import Image, ImageFont, ImageDraw

ROOT=Path(__file__).resolve().parent
# Solo el archivo .py y el workflow .yml viven en el repositorio.
# media/ es una carpeta TEMPORAL creada por GitHub Actions (no se confirma en git).
CARPETA=Path(os.getenv('RUNNER_TEMP','/tmp'))/'cuanto_paga_chile'
CARPETA.mkdir(parents=True,exist_ok=True)
MEDIA=CARPETA/'site'/'media'
MEDIA.mkdir(parents=True,exist_ok=True)
(CARPETA/'site'/'index.html').write_text('<!doctype html><html lang="es"><meta charset="utf-8"><title>Cuánto Paga Chile</title>',encoding='utf8')
ANIO=int(os.getenv('ANIO',str(datetime.now(ZoneInfo('America/Santiago')).year)))
MAX_MB_DESCARGA=int(os.getenv('MAX_MB_DESCARGA','45'))
MAX_REGISTROS=int(os.getenv('MAX_REGISTROS','15000'))
CASOS_POR_EJECUCION=int(os.getenv('CASOS_POR_EJECUCION','1'))
URL=f'https://data.open-contracting.org/es/publication/144/download?name={ANIO}.jsonl.gz'
URL_REPLICA=f'https://fastly.data.open-contracting.org/downloads/chile_compra_api_releases/3730/{ANIO}.jsonl.gz'
GRAPH_VERSION=os.getenv('META_GRAPH_VERSION','v25.0')
GRAPH=f'{os.getenv("IG_GRAPH_BASE","https://graph.instagram.com").rstrip("/")}/{GRAPH_VERSION}'
MEDIA_BRANCH=os.getenv('IG_MEDIA_BRANCH','instagram-media')
MEDIA_PATH='cuanto_paga_chile/imagenes'
STATE_ISSUE_TITLE='[BOT] Estado de publicaciones Cuánto Paga Chile'

def media_url(name):
    """URL permanente de la imagen en la rama de medios, independiente de Pages."""
    repo=os.getenv('GITHUB_REPOSITORY','')
    if '/' not in repo:
        raise RuntimeError('GITHUB_REPOSITORY no configurado')
    return (f'https://raw.githubusercontent.com/{repo}/'
            f'{quote(MEDIA_BRANCH,safe="")}/{MEDIA_PATH}/{quote(name)}')


def legacy_pages_url(name):
    base=os.getenv('GH_PAGES_BASE_URL','').rstrip('/')
    if not base:
        repo=os.getenv('GITHUB_REPOSITORY','')
        owner,repository=repo.split('/',1)
        base=(f'https://{owner}.github.io/{repository}'
              if repository.lower()!=f'{owner.lower()}.github.io'
              else f'https://{owner}.github.io')
    return f'{base}/media/{quote(name)}'


def github_api(method,path,*,data=None):
    repo=os.getenv('GITHUB_REPOSITORY','')
    token=os.getenv('GITHUB_TOKEN','')
    if not repo or not token:
        raise RuntimeError('Se requieren GITHUB_TOKEN y GITHUB_REPOSITORY')
    r=requests.request(method,f'https://api.github.com/repos/{repo}{path}',
        headers={'Authorization':f'Bearer {token}',
                 'Accept':'application/vnd.github+json',
                 'X-GitHub-Api-Version':'2022-11-28'},
        json=data,timeout=60)
    if r.status_code>=400:
        raise RuntimeError(f'GitHub {method} {path}: HTTP {r.status_code} '
                           f'{r.text[:220]}')
    return r.json() if r.content else None


def ensure_media_branch():
    """Crear rama de medios una sola vez; jamás modificar la rama principal."""
    repo=os.getenv('GITHUB_REPOSITORY','')
    details=github_api('GET','')
    if details.get('private'):
        raise RuntimeError('Para URLs raw públicas, el repositorio debe ser público')
    ref=quote(MEDIA_BRANCH,safe='')
    token=os.getenv('GITHUB_TOKEN','')
    resp=requests.get(f'https://api.github.com/repos/{repo}/git/ref/heads/{ref}',
        headers={'Authorization':f'Bearer {token}',
                 'Accept':'application/vnd.github+json'},timeout=30)
    if resp.status_code==200:
        return
    if resp.status_code!=404:
        raise RuntimeError(f'Error consultando rama de imágenes: HTTP {resp.status_code}')
    default_branch=details.get('default_branch')
    base=github_api('GET','/git/ref/heads/'+quote(default_branch,safe=''))
    sha=base['object']['sha']
    try:
        github_api('POST','/git/refs',data={
            'ref':f'refs/heads/{MEDIA_BRANCH}', 'sha':sha})
        print('Rama pública de imágenes creada:',MEDIA_BRANCH)
    except RuntimeError:
        # Resolver un posible conflicto de creación concurrente.
        check=requests.get(f'https://api.github.com/repos/{repo}/git/ref/heads/{ref}',
            headers={'Authorization':f'Bearer {token}'},timeout=30)
        if check.status_code!=200:
            raise


def put_media(name,path):
    """Conservar cada JPG por nombre inmutable, como hace SISMONAUTA."""
    ensure_media_branch()
    repo=os.getenv('GITHUB_REPOSITORY','')
    filepath=f'{MEDIA_PATH}/{name}'
    endpoint='/contents/'+quote(filepath,safe='/')
    data=path.read_bytes()
    token=os.getenv('GITHUB_TOKEN','')
    headers={'Authorization':f'Bearer {token}',
             'Accept':'application/vnd.github+json',
             'X-GitHub-Api-Version':'2022-11-28'}
    query={'ref':MEDIA_BRANCH}
    r=requests.get(f'https://api.github.com/repos/{repo}{endpoint}',
                   headers=headers,params=query,timeout=30)
    if r.status_code==200:
        existing=r.json()
        # Blob SHA permite comprobar si el contenido ya es el mismo.
        blob='blob '+str(len(data))+'\0'
        git_sha=hashlib.sha1(blob.encode('utf8')+data).hexdigest()
        if existing.get('sha')!=git_sha:
            raise RuntimeError('La imagen remota tiene otro contenido; no sobrescribir')
        return media_url(name)
    if r.status_code!=404:
        raise RuntimeError(f'Error leyendo imagen remota: HTTP {r.status_code}')
    body={'message':f'Cuánto Paga Chile: imagen {name}',
          'branch':MEDIA_BRANCH,'content':base64.b64encode(data).decode('ascii')}
    try:
        github_api('PUT',endpoint,data=body)
    except Exception:
        # El PUT pudo confirmarse remotamente aunque su respuesta se perdiera.
        check=requests.get(f'https://api.github.com/repos/{repo}{endpoint}',
                           headers=headers,params=query,timeout=30)
        if check.status_code==200:
            existing=check.json()
            git_sha=hashlib.sha1((f'blob {len(data)}\0').encode()+data).hexdigest()
            if existing.get('sha')==git_sha:
                return media_url(name)
        raise
    return media_url(name)

# Historial persistente sin archivos adicionales en el repositorio: Issue de GitHub.
# Requiere habilitar Issues y conceder "issues: write" al workflow.
def issue_request(method,route,*,body=None):
    token=os.getenv('GITHUB_TOKEN')
    repo=os.getenv('GITHUB_REPOSITORY')
    if not token or not repo:
        raise RuntimeError('GITHUB_TOKEN y GITHUB_REPOSITORY son necesarios para mantener historial sin archivos extra')
    url=f'https://api.github.com/repos/{repo}{route}'
    r=requests.request(method,url,headers={
        'Authorization':f'Bearer {token}',
        'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28'},json=body,timeout=40)
    if r.status_code>=400:
        raise RuntimeError(f'GitHub API ({r.status_code}): {r.text[:400]}')
    return r.json() if r.content else None

ISSUE_NUMBER=None

def state_load():
    global ISSUE_NUMBER
    matches=issue_request('GET','/issues?state=all&per_page=100')
    for it in matches:
        if it.get('title')==STATE_ISSUE_TITLE and 'pull_request' not in it:
            ISSUE_NUMBER=it['number']
            try:
                data=json.loads(it.get('body') or '{}')
            except ValueError:
                raise RuntimeError('El issue de estado contiene JSON inválido; revísalo antes de seguir')
            return {'history':data.get('history',[]),'pending':data.get('pending',[]),'abandoned':data.get('abandoned',[]),'uncertain':data.get('uncertain',[])}
    created=issue_request('POST','/issues',body={
        'title':STATE_ISSUE_TITLE,
        'body':json.dumps({'history':[],'pending':[],'abandoned':[],'uncertain':[]},ensure_ascii=False,indent=2),
        'labels':[]})
    ISSUE_NUMBER=created['number']
    return {'history':[],'pending':[],'abandoned':[],'uncertain':[]}

def state_save(data):
    if not ISSUE_NUMBER:
        raise RuntimeError('Primero es necesario cargar el issue de historial')
    issue_request('PATCH',f'/issues/{ISSUE_NUMBER}',body={
        'body':json.dumps(data,ensure_ascii=False,indent=2)})

STATE=None

def init_state():
    global STATE
    if STATE is None:
        STATE=state_load()
    return STATE


def restore_pending_media():
    """Restaurar pendientes desde la rama de medios o el sitio Pages antiguo.

    No se reintenta ni se abandona una operación con estado incierto.
    """
    state=init_state()
    if not state.get('pending'):
        return []
    recoverable=[]
    changed=False
    abandoned=state.setdefault('abandoned',[])
    for job in state['pending']:
        status=job.get('instagram_status','pending')
        if status in ('uncertain','sending'):
            print('SIN REINTENTO: publicación incierta:',job.get('ocid'))
            recoverable.append(job)
            continue
        path=MEDIA/job['media_name']
        problem=None
        if not path.is_file():
            for url in (media_url(job['media_name']),legacy_pages_url(job['media_name'])):
                try:
                    r=requests.get(url,timeout=35)
                    r.raise_for_status()
                    if not r.headers.get('content-type','').lower().startswith('image/'):
                        raise ValueError('El recurso remoto no es una imagen')
                    path.write_bytes(r.content)
                    with Image.open(path) as img:img.verify()
                    problem=None
                    print('Imagen pendiente recuperada:',url)
                    break
                except (requests.RequestException,OSError,ValueError) as exc:
                    problem=f'{type(exc).__name__}: {str(exc)[:150]}'
                    path.unlink(missing_ok=True)
        if problem:
            changed=True
            if job.get('published',{}).get('instagram'):
                if job['unique_id'] not in state['history']:
                    state['history'].append(job['unique_id'])
            elif not any(x.get('unique_id')==job['unique_id'] for x in abandoned):
                abandoned.append({'unique_id':job['unique_id'],'ocid':job.get('ocid',''),
                    'status':'NO_PUBLICADO','reason':'Imagen histórica ausente: '+problem})
            print('Imagen antigua no recuperable; caso archivado:',job.get('ocid'))
            continue
        recoverable.append(job)
    if changed:
        state['pending']=recoverable
        state_save(state)
    return recoverable

def require_env(*keys):
    missing=[k for k in keys if not os.getenv(k)]
    if missing:raise RuntimeError('Faltan GitHub Secrets: '+', '.join(missing))


def instagram_credentials():
    # Compatibilidad con los dos nombres de credenciales, sin exigir cambiar Secrets.
    token=(os.getenv('INSTAGRAM_ACCESS_TOKEN') or os.getenv('INSTAGRAM_TOKEN') or '').strip()
    user=(os.getenv('IG_USER_ID') or os.getenv('INSTAGRAM_USER_ID') or '').strip()
    if not token or not user:
        raise RuntimeError('Faltan INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_TOKEN o IG_USER_ID/INSTAGRAM_USER_ID')
    if token.lower().startswith('bearer ') or any(ch.isspace() for ch in token) or token[0] in ('"',"'"):
        raise RuntimeError('El secreto Instagram contiene formato inválido: usar solo el token, sin Bearer, comillas ni espacios')
    if not user.isdigit():
        raise RuntimeError('IG_USER_ID debe contener el ID numérico profesional de Instagram')
    return user,token


def meta_request(method,path,token,*,data=None,timeout=75):
    """Mismo mecanismo que SISMONAUTA: parámetro access_token en Meta API."""
    values={**(data or {}),'access_token':token}
    url=GRAPH+'/'+path.lstrip('/')
    r=requests.get(url,params=values,timeout=timeout) if method=='GET' else requests.post(url,data=values,timeout=timeout)
    try:body=r.json()
    except ValueError:
        raise RuntimeError(f'Instagram API respondió HTTP {r.status_code} sin JSON')
    if r.status_code>=400 or body.get('error'):
        err=body.get('error') or {}
        code=err.get('code','?')
        message=str(err.get('message','Error desconocido'))[:230]
        hint=(' Revise el secreto INSTAGRAM_ACCESS_TOKEN: Meta no puede interpretar el token.'
              if code==190 else '')
        raise RuntimeError(f'Instagram API HTTP {r.status_code} código {code}: {message}.{hint}')
    return body


def instagram_check():
    """Preflight sin publicar, inspirado en ig_check de SISMONAUTA."""
    user,token=instagram_credentials()
    try:
        info=meta_request('GET','me',token,data={'fields':'id,user_id,username'})
    except RuntimeError as exc:
        if 'user_id' not in str(exc).lower():
            raise
        info=meta_request('GET','me',token,data={'fields':'id,username'})
    account=str(info.get('username') or '').strip().lower().lstrip('@')
    expected=os.getenv('IG_EXPECTED_USERNAME','').strip().lower().lstrip('@')
    if expected and account!=expected:
        raise RuntimeError(f'Cuenta Instagram incorrecta: esperada @{expected}, obtenida @{account}')
    valid_ids={str(info.get(k) or '') for k in ('id','user_id')}
    if user not in valid_ids:
        direct=meta_request('GET',user,token,data={'fields':'id,username'})
        if str(direct.get('id'))!=user or str(direct.get('username','')).lower()!=account:
            raise RuntimeError('IG_USER_ID no coincide con el token autorizado')
    print(f'INSTAGRAM CHECK OK: @{account}; ID profesional verificado')
    return user,token


def check_public_image(url):
    for intento in range(15):
        try:
            r=requests.get(url,timeout=25,stream=True)
            try:
                if r.ok and r.headers.get('Content-Type','').lower().startswith('image/'):
                    return
                last=f'HTTP {r.status_code}'
            finally:r.close()
        except requests.RequestException as exc:
            last=type(exc).__name__
        if intento<14:time.sleep(4)
    raise RuntimeError('La imagen pública de GitHub no está disponible: '+last)


def instagram_publish(url,caption,user,token):
    # Solo invocar después de persistir status=uncertain, nunca reintentar a ciegas.
    container=meta_request('POST',f'{user}/media',token,
        data={'image_url':url,'caption':caption})['id']
    print('Instagram contenedor creado:',container)
    for _ in range(18):
        time.sleep(8)
        status=meta_request('GET',str(container),token,
            data={'fields':'status_code'}).get('status_code')
        if status=='FINISHED':break
        if status in ('ERROR','EXPIRED'):
            raise RuntimeError('Instagram rechazó contenedor: '+str(status))
    else:
        raise RuntimeError('Instagram no confirmó el procesamiento del contenedor')
    result=meta_request('POST',f'{user}/media_publish',token,
        data={'creation_id':container})
    if not result.get('id'):
        raise RuntimeError('Instagram no devolvió ID de publicación; revisar manualmente')
    return str(result['id'])


def publicar():
    state=init_state()
    if not state['pending']:
        print('No hay publicaciones pendientes.')
        return
    if os.getenv('PUBLISH_ENABLED','false').lower()!='true':
        print('VISTA PREVIA: publicación desactivada.')
        return
    user,token=instagram_check()   # VALIDAR ANTES DE CUALQUIER POST
    errores=[]
    for job in list(state['pending']):
        uid=job['unique_id']
        if job.get('published',{}).get('instagram'):
            if uid not in state['history']:state['history'].append(uid)
            state['pending'].remove(job)
            state_save(state)
            continue
        if job.get('instagram_status') in ('sending','uncertain'):
            print('SIN REINTENTO AUTOMÁTICO: envío incierto; revisar Instagram y GitHub Issue:',job.get('ocid'))
            continue
        if job.get('instagram_status','pending')!='pending':
            raise RuntimeError('Estado de Instagram desconocido: '+str(job.get('instagram_status')))
        try:
            image_file=MEDIA/job['media_name']
            if not image_file.is_file():
                restore_pending_media()
            if not image_file.is_file():
                raise RuntimeError('Imagen faltante; se requiere recuperar o generar')
            if job not in state['pending']:
                raise RuntimeError('Pendiente archivado durante recuperación; no se enviará')
            url=put_media(job['media_name'],image_file)
            check_public_image(url)
            job['image_url']=url
            state_save(state)
        except Exception as exc:
            errores.append(f'{job["ocid"]}: imagen/hosting: {str(exc)[:160]}')
            print('ERROR ANTES DEL ENVÍO:',errores[-1])
            continue
        # Se graba el estado antes del primer POST para evitar duplicados si
        # GitHub Actions muere tras la publicación pero antes de guardar su ID.
        job['instagram_status']='uncertain'
        job['started_at']=datetime.now(ZoneInfo('America/Santiago')).isoformat()
        state_save(state)
        try:
            post_id=instagram_publish(url,job['caption'],user,token)
            job.setdefault('published',{})['instagram']=post_id
            job['instagram_status']='published'
            state_save(state)
            if uid not in state['history']:state['history'].append(uid)
            state['pending'].remove(job)
            state_save(state)
            print('INSTAGRAM PUBLICADO:',post_id,'| Caso:',job['ocid'])
        except Exception as exc:
            job['instagram_error']=str(exc)[:250]
            try:state_save(state)
            except Exception as err:print('ERROR GUARDANDO ESTADO:',type(err).__name__)
            errores.append(f'{job["ocid"]}: {str(exc)[:160]}')
            print('PUBLICACIÓN INCIERTA; NO REINTENTAR A CIEGAS:',errores[-1])
    if errores:
        raise RuntimeError(f'{len(errores)} envío(s) fallaron o requieren revisión; consulte el historial de GitHub Issues')

def generar():
    state=init_state()
    preview = os.getenv('PUBLISH_ENABLED','false').lower() != 'true'
    if state['pending'] and preview:
        # Preservar envíos inciertos aunque se active temporalmente modo prueba.
        uncertain=[j for j in state['pending'] if j.get('instagram_status') in ('uncertain','sending')]
        for job in uncertain:
            if not any(x.get('unique_id')==job['unique_id'] for x in state.setdefault('uncertain',[])):
                state['uncertain'].append(job)
        print('Modo prueba: liberando solo pendientes sin envío; inciertos archivados.')
        state['pending'] = []
        state_save(state)
    elif state['pending']:
        recovered=restore_pending_media()
        safe=[j for j in recovered if j.get('instagram_status','pending')=='pending']
        if safe:
            print(f'Hay {len(safe)} caso(s) recuperados para publicación.')
            return
        # Ningún pendiente recuperable: buscar otra adjudicación automáticamente.
        print('Sin imágenes anteriores recuperables. Buscando una adjudicación nueva.')
    # No dejar que un caso de publicación incierta bloquee nuevas ediciones.
    # Se conserva íntegro en el historial para reconciliar manualmente.
    uncertain=[j for j in state['pending'] if j.get('instagram_status') in ('uncertain','sending')]
    if uncertain:
        for job in uncertain:
            if not any(x.get('unique_id')==job['unique_id'] for x in state.setdefault('uncertain',[])):
                state['uncertain'].append(job)
        state['pending']=[j for j in state['pending'] if j not in uncertain]
        state_save(state)
        print('Envíos inciertos archivados para revisión manual:',len(uncertain))
    # Descargar JSONL gzip por streaming hasta presupuesto: la descarga se corta en un límite.
    # Los registros JSON completos recuperados son genuinos; los registros truncados se descartan.
    def descargar_registros(url, max_mb=MAX_MB_DESCARGA, max_records=MAX_REGISTROS):
        print('Consultando conjunto abierto OCDS de ChileCompra (sin token):',url)
        r=requests.get(url,stream=True,timeout=(20,90),headers={'User-Agent':'ChileBajoLaLupaResearch/1.0 (datos abiertos)'})
        r.raise_for_status()
        dec=zlib.decompressobj(16+zlib.MAX_WBITS)
        buffer=b''; bytes_comprimidos=0; records=[]
        for chunk in r.iter_content(chunk_size=256*1024):
            if not chunk: continue
            bytes_comprimidos+=len(chunk)
            data=dec.decompress(chunk)
            buffer+=data
            *lineas,buffer=buffer.split(b'\n')
            for linea in lineas:
                if not linea.strip(): continue
                try:
                    records.append(json.loads(linea))
                except (ValueError,UnicodeDecodeError):
                    continue
                if len(records)>=max_records: break
            if bytes_comprimidos >= max_mb*1024*1024 or len(records)>=max_records:
                break
        r.close()
        print(f'Registros OCDS descargados: {len(records)} | Transferencia: {bytes_comprimidos/1024/1024:.1f} MB')
        if not records:
            raise RuntimeError('No se pudieron leer registros JSON reales de la fuente abierta.')
        return records

    registros=None; errores=[]
    for endpoint in (URL,URL_REPLICA):
        try:
            registros=descargar_registros(endpoint)
            if registros:
                URL_USADA=endpoint
                break
        except Exception as e:
            errores.append(str(e))
            print('La fuente no respondió:',str(e)[:170])
    if not registros:
        raise RuntimeError('No fue posible consultar datos abiertos reales. No se generan publicaciones ficticias. '+ ' | '.join(errores))

    # Compatibilidad OCDS: compiled releases y paquetes de releases.
    def iterar_releases(obj):
        if isinstance(obj,dict):
            if isinstance(obj.get('compiledRelease'),dict): yield obj['compiledRelease']
            elif isinstance(obj.get('releases'),list):
                for v in obj['releases']:
                    if isinstance(v,dict): yield v
            elif ('awards' in obj or 'tender' in obj): yield obj


    def num(v):
        if isinstance(v,(float,int)) and math.isfinite(v): return float(v)
        if isinstance(v,str):
            try:
                s=v.strip().replace(' ','')
                if ',' in s and '.' in s: s=s.replace('.','').replace(',','.')
                elif ',' in s: s=s.replace(',','.')
                return float(s)
            except ValueError: pass
        return None


    def normalizar(txt):
        import unicodedata
        txt=unicodedata.normalize('NFKD',str(txt).lower())
        txt=''.join(c for c in txt if not unicodedata.combining(c))
        return re.sub(r'\s+',' ',re.sub('[^a-z0-9 ]',' ',txt)).strip()


    def fmt(x): return '$'+format(round(x),',').replace(',','.')

    items=[]
    for rec in registros:
        for release in iterar_releases(rec):
            comprador=(release.get('buyer') or {}).get('name') or 'Organismo no indicado'
            ocid=release.get('ocid') or rec.get('ocid') or release.get('id') or ''
            fecha_registro=release.get('date') or ''
            for award in (release.get('awards') or []):
                if not isinstance(award,dict): continue
                # Usar solamente adjudicaciones; no confundir con compras efectivamente pagadas.
                if str(award.get('status','')).lower() in ('cancelled','canceled','unsuccessful'): continue
                suppliers=award.get('suppliers') or []
                proveedor=(suppliers[0].get('name') if suppliers and isinstance(suppliers[0],dict) else '') or 'No indicado'
                for it in (award.get('items') or []):
                    if not isinstance(it,dict): continue
                    desc=it.get('description') or ((it.get('classification') or {}).get('description')) or ''
                    cant=num(it.get('quantity'))
                    unidad=(it.get('unit') or {})
                    if not isinstance(unidad,dict): unidad={}
                    precio=(unidad.get('value') or {})
                    if not isinstance(precio,dict): precio={}
                    unit_amount=num(precio.get('amount'))
                    currency=precio.get('currency') or (award.get('value') or {}).get('currency')
                    unitname=unidad.get('name') or unidad.get('id') or ''
                    if not desc or not unit_amount or unit_amount<=0 or currency!='CLP': continue
                    if cant is not None and cant<=0: continue
                    key=normalizar(desc)
                    # Excluir descripciones demasiado genéricas: comparables deben tener identidad legible.
                    if len(key)<12 or len(key.split())<2: continue
                    items.append({'ocid':ocid,'award_id':award.get('id',''),'fecha_adjudicacion':award.get('date') or '', 'fecha_registro':fecha_registro,
                        'comprador':comprador,'proveedor':proveedor,'descripcion':desc,
                        'clave':key,'precio_unitario':unit_amount,'cantidad':cant,'unidad':normalizar(unitname),
                        'moneda':'CLP','fuente':URL_USADA})

    if not items:
        raise RuntimeError('El bloque descargado no contiene adjudicaciones con ítems y precio unitario CLP verificable. No se generará contenido engañoso.')

    # Selección: coincidencia EXACTA de descripción normalizada + unidad.
    # No implica igualdad de marca/modelo; por eso siempre se presenta como referencia orientativa.
    grupo=defaultdict(list)
    for it in items:
        grupo[(it['clave'],it['unidad'])].append(it)

    comparables=[]
    for key, rows in grupo.items():
        vals=list({r['ocid']: r for r in rows if r['ocid']}.values())
        if len(vals)<3: continue
        for candidato in vals:
            if not candidato.get('fecha_adjudicacion'): continue
            base=[r['precio_unitario'] for r in vals if r['ocid']!=candidato['ocid']]
            if len(base)<2 or min(base)<=0 or max(base)/min(base)>4: continue
            ref_c=statistics.median(base)
            dif_c=(candidato['precio_unitario']/ref_c-1)*100
            comparables.append({'caso':candidato,'ref':ref_c,'dif':dif_c,'nref':len(base),'comparacion':True})

    # También considerar adjudicaciones reales sin comparables, indicando que no hay porcentaje.
    # Se deduplica por proceso; se exige descripción, precio unitario CLP y fuente real.
    sin_comparacion=[]
    for it in items:
        if it.get('ocid'):
            sin_comparacion.append({'caso':it,'ref':None,'dif':None,'nref':None,'comparacion':False})

    # No volver a seleccionar siempre el valor máximo o el mayor porcentaje.
    # La aleatoriedad del sistema hace variar el orden entre ejecuciones, sin añadir datos falsos.
    random.SystemRandom().shuffle(comparables)
    random.SystemRandom().shuffle(sin_comparacion)

    historial=(set(state['history']) | {p['unique_id'] for p in state['pending']} |
               {p['unique_id'] for p in state.get('abandoned', [])} |
               {p['unique_id'] for p in state.get('uncertain', [])})

    vistos=set()
    candidatos=[]
    for entry in comparables:
        it=entry['caso']
        unique_id=f"{it['ocid']}::{it.get('award_id','')}::{it['clave']}"
        if unique_id in vistos:continue
        vistos.add(unique_id)
        if unique_id in historial:continue
        entry['unique_id']=unique_id
        candidatos.append(entry)

    if not candidatos:
        raise RuntimeError('Ya se utilizaron todos los candidatos del bloque descargado. '
            'Aumenta MAX_MB_DESCARGA / MAX_REGISTROS o usa otro ANIO para ampliar la muestra. '
            'No se borró el historial ni se repitieron casos.')

    seleccionados=[]
    procesos_elegidos=set()
    for entry in candidatos:
        expediente=entry['caso']['ocid']
        if expediente in procesos_elegidos:continue
        seleccionados.append(entry)
        procesos_elegidos.add(expediente)
        if len(seleccionados)>=CASOS_POR_EJECUCION:break

    print(f'Casos nuevos seleccionados: {len(seleccionados)} | Ya usados previamente: {len(historial)}')

    def generar_publicacion(entry, indice):
        caso=entry["caso"]
        ref=entry["ref"]
        dif=entry["dif"]
        nref=entry["nref"]
        comparacion=entry["comparacion"]
        destino=CARPETA / f"caso_{indice:02d}_{hashlib.sha256(entry['unique_id'].encode()).hexdigest()[:10]}"
        destino.mkdir(parents=True, exist_ok=True)
        # Metadatos del procedimiento: se usa SOLO la fecha award.date para afirmar adjudicación.
        # 'release.date' refleja la fecha del registro/publicación OCDS, no necesariamente la adjudicación.
        def fecha_legible(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value).replace('Z', '+00:00')).strftime('%d-%m-%Y')
            except (ValueError, TypeError):
                return None

        fecha_adj=fecha_legible(caso.get('fecha_adjudicacion'))
        fecha_reg=fecha_legible(caso.get('fecha_registro'))
        fecha_hoy=datetime.now(ZoneInfo('America/Santiago')).strftime('%d-%m-%Y')
        fuente_detalle='https://data.open-contracting.org/es/publication/144'

        # "Incumbentes" = proveedores observados en adjudicaciones ANTERIORES del
        # mismo artículo normalizado en la MUESTRA disponible, no del universo completo.
        proveedores_anteriores=[]
        if fecha_adj and caso.get('fecha_adjudicacion'):
            try:
                momento=datetime.fromisoformat(str(caso['fecha_adjudicacion']).replace('Z','+00:00'))
                for r in items:
                    if r['ocid']==caso['ocid'] or r['clave']!=caso['clave'] or r['unidad']!=caso['unidad']:
                        continue
                    if not r.get('fecha_adjudicacion') or not r.get('proveedor'):
                        continue
                    try:
                        anterior=datetime.fromisoformat(str(r['fecha_adjudicacion']).replace('Z','+00:00'))
                        if anterior.tzinfo is None and momento.tzinfo is not None:
                            anterior=anterior.replace(tzinfo=momento.tzinfo)
                        if momento.tzinfo is None and anterior.tzinfo is not None:
                            momento=momento.replace(tzinfo=anterior.tzinfo)
                    except (ValueError,TypeError):
                        continue
                    if anterior < momento:
                        proveedores_anteriores.append((r['proveedor'],r['ocid'],fecha_legible(r['fecha_adjudicacion'])))
            except (ValueError,TypeError):
                pass
        proveedores_anteriores=list({(p,ocid,fecha) for p,ocid,fecha in proveedores_anteriores})
        proveedores_anteriores.sort(key=lambda r:(r[0],r[1]))
        incumbentes=sorted({p for p,_,_ in proveedores_anteriores})

        proveedor_prev_mismo_comprador=[]
        if fecha_adj and caso.get('fecha_adjudicacion'):
            for r in items:
                if r['ocid']==caso['ocid'] or r['clave']!=caso['clave'] or r['unidad']!=caso['unidad']:
                    continue
                if r['comprador']!=caso['comprador'] or not r.get('fecha_adjudicacion'):
                    continue
                try:
                    if str(r['fecha_adjudicacion'])[:10] < str(caso['fecha_adjudicacion'])[:10]:
                        proveedor_prev_mismo_comprador.append(r['proveedor'])
                except Exception:
                    pass
        proveedor_prev_mismo_comprador=sorted(set(proveedor_prev_mismo_comprador))

        precio=fmt(caso['precio_unitario'])
        fecha_nota = (f'La adjudicación está fechada el {fecha_adj}. ' if fecha_adj else
                      (f'La fecha de adjudicación no consta con claridad en el registro; la fecha visible del registro es {fecha_reg}. ' if fecha_reg else
                       'La fecha de adjudicación no consta con claridad en el registro. '))

        if comparacion:
            comparacion_txt=(f'Para poner la cifra en contexto, contrastamos el precio unitario de {precio} '
                             f'con otras {nref} adjudicaciones del archivo descargado que presentan una descripción '
                             f'normalizada y unidad de medida idénticas. La mediana de esas referencias fue {fmt(ref)}, '
                             f'por lo que el precio reportado resulta {abs(dif):.1f}% '
                             f'{"superior" if dif>0 else "inferior" if dif<0 else "igual"} a ese parámetro. ')
        else:
            comparacion_txt=('En el segmento descargado no encontramos suficientes adjudicaciones comparables con descripción '
                             'normalizada y unidad de medida idénticas como para construir una referencia estadística sólida. ')

        if incumbentes:
            if len(incumbentes)==1:
                inc_txt=f'Entre los antecedentes previos observados para el mismo artículo aparece como proveedor anterior {incumbentes[0]}. '
            else:
                lista=', '.join(incumbentes[:3])
                inc_txt=(f'Entre los antecedentes previos observados para el mismo artículo aparecen como proveedores anteriores {lista}'
                         + (' y otros. ' if len(incumbentes)>3 else '. '))
        else:
            inc_txt='En la muestra descargada no identificamos proveedores anteriores verificables para el mismo artículo. '

        if proveedor_prev_mismo_comprador:
            mismos=', '.join(proveedor_prev_mismo_comprador[:3])
            comprador_hist=(f'Además, para el mismo organismo comprador figuran antecedentes previos de este artículo adjudicados a {mismos}'
                            + (' y otros proveedores. ' if len(proveedor_prev_mismo_comprador)>3 else '. '))
        else:
            comprador_hist=''

        texto=(
            '=== CUANTO PAGA CHILE? === 🔎 ¿Qué muestran los registros de esta adjudicación pública? '
            f'En los datos abiertos de compras públicas de Chile, publicados por Open Contracting Partnership a partir de información de ChileCompra, '
            f'encontramos el proceso {caso["ocid"]}, en el que figura {caso["comprador"]} como organismo comprador y {caso["proveedor"]} '
            f'como proveedor adjudicado. El artículo descrito como «{caso["descripcion"]}» registra un precio unitario de {precio} pesos chilenos. '
            + fecha_nota + comparacion_txt + inc_txt + comprador_hist +
            'NOTA: Las cifras corresponden a adjudicaciones, no necesariamente a pagos realizados. '
            f'Fuente: ChileCompra vía OCP ({fuente_detalle}). Consulta: {fecha_hoy}. '
            '#ComprasPúblicas #ChileCompra #DatosAbiertos #Transparencia #Chile'
        )

        (destino/'pie_publicacion.txt').write_text(texto,encoding='utf8')
        (destino/'respaldo.json').write_text(json.dumps({
            'fuente':URL_USADA,'fecha_consulta':fecha_hoy,
            'seleccion':caso,'fecha_adjudicacion_verificada':fecha_adj,
            'fecha_registro':fecha_reg,'comparacion_adjudicaciones':comparacion,
            'mediana_referencia':ref,'n_referencias':nref,'diferencia_pct':dif,
            'incumbentes_observados_en_muestra':incumbentes,
            'adjudicaciones_previas_observadas':[
                {'proveedor':p,'ocid':ocid,'fecha':f} for p,ocid,f in proveedores_anteriores
            ],
            'anteriores_mismo_comprador':proveedor_prev_mismo_comprador,
            'total_items_validos_en_muestra':len(items),
            'observacion':'Datos parciales del archivo anual OCDS; el referente es de adjudicaciones públicas, no de comercios.'
        },ensure_ascii=False,indent=2),encoding='utf8')

        W,H=1080,1350
        im=Image.new('RGB',(W,H),'#0a1223')
        d=ImageDraw.Draw(im)
        from functools import lru_cache
        from pathlib import Path

        @lru_cache(maxsize=32)
        def font(size,bold=False):
            names=['DejaVuSans-Bold.ttf','LiberationSans-Bold.ttf'] if bold else ['DejaVuSans.ttf','LiberationSans-Regular.ttf']
            roots=['/usr/share/fonts/truetype/dejavu','/usr/share/fonts/truetype/liberation2',
                   '/usr/share/fonts/truetype/liberation','/usr/local/lib/python3.13/site-packages/matplotlib/mpl-data/fonts/ttf',
                   '/usr/local/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf']
            for root in roots:
                for name in names:
                    path=Path(root)/name
                    if path.is_file():
                        try:return ImageFont.truetype(str(path),size)
                        except OSError:pass
            try:return ImageFont.load_default(size=size)
            except TypeError:return ImageFont.load_default()

        def lines_fit(txt,width,f):
            result=[];cur=''
            for word in str(txt).split():
                cand=f'{cur} {word}'.strip()
                if d.textbbox((0,0),cand,font=f)[2]<=width:
                    cur=cand
                else:
                    if cur:
                        result.append(cur)
                    cur=word
            if cur:
                result.append(cur)
            return result

        def block(txt,x,y,width,size,color,weight=False,max_lines=3,gap=9,align='left'):
            f=font(size,weight)
            lines=lines_fit(txt,width,f)
            if len(lines)>max_lines:
                lines=lines[:max_lines]
                while lines[-1] and d.textbbox((0,0),lines[-1]+'…',font=f)[2]>width:
                    lines[-1]=lines[-1][:-1]
                lines[-1]=lines[-1].rstrip()+'…'
            for s in lines:
                bbox=d.textbbox((0,0),s,font=f)
                tw=bbox[2]-bbox[0]
                xx=x if align=='left' else x+(width-tw)//2
                d.text((xx,y),s,font=f,fill=color)
                y+=size+gap
            return y

        def centered_text(y,text,size,color,bold=True,max_width=960):
            f=font(size,bold)
            while size>16:
                bbox=d.textbbox((0,0),text,font=f)
                tw=bbox[2]-bbox[0]
                if tw<=max_width:
                    break
                size-=2
                f=font(size,bold)
            bbox=d.textbbox((0,0),text,font=f)
            tw=bbox[2]-bbox[0]
            x=(W-tw)//2
            d.text((x,y),text,font=f,fill=color)
            return y + (bbox[3]-bbox[1])

        # Panel superior con cifras alineadas al centro
        d.rounded_rectangle((35,35,1045,670),radius=38,fill='#15243f')
        y=78
        y=block('PRECIO UNITARIO ADJUDICADO',60,y,960,28,'#c8d7f0',weight=True,max_lines=1,align='center')
        y+=18
        y=centered_text(y,precio,106,'#ffdf62',True,900)
        y+=52

        if comparacion:
            y=block('DIFERENCIA VS OTRAS ADJUDICACIONES',60,y,960,26,'#c8d7f0',weight=True,max_lines=1,align='center')
            y+=18
            diff_color = '#ff5c5c' if dif>0 else '#30d07d' if dif<0 else '#ffd84d'
            y=centered_text(y,f'{dif:+.1f}%' if dif!=0 else '0.0%',126,diff_color,True,900)
            y+=26
            y=block(f'Mediana de referencia: {fmt(ref)}',60,y,960,36,'#f4f7fc',weight=True,max_lines=1,align='center')
        else:
            y=block('SIN REFERENCIA HOMOGÉNEA',60,y,960,42,'#ffab72',weight=True,max_lines=1,align='center')

        # Sección inferior con el resto de los textos
        d.text((68,718),'DETALLES DE LA ADJUDICACIÓN',font=font(30,True),fill='#f1f5fb')
        d.line((68,766,1012,766),fill='#364760',width=2)
        y=792
        entries=[
            ('ARTÍCULO',caso['descripcion'],3),
            ('ORGANISMO',caso['comprador'],2),
            ('PROVEEDOR ADJUDICADO',caso['proveedor'],2),
            ('FECHA DE ADJUDICACIÓN',fecha_adj or 'No consta en el registro',1),
            ('IDENTIFICADOR',str(caso['ocid']),2),
        ]
        for key,value,maxlines in entries:
            if y>1190:
                break
            d.text((70,y),key,font=font(21,True),fill='#75baff')
            y+=30
            y=block(str(value),70,y,940,27,'#f1f5fb',max_lines=maxlines,gap=6)
            y+=15

        # Pie mínimo sin los textos eliminados por el usuario.
        d.rectangle((0,1260,1080,1350),fill='#07101e')
        footer1='Fuente: ChileCompra vía OCP'
        d.text((65,1296),footer1,font=font(21,True),fill='#ffd28a')

        img_path=destino/'imagen_instagram_facebook.png'
        im.save(img_path)

        print('\n=== PIE DE PUBLICACIÓN ===\n'+texto)
        print('Archivos del caso:',destino)
        return img_path


    nuevos=[]
    for i,entry in enumerate(seleccionados,1):
        uid=entry['unique_id']
        imagen=generar_publicacion(entry,i)
        name=hashlib.sha256(uid.encode('utf8')).hexdigest()[:24]+'.jpg'
        # Imagen pública exclusivamente para que Instagram pueda recuperarla.
        with Image.open(imagen) as img:
            img.convert('RGB').save(MEDIA/name,'JPEG',quality=92,optimize=True)
        caption=(imagen.parent/'pie_publicacion.txt').read_text(encoding='utf8')
        # Límite de caption Instagram para foto individual.
        if len(caption)>2200:
            raise RuntimeError(f'Pie de {entry["caso"]["ocid"]} supera los 2200 caracteres de Instagram. Reducir texto sin perder verificación.')
        nuevos.append({'unique_id':uid,'ocid':entry['caso']['ocid'],'media_name':name,
             'caption':caption,'instagram_status':'pending','published':{'instagram':None}})
    if not nuevos:
        raise RuntimeError('No se encontraron casos nuevos con comparación verificable. No se publicará nada.')
    state['pending']=nuevos
    state_save(state)
    print(f'{len(nuevos)} casos preparados para Instagram. Imágenes temporales: {MEDIA}.')


def main():
    p=argparse.ArgumentParser(description='Cuanto Paga Chile: generacion y publicacion')
    p.add_argument('command',choices=['generate','publish','check-instagram'])
    args=p.parse_args()
    if args.command=='generate': generar()
    elif args.command=='check-instagram': instagram_check()
    else: publicar()

if __name__=='__main__': main()
