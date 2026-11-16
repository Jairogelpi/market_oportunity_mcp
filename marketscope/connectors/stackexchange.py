"""Stack Exchange question search (API 2.3, /search/advanced). Docs: https://api.stackexchange.com/docs/throttle

Read-only search needs no key: 300 requests/day per IP without an app key, 10,000 with one.
A `backoff` field in a response obliges waiting that many seconds before calling again.
Optional app key from MARKETSCOPE_STACKEXCHANGE_KEY (redacted from logged URLs).
Owner names are not stored: only the numeric user_id, to group testimonies by author."""
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure, StepBlocked
from .base import Connector, Hit, clip, html_text, TREND_OPTIONS, period_ranges, trend_hit

API = 'https://api.stackexchange.com/2.3/search/advanced'
QUESTIONS = 'https://api.stackexchange.com/2.3/questions'
PAGESIZE = 30
KEYLESS_DAILY_QUOTA = 250   # below the documented 300/day per IP
KEYED_DAILY_QUOTA = 9000    # below the documented 10,000/day

def _iso(epoch):
    return datetime.fromtimestamp(epoch,timezone.utc).isoformat() if isinstance(epoch,int) and epoch>0 else 'unknown'

class StackExchange(Connector):
    evidence_kind = 'opinion'
    ENGLISH_FIRST = True
    OPTIONS = {'site':r'[a-z][a-z0-9.\-]{1,40}','tagged':r'[a-z0-9.#+\-]{1,35}(;[a-z0-9.#+\-]{1,35}){0,4}',**TREND_OPTIONS}

    def __init__(self):
        self.key=os.environ.get('MARKETSCOPE_STACKEXCHANGE_KEY','').strip()
        self.policy=Policy('stackexchange',frozenset({'api.stackexchange.com'}),min_interval=0.5,
                           daily_quota=KEYED_DAILY_QUOTA if self.key else KEYLESS_DAILY_QUOTA,
                           redact_params=frozenset({'key'}))

    def _total(self, fetch, url, params):
        if self.key: params={**params,'key':self.key}
        data,_,_=fetch(url,params)
        if isinstance(data,dict) and isinstance(data.get('backoff'),int) and data['backoff']>0: fetch.defer(data['backoff'])
        if not isinstance(data,dict) or not isinstance(data.get('total'),int):
            raise ConnectorFailure('Respuesta de Stack Exchange sin total')
        return data['total']

    def trend(self, fetch, query, options):
        """Questions matching the query per complete period (filter=total), normalized by all questions of the
        site in the period (/questions). Cost: 1 request per period, 2 when normalized (daily quota applies)."""
        site=options.get('site','stackoverflow'); granularity=options.get('granularity','year')
        normalize=options.get('normalize','yes')=='yes'
        ranges=period_ranges(granularity,int(options.get('periods','5')))
        base={'site':site,'filter':'total'}
        if 'tagged' in options: base['tagged']=options['tagged']
        series=[]
        for label,s,e in ranges:
            window={'fromdate':int(s.timestamp()),'todate':int(e.timestamp())-1}
            count=self._total(fetch,API,{**base,'q':query,**window})
            total=self._total(fetch,QUESTIONS,{'site':site,'filter':'total',**window}) if normalize else None
            series.append((label,count,total))
        start,end=ranges[0][1],ranges[-1][2]
        url=API+'?'+urlencode({**base,'q':query,'fromdate':int(start.timestamp()),'todate':int(end.timestamp())-1})
        return [trend_hit('stackexchange',query,'Preguntas',granularity,series,url,normalize,end.isoformat(),
                          {'site':site,'caveat':'Población técnica; el volumen total del sitio cambia con el tiempo'})]

    def search(self, fetch, query, options):
        if options.get('mode')=='trend': return self.trend(fetch,query,options)
        site=options.get('site','stackoverflow')
        params={'q':query,'site':site,'pagesize':PAGESIZE,'order':'desc','sort':'relevance','filter':'withbody'}
        if 'tagged' in options: params['tagged']=options['tagged']
        if self.key: params['key']=self.key
        data,_,_=fetch(API,params)
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):
            raise ConnectorFailure('Respuesta de Stack Exchange sin lista items')
        if isinstance(data.get('backoff'),int) and data['backoff']>0: fetch.defer(data['backoff'])
        if data.get('quota_remaining')==0 and not data['items']:
            raise StepBlocked('Cuota diaria de Stack Exchange agotada (quota_remaining=0)')
        context={'quota_remaining':data.get('quota_remaining'),'has_more':data.get('has_more')}
        return [h for h in (self._hit(i,site,context) for i in data['items']) if h]

    def _hit(self, item, site, context):
        link=item.get('link'); title=html_text(item.get('title'))
        if not isinstance(link,str) or not link.startswith('https://') or not title: return None
        user=(item.get('owner') or {}).get('user_id')
        group=f'stackexchange:{site}:user:{user}' if isinstance(user,int) else f"stackexchange:{site}:question:{item.get('question_id')}"
        body=html_text(item.get('body'))
        return Hit(url=link,title=title,published_at=_iso(item.get('creation_date')),
                   snippet=clip(f'{title}. {body}' if body else title),independence_group=group,
                   metadata={'site':site,'question_id':item.get('question_id'),'score':item.get('score'),
                             'answer_count':item.get('answer_count'),'is_answered':item.get('is_answered'),
                             'view_count':item.get('view_count'),'tags':item.get('tags'),**context})
