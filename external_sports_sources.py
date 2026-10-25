"""Explicit alternate MLB channels, never automatic game matching or recovery."""

ROXIE_HOST = 'roxiestreams.su'
ROXIE_CAPABILITY = 'external-baseball-v1'
ROXIE_URLS = tuple(f'https://{ROXIE_HOST}/mlb-streams-1?sk-source={n}' for n in (1, 2))


def baseball_choices(category, current_url, capabilities):
    if category != 'baseball':
        return []
    available = ROXIE_CAPABILITY in capabilities
    return [dict(url=url, source='RoxieStreams', number=n,
                 language='MLB channel — confirm matchup', hd=False, viewers=None,
                 current=url == current_url, available=available,
                 requires_companion=None if available else '0.2.8')
            for n, url in enumerate(ROXIE_URLS, 1)]
