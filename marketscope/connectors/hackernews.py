"""Hacker News search through the Algolia HN Search API. Docs: https://hn.algolia.com/api
(the official Firebase API has no search). No key. Options: tags (story, comment, show_hn, ask_hn),
since (YYYY-MM-DD), sort (relevance | date). Author names are not stored: they are hashed into the
independence group so several comments by one person count as one testimony."""
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode
from ..http_client import Policy, ConnectorFailure
from .base import Connector, Hit, clip, html_text, iso_datetime, TREND_OPTIONS, period_ranges, trend_hit

SEARCH = 'https://hn.algolia.com/api/v1/search'
BY_DATE = 'https://hn.algolia.com/api/v1/search_by_date'
PER_PAGE = 30

class HackerNews(Connector):
    evidence_kind = 'opinion'
    ENGLISH_FIRST = True
    OPTIONS = {'tags':r'story|comment|show_hn|ask_hn','since':r'\d{4}-\d{2}-\d{2}','sort':r'relevance|date',**TREND_OPTIONS}

    def __init__(self):
        self.policy=Policy('hackernews',frozenset({'hn.algolia.com'}),min_interval=0.5)

    def trend(self, fetch, query, options):
        """Items per complete period (nbHits with hitsPerPage=0); normalized by all items of the period.
        Cost: 1 request per period, 2 when normalized. Algolia matching is typo-tolerant: counts are approximate."""
        granularity=options.get('granularity','year'); normalize=options.get('normalize','yes')=='yes'
        ranges=period_ranges(granularity,int(options.get('periods','5')))
        tags=options.get('tags','(story,comment)')
        def count(q,start,end):
            flt=f'created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}'
            data,_,_=fetch(SEARCH,{'query':q,'tags':tags,'hitsPerPage':0,'numericFilters':flt})
            if not isinstance(data,dict) or not isinstance(data.get('nbHits'),int):
                raise ConnectorFailure('Respuesta de HN Algolia sin nbHits')
            return data['nbHits']
        series=[(label,count(query,s,e),count('',s,e) if normalize else None) for label,s,e in ranges]
        start,end=ranges[0][1],ranges[-1][2]
        url=SEARCH+'?'+urlencode({'query':query,'tags':tags,'hitsPerPage':0,
                                  'numericFilters':f'created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}'})
        return [trend_hit('hackernews',query,'Publicaciones y comentarios',granularity,series,url,normalize,end.isoformat(),
                          {'tags':tags,'caveat':'Búsqueda tolerante a erratas: recuentos aproximados; población técnica'})]

    def search(self, fetch, query, options):
        if options.get('mode')=='trend': return self.trend(fetch,query,options)
        params={'query':query,'hitsPerPage':PER_PAGE,'tags':options.get('tags','(story,comment)')}
        if 'since' in options:
            ts=int(datetime.strptime(options['since'],'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
            params['numericFilters']=f'created_at_i>{ts}'
        data,_,_=fetch(BY_DATE if options.get('sort')=='date' else SEARCH,params)
        if not isinstance(data,dict) or not isinstance(data.get('hits'),list):
            raise ConnectorFailure('Respuesta de HN Algolia sin lista hits')
        context={'total_count':data.get('nbHits')}
        return [h for h in (self._hit(x,context) for x in data['hits']) if h]

    def _hit(self, x, context):
        oid=str(x.get('objectID') or '')
        if not oid.isdigit(): return None
        is_comment='comment' in (x.get('_tags') or []) or x.get('comment_text')
        body=html_text(x.get('comment_text') if is_comment else x.get('story_text'))
        title=' '.join(((x.get('story_title') if is_comment else x.get('title')) or '').split())
        if not title and not body: return None
        author=x.get('author') or ''
        group=('hackernews:author:'+hashlib.sha256(author.encode()).hexdigest()[:12]) if author else f'hackernews:item:{oid}'
        head=f'Comentario en «{title}»' if is_comment else title
        return Hit(url=f'https://news.ycombinator.com/item?id={oid}',title=(head or f'HN {oid}')[:300],
                   published_at=iso_datetime(x.get('created_at')),snippet=clip(f'{head}. {body}' if body else head),
                   independence_group=group,
                   metadata={'type':'comment' if is_comment else 'story','points':x.get('points'),
                             'num_comments':x.get('num_comments'),'story_id':x.get('story_id'),
                             'external_url':x.get('url') or x.get('story_url'),**context})
