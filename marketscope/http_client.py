"""Hardened HTTP for connectors: host allowlist, public-IP check, no redirects, size/time limits,
per-source rate limits and quotas, and one audited `connector_call` event per request."""
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
from .core import EvidenceError, append_event, remaining_queries, normalize_url

TIMEOUT = 15
MAX_BYTES = 5*1024*1024
MAX_WAIT = 30  # never block the MCP longer than this waiting for a rate limit / backoff
CHALLENGE = re.compile(r'captcha|cf-chl|challenge-platform|are you a robot|verify you are human|unusual traffic|'
                       r'sign[ -]?in|log[ -]?in|iniciar sesi',re.I)
CLIENT = 'MarketScopeResearch/0.3'
PURPOSE = 'research prototype; respects documented API limits'

class BudgetExhausted(EvidenceError): pass
class StepBlocked(EvidenceError): pass       # access refused or challenged: terminal, never retried or evaded
class ConnectorFailure(EvidenceError): pass  # technical failure: retry only on explicit request
class NotFound(ConnectorFailure): pass        # HTTP 404: connectors may try an alternative resource

@dataclass(frozen=True)
class Policy:
    source_id: str
    allowed_hosts: frozenset
    min_interval: float            # seconds between requests to this source
    daily_quota: int | None = None  # requests per UTC day across all runs
    redact_params: frozenset = frozenset()
    timeout: float = TIMEOUT       # some official APIs (World Bank) answer in ~25 s
    # Env var holding the contact to declare in the User-Agent, only for APIs whose policy requires it
    # (SEC: MARKETSCOPE_CONTACT; Wikimedia: MARKETSCOPE_WIKIMEDIA_CONTACT). Never sent elsewhere.
    contact_env: str | None = None

def _json_error(body):
    """Short error description from a JSON error body (e.g. Stack Exchange error_name/error_message)."""
    try:
        data=json.loads(body[:20000])
    except ValueError:
        return ''
    if not isinstance(data,dict): return ''
    parts=[str(data[k]) for k in ('error_name','error_message','message','error') if isinstance(data.get(k),(str,int))]
    # Eurostat: {"error":[{"status":404,"label":"..."}]}; TED: {"error":{"type":"QUERY_SYNTAX_ERROR",...}}
    errors=data.get('error')
    for e in (errors if isinstance(errors,list) else [errors] if isinstance(errors,dict) else []):
        if isinstance(e,dict): parts+=[str(e[k]) for k in ('type','label') if isinstance(e.get(k),str)]
    return ' — '.join(parts)[:300]

def calls_today(db, source_id):
    today=datetime.now(timezone.utc).date().isoformat()
    # Connector step_id == source_id, so this counts every request made to the source today.
    return db.execute("SELECT COUNT(*) FROM research_events WHERE event_type='connector_call' AND step_id=? AND occurred_at>=?",
                      (source_id,today)).fetchone()[0]

