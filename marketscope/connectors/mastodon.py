"""Mastodon public hashtag timelines. Docs: https://docs.joinmastodon.org/methods/timelines/#tag

Full-text search needs a logged-in account (checked live: anonymous /api/v2/search returns nothing), but
hashtag timelines of public posts are open. The query is 1-3 hashtags without '#' separated by '|'
(one request each). Option `instance` selects one of a fixed list of large instances (default mastodon.social).
Only public, original posts (no boosts). No account names or handles are stored apart from the post URL
itself (needed for citation); authors are grouped by the instance-local numeric account id."""
import re
from ..http_client import Policy, ConnectorFailure, NotFound
from .base import Connector, Hit, clip, html_text, iso_datetime

INSTANCES = frozenset({'mastodon.social','mastodon.online','mstdn.social','fosstodon.org','hachyderm.io'})
TIMELINE = 'https://{instance}/api/v1/timelines/tag/{tag}'
LIMIT = 40
MAX_TAGS = 3

class Mastodon(Connector):
    evidence_kind = 'opinion'
    OPTIONS = {'instance':'|'.join(re.escape(i) for i in sorted(INSTANCES))}
    QUERY_HELP = 'Mastodon necesita query con 1-3 hashtags sin # separados por | (p. ej. "smallbusiness|freelance")'

    def __init__(self):
        self.policy=Policy('mastodon',INSTANCES,min_interval=1.0)

    def build_query(self, terms):
        return ''  # hashtags must be chosen, not derived from a market description

    def validate_query(self, query):
        tags=query.split('|')
        if not 1<=len(tags)<=MAX_TAGS or not all(re.fullmatch(r'[^\W_][\w]{0,49}',t,re.UNICODE) for t in tags):
            raise ValueError(f'Usa de 1 a {MAX_TAGS} hashtags (letras, dígitos o _) sin #, separados por |')

    def search(self, fetch, query, options):
        instance=options.get('instance','mastodon.social')
        hits,missing,seen=[],[],set()
        for tag in query.split('|'):
            try:
                data,_,_=fetch(TIMELINE.format(instance=instance,tag=tag.lower()),{'limit':LIMIT})
            except NotFound:
                missing.append(tag); continue
            if not isinstance(data,list): raise ConnectorFailure('Respuesta de Mastodon con formato inesperado')
            for s in data:
                h=self._hit(s,instance,tag)
                if h and h.url not in seen: seen.add(h.url); hits.append(h)
        for h in hits: h.metadata['missing_tags']=missing
        return hits

    def _hit(self, s, instance, tag):
        if not isinstance(s,dict) or s.get('reblog') or s.get('visibility')!='public': return None
        url=s.get('url') or ''
        text=html_text(s.get('content'))
        if not url.startswith('https://') or not text: return None
        account=(s.get('account') or {}).get('id')
        group=f'mastodon:{instance}:account:{account}' if account else f"mastodon:{instance}:status:{s.get('id')}"
        return Hit(url=url,title=text[:120],published_at=iso_datetime(s.get('created_at')),snippet=clip(text),
                   independence_group=group,
                   metadata={'instance':instance,'hashtag':tag,'language':s.get('language'),
                             'replies':s.get('replies_count'),'boosts':s.get('reblogs_count'),
                             'favourites':s.get('favourites_count'),'tags':[t.get('name') for t in s.get('tags') or []]})
