"""Read top-level Yahoo search candidates without interpreting post bodies."""
from html.parser import HTMLParser
import json


class NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.capture = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def search_page(document):
    if not isinstance(document, str):
        raise ValueError('invalid_search_response')
    parser = NextData()
    parser.feed(document)
    try:
        value = json.loads(''.join(parser.parts))
    except (ValueError, RecursionError):
        raise ValueError('invalid_search_response') from None
    for key in ('props', 'pageProps', 'pageData'):
        if not isinstance(value, dict):
            raise ValueError('invalid_search_response')
        value = value.get(key)
    if not isinstance(value, dict):
        raise ValueError('invalid_search_response')
    return value


def candidate_entries(page):
    best, timeline = page.get('bestTweet'), page.get('timeline')
    if best is not None and not isinstance(best, dict):
        raise ValueError('invalid_search_response')
    if timeline is None:
        if not best:
            raise ValueError('invalid_search_response')
        entries = []
    elif not isinstance(timeline, dict) or not isinstance(timeline.get('entry'), list):
        raise ValueError('invalid_search_response')
    else:
        entries = timeline['entry']
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError('invalid_search_response')
    # bestTweet is a separate candidate, not a guarantee of the newest post.
    return ([best] if best else []) + entries