class SafeHttp:
    def __init__(self, transport=None, resolver=socket.getaddrinfo, sleep=time.sleep, clock=time.monotonic):
        self.client=httpx.Client(transport=transport,follow_redirects=False,timeout=TIMEOUT)
        self.resolver,self.sleep,self.clock=resolver,sleep,clock
        self._last={}; self._not_before={}

    @staticmethod
    def user_agent(policy):
        """`<client>/<version> (<contact>; ...)` (Wikimedia format, accepted by SEC). The contact is read
        at request time, so it always matches what the connector validated."""
        contact=os.environ.get(policy.contact_env,'').strip() if policy.contact_env else ''
        return f'{CLIENT} ({contact}; {PURPOSE})' if contact else f'{CLIENT} ({PURPOSE})'

    def defer(self, source_id, seconds):
        """Honour a server-requested backoff for this source."""
        self._not_before[source_id]=max(self._not_before.get(source_id,0),self.clock()+float(seconds))

    def _check_host(self, policy, url):
        if url.scheme!='https': raise ConnectorFailure('Solo se permiten peticiones HTTPS')
        if url.host not in policy.allowed_hosts:
            raise ConnectorFailure(f'Host no permitido para {policy.source_id}: {url.host}')
        if url.port not in (None,443): raise ConnectorFailure('Puerto no permitido')
        try:
            addrs={info[4][0] for info in self.resolver(url.host,443,proto=socket.IPPROTO_TCP)}
        except OSError as ex:
            raise ConnectorFailure(f'Resolución DNS fallida para {url.host}: {ex}')
        if not addrs or any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addrs):
            raise ConnectorFailure(f'{url.host} resuelve a una dirección no pública')

    def _wait_turn(self, policy):
        sid=policy.source_id
        ready=max(self._not_before.get(sid,0),self._last.get(sid,float('-inf'))+policy.min_interval)
        wait=ready-self.clock()
        if wait>MAX_WAIT: raise ConnectorFailure(f'Backoff activo para {sid}: reintentar dentro de {wait:.0f} s')
        if wait>0: self.sleep(wait)

    def _logged_url(self, policy, url):
        for name in policy.redact_params: url=url.copy_remove_param(name)
        return normalize_url(str(url))

    def probe(self, policy, url, params=None, headers=None, json_body=None):
        """Maintenance request outside any research run (e.g. checking that a cited post still exists):
        same host, IP, redirect, size and rate-limit protections, no budget. Returns (status, json_or_None)."""
        req=httpx.URL(url,params=params or {})
        self._check_host(policy,req)
        self._wait_turn(policy)
        try:
            r=self.client.request('GET' if json_body is None else 'POST',req,json=json_body,timeout=policy.timeout,
                                  headers={'User-Agent':self.user_agent(policy),'Accept':'application/json',**(headers or {})})
        except httpx.HTTPError as ex:
            raise ConnectorFailure(f'Error de red: {type(ex).__name__}')
        finally:
            self._last[policy.source_id]=self.clock()
        if len(r.content)>MAX_BYTES: raise ConnectorFailure('Respuesta demasiado grande')
        try:
            data=r.json() if 'json' in r.headers.get('content-type','') else None
        except ValueError:
            data=None
        return r.status_code,data

    def get_json(self, db, run_id, step_id, policy, url, params=None, headers=None, query='', json_body=None, expect='json',
                 on_body=None):
        """One budgeted GET (or POST when json_body is given, e.g. TED search).
        Returns (data, content_sha256, response_headers); with expect='text' (archived HTML pages) data is the
        decoded body. Headers and bodies are never logged; `query` is."""
        if remaining_queries(db,run_id)<=0: raise BudgetExhausted('Presupuesto de consultas del run agotado')
        if policy.daily_quota is not None and calls_today(db,policy.source_id)>=policy.daily_quota:
            raise StepBlocked(f'Cuota diaria de {policy.source_id} agotada ({policy.daily_quota} peticiones)')
        req=httpx.URL(url,params=params or {})
        self._check_host(policy,req)
        self._wait_turn(policy)
        status=error=None; body=b''; too_big=False; resp_headers=httpx.Headers()
        try:
            with self.client.stream('GET' if json_body is None else 'POST',req,json=json_body,timeout=policy.timeout,
                                    headers={'User-Agent':self.user_agent(policy),'Accept':'application/json',**(headers or {})}) as r:
                status,resp_headers=r.status_code,r.headers
                for chunk in r.iter_bytes():
                    body+=chunk
                    if len(body)>MAX_BYTES: too_big=True; break
        except httpx.HTTPError as ex:
            error=f'{type(ex).__name__}: {ex}'
        finally:
            self._last[policy.source_id]=self.clock()
        sha=hashlib.sha256(body).hexdigest() if body else None
        append_event(db,run_id,'connector_call',query=query,source_url=self._logged_url(policy,req),
                     reason=error or f'{len(body)} bytes',step_id=step_id,http_status=status,content_sha256=sha)
        db.commit()
        if error: raise ConnectorFailure(f'Error de red: {error}')
        if too_big: raise ConnectorFailure(f'Respuesta supera {MAX_BYTES} bytes; descartada')
        if 300<=status<400:
            location=resp_headers.get('location','')[:200]
            raise ConnectorFailure(f'Redirección HTTP {status} no seguida'+(f' (destino: {location})' if location else ''))
        detail=_json_error(body) if status>=400 else ''
        # GitHub signals rate limits with 403 too; those are temporary, not access refusals.
        if status==429 or (status==403 and re.search(r'rate limit',detail,re.I)):
            # Temporary rate limit: retry only explicitly and after the server-requested wait.
            ra=resp_headers.get('retry-after','')
            if ra.isdigit(): self.defer(policy.source_id,int(ra))
            raise ConnectorFailure(f'Límite temporal (HTTP {status}){f"; esperar {ra} s antes de retry" if ra.isdigit() else ""}')
        if status in (401,403):
            raise StepBlocked(f'Acceso denegado (HTTP {status}); no se reintenta ni se elude')
        if status==404: raise NotFound('HTTP 404'+(f': {detail}' if detail else ''))
        if status>=400:
            if re.search(r'throttle|quota',detail,re.I):
                raise StepBlocked(f'Límite de la API alcanzado (HTTP {status}: {detail}); no se reintenta ni se elude')
            raise ConnectorFailure(f'HTTP {status}'+(f': {detail}' if detail else ''))
        if on_body and body:
            # Successful raw response, before any interpretation (Bronze layer).
            on_body(body,sha,self._logged_url(policy,req),resp_headers.get('content-type',''))
        if expect=='text':
            # Archived pages legitimately contain "log in"/"sign in": no challenge heuristics here.
            charset=(re.search(r'charset=([\w\-]+)',resp_headers.get('content-type','')) or [None,'utf-8'])[1]
            try:
                return body.decode(charset,'replace'),sha,resp_headers
            except LookupError:
                return body.decode('utf-8','replace'),sha,resp_headers
        if 'json' not in resp_headers.get('content-type',''):
            if CHALLENGE.search(body[:20000].decode('utf-8','replace')):
                raise StepBlocked('Respuesta con login o CAPTCHA en lugar de datos; no se elude')
            raise ConnectorFailure('Respuesta no JSON')
        try:
            return json.loads(body),sha,resp_headers
        except ValueError:
            raise ConnectorFailure('JSON inválido')
