"""Obtain a Google Ads API OAuth refresh token and store it in .env (never printed).

Prerequisites in .env: MARKETSCOPE_GOOGLE_ADS_CLIENT_ID and MARKETSCOPE_GOOGLE_ADS_CLIENT_SECRET
(Google Cloud Console > APIs & Services > Credentials > OAuth client ID, type "Desktop app",
with the Google Ads API enabled in the project).

Usage (from the project root):  .venv\\Scripts\\python tools\\google_ads_auth.py
A browser opens; sign in with the Google account that has access to your Google Ads accounts and
accept the "Google Ads" permission. The local loopback server receives the code and exchanges it."""
import hashlib
import base64
import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path
import httpx

ENV = Path(__file__).resolve().parents[1]/'.env'
AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/adwords'

def read_env():
    values={}
    for line in ENV.read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            k,v=line.split('=',1); values[k.strip()]=v.strip()
    return values

def write_env(key, value):
    lines=ENV.read_text(encoding='utf-8').splitlines()
    lines=[l for l in lines if not l.startswith(key+'=')]+[f'{key}={value}']
    ENV.write_text('\n'.join(lines)+'\n',encoding='utf-8')

def main():
    env=read_env()
    client_id,client_secret=env.get('MARKETSCOPE_GOOGLE_ADS_CLIENT_ID'),env.get('MARKETSCOPE_GOOGLE_ADS_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise SystemExit('Faltan MARKETSCOPE_GOOGLE_ADS_CLIENT_ID / MARKETSCOPE_GOOGLE_ADS_CLIENT_SECRET en .env')
    state=secrets.token_urlsafe(24)
    verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result={}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get('state',[''])[0]!=state:
                self.send_response(400); self.end_headers(); self.wfile.write(b'state invalido'); return
            result.update(code=q.get('code',[''])[0],error=q.get('error',[''])[0])
            self.send_response(200); self.send_header('Content-Type','text/plain; charset=utf-8'); self.end_headers()
            self.wfile.write('Listo. Puedes cerrar esta pestaña y volver a la terminal.'.encode())
        def log_message(self,*args): pass

    server=http.server.HTTPServer(('127.0.0.1',0),Handler)
    redirect=f'http://127.0.0.1:{server.server_port}'
    url=AUTH+'?'+urllib.parse.urlencode({'client_id':client_id,'redirect_uri':redirect,'response_type':'code',
        'scope':SCOPE,'access_type':'offline','prompt':'consent','state':state,
        'code_challenge':challenge,'code_challenge_method':'S256'})
    thread=threading.Thread(target=server.handle_request,daemon=True); thread.start()
    print('Abriendo el navegador para autorizar el acceso a Google Ads...')
    print('Si no se abre, copia esta URL en el navegador:\n'+url)
    webbrowser.open(url)
    thread.join(timeout=300); server.server_close()
    if result.get('error') or not result.get('code'):
        raise SystemExit(f"No se obtuvo autorización ({result.get('error') or 'tiempo agotado'})")
    r=httpx.post(TOKEN,data={'code':result['code'],'client_id':client_id,'client_secret':client_secret,
                             'redirect_uri':redirect,'grant_type':'authorization_code','code_verifier':verifier},timeout=30)
    data=r.json()
    if r.status_code!=200 or 'refresh_token' not in data:
        raise SystemExit(f"Google rechazó el intercambio: {data.get('error')} {data.get('error_description','')}")
    write_env('MARKETSCOPE_GOOGLE_ADS_REFRESH_TOKEN',data['refresh_token'])
    print('Refresh token guardado en .env como MARKETSCOPE_GOOGLE_ADS_REFRESH_TOKEN (no se muestra).')

if __name__=='__main__':
    main()
