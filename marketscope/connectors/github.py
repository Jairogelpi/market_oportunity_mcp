"""GitHub issue search (REST /search/issues). Docs: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api

Search allows ~10 requests/min unauthenticated and ~30/min with a token. Optional token from
MARKETSCOPE_GITHUB_TOKEN (fine-grained, public repositories read-only), sent as a bearer header.
Only issues (pull requests skipped). Author logins are not stored: only the numeric user id,
to group testimonies by author."""
import os
import re
import time
from datetime import datetime
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, html_text

API = 'https://api.github.com/search/issues'
PER_PAGE = 30

def _iso(value):
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00')).isoformat()
    except (AttributeError,ValueError):
        return 'unknown'

class GitHubIssues(Connector):
    evidence_kind = 'opinion'
    ENGLISH_FIRST = True
    OPTIONS = {'repo':r'[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}','state':r'open|closed',
               'sort':r'comments|reactions|created|updated'}

    def __init__(self):
        self.token=os.environ.get('MARKETSCOPE_GITHUB_TOKEN','').strip()
        self.policy=Policy('github_issues',frozenset({'api.github.com'}),min_interval=2.0 if self.token else 6.0)

    def search(self, fetch, query, options):
        q=query if re.search(r'\bis:(issue|pr|pull-request)\b',query) else f'{query} is:issue'
        for key in ('repo','state'):
            if key in options: q+=f' {key}:{options[key]}'
        params={'q':q,'per_page':PER_PAGE}
        if 'sort' in options: params.update(sort=options['sort'],order='desc')
        headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
        if self.token: headers['Authorization']=f'Bearer {self.token}'
        data,_,resp_headers=fetch(API,params,headers)
        if resp_headers.get('x-ratelimit-remaining')=='0' and (reset:=resp_headers.get('x-ratelimit-reset','')).isdigit():
            fetch.defer(max(0,int(reset)-time.time()))
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):
            raise ConnectorFailure('Respuesta de GitHub sin lista items')
        context={'total_count':data.get('total_count'),'incomplete_results':data.get('incomplete_results')}
        return [h for h in (self._hit(i,context) for i in data['items']) if h]

    def _hit(self, item, context):
        url=item.get('html_url'); title=' '.join((item.get('title') or '').split())
        if 'pull_request' in item or not isinstance(url,str) or not url.startswith('https://github.com/') or not title:
            return None
        repo='/'.join(str(item.get('repository_url','')).split('/')[-2:])
        user=(item.get('user') or {}).get('id')
        group=f'github:user:{user}' if isinstance(user,int) else f'github:issue:{repo}#{item.get("number")}'
        body=html_text(item.get('body'))
        return Hit(url=url,title=title,published_at=_iso(item.get('created_at')),
                   snippet=clip(f'{title}. {body}' if body else title),independence_group=group,
                   metadata={'repo':repo,'number':item.get('number'),'state':item.get('state'),
                             'comments':item.get('comments'),'reactions':(item.get('reactions') or {}).get('total_count'),
                             'labels':[l.get('name') for l in item.get('labels') or [] if isinstance(l,dict)],
                             'author_association':item.get('author_association'),**context})
