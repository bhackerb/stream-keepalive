"""Explicit user-supplied public HTTPS page URLs; no server-side URL fetching."""
import re
from urllib.parse import urlparse

USER_SOURCE_CAPABILITY = 'user-source-v1'
USER_SOURCE_VERSION = '0.2.9'


def validate_user_source_url(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
            raise ValueError()
        u = urlparse(value)
        host = u.hostname or ''
        labels = host.split('.')
        if (u.scheme != 'https' or u.username is not None or u.password is not None or
            u.port not in (None, 443) or len(labels) < 2 or len(host) > 253 or
            not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels) or
            not re.fullmatch(r'[a-z][a-z0-9-]*', labels[-1]) or
            labels[-1] in ('local', 'localhost', 'internal', 'lan') or host.endswith('.home.arpa')):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use a public HTTPS stream page URL without credentials, local addresses or custom ports') from None
    return value
