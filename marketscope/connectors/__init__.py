"""Implemented connectors by source_id (must match catalog/sources.csv)."""
from .bluesky import Bluesky
from .eurlex import EurLex
from .eurostat import Eurostat
from .federal_register import FederalRegister
from .gdelt import Gdelt
from .github import GitHubIssues
from .hackernews import HackerNews
from .internet_archive import InternetArchive
from .mastodon import Mastodon
from .oecd import Oecd
from .openalex import OpenAlex
from .sec_edgar import SecEdgar
from .stackexchange import StackExchange
from .ted import Ted
from .us_census import UsCensus
from .wikidata import Wikidata
from .wikimedia_pageviews import WikimediaPageviews
from .worldbank import WorldBank

REGISTRY = {'openalex': OpenAlex, 'stackexchange': StackExchange, 'github_issues': GitHubIssues,
            'eurostat': Eurostat, 'ted': Ted, 'sec_edgar': SecEdgar, 'worldbank': WorldBank,
            'eurlex': EurLex, 'federal_register': FederalRegister,
            'wikimedia_pageviews': WikimediaPageviews, 'wikidata': Wikidata,
            'internet_archive': InternetArchive, 'oecd': Oecd, 'us_census': UsCensus,
            'hackernews': HackerNews, 'mastodon': Mastodon, 'bluesky': Bluesky, 'gdelt': Gdelt}
