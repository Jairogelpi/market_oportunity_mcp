"""Bluesky post search (AT Protocol). Docs: https://docs.bsky.app/docs/api/app-bsky-feed-search-posts

Anonymous search is refused (checked live: 403 on public.api.bsky.app), so the connector opens an official
session with the user's own handle and an app password (Settings > Privacy and security > App passwords),
from MARKETSCOPE_BLUESKY_HANDLE / MARKETSCOPE_BLUESKY_APP_PASSWORD. The password and the session token
travel only in the request body / Authorization header, which are never logged. Options: lang (ISO 639-1),
since (YYYY-MM-DD), sort (top | latest). Post URLs use the author's DID, not the handle."""
import os
import re
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, iso_datetime

PDS = 'https://bsky.social'
SESSION = PDS+'/xrpc/com.atproto.server.createSession'
SEARCH = PDS+'/xrpc/app.bsky.feed.searchPosts'
LIMIT = 25

class Bluesky(Connector):
    evidence_kind = 'opinion'
    OPTIONS = {'lang':r'[a-z]{2}','since':r'\d{4}-\d{2}-\d{2}','sort':r'top|latest'}

    def __init__(self):
        self.handle=os.environ.get('MARKETSCOPE_BLUESKY_HANDLE','').strip()
        self.password=os.environ.get('MARKETSCOPE_BLUESKY_APP_PASSWORD','').strip()
        self.policy=Policy('bluesky',frozenset({'bsky.social'}),min_interval=1.0)

    def validate_query(self, query):
        if not (self.handle and self.password):
            raise ValueError('Bluesky no permite búsquedas anónimas: crea una contraseña de aplicación y define '
                             'MARKETSCOPE_BLUESKY_HANDLE y MARKETSCOPE_BLUESKY_APP_PASSWORD en .env. '
                             'No se ha enviado ninguna petición.')
        if len(query)>200: raise ValueError('Consulta demasiado larga (máx. 200 caracteres)')

    def search(self, fetch, query, options):
        session,_,_=fetch(SESSION,None,None,json_body={'identifier':self.handle,'password':self.password})
        token=session.get('accessJwt') if isinstance(session,dict) else None
        if not token: raise ConnectorFailure('Bluesky no devolvió token de sesión')
        params={'q':query,'limit':LIMIT,'sort':options.get('sort','top')}
        if 'lang' in options: params['lang']=options['lang']
        if 'since' in options: params['since']=f"{options['since']}T00:00:00Z"
        data,_,_=fetch(SEARCH,params,{'Authorization':f'Bearer {token}'})
        if not isinstance(data,dict) or not isinstance(data.get('posts'),list):
            raise ConnectorFailure('Respuesta de Bluesky sin lista posts')
        context={'hits_total':data.get('hitsTotal')}
        return [h for h in (self._hit(p,context) for p in data['posts']) if h]

    def _hit(self, p, context):
        m=re.fullmatch(r'at://(did:[a-z]+:[A-Za-z0-9._:%\-]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)',str(p.get('uri','')))
        record=p.get('record') or {}
        text=' '.join(str(record.get('text','')).split())
        if not m or not text: return None
        did,rkey=m.groups()
        return Hit(url=f'https://bsky.app/profile/{did}/post/{rkey}',title=text[:120],
                   published_at=iso_datetime(record.get('createdAt')),snippet=clip(text),
                   independence_group=f'bluesky:{did}',
                   metadata={'langs':record.get('langs'),'likes':p.get('likeCount'),'replies':p.get('replyCount'),
                             'reposts':p.get('repostCount'),**context})
