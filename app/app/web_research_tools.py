"""Search adapters and bounded public-page reader, shared by the MCP tools."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

VERSION = 'web-research-v1'
MAX_BYTES = 750_000
SECRET_MARKERS = re.compile(
    r'(?i)(?:-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+|'
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)|'
    r'(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|'
    r'private[_-]?key|authorization|cookie|credential)\s*["\']?\s*[:=]\s*\S+)')
SECRET_ENV = re.compile(r'(?i)(?:^|_)(?:password|passwd|secret|api_key|access_token|refresh_token|token|cookie|credential)(?:$|_)')


class ResearchToolError(ValueError):
    """Only a fixed, non-secret error code may cross the tool boundary."""


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, maximum=16000):
    if not isinstance(value, str) or '\x00' in value:
        raise ResearchToolError('INVALID_TEXT')
    if SECRET_MARKERS.search(value):
        raise ResearchToolError('SENSITIVE_CONTENT_BLOCKED')
    # Local authentication material must never enter a model/tool response.
    for key, secret in os.environ.items():
        if SECRET_ENV.search(key):
            if secret and secret in value:
                raise ResearchToolError('SENSITIVE_CONTENT_BLOCKED')
    return value[:maximum]


def validate_query(query):
    query = safe_text(query, 400).strip()
    if not query or len(query) > 240 or len(query.split()) > 40 or '\n' in query or '\r' in query:
        raise ResearchToolError('QUERY_MUST_BE_SHORT')
    return query


def public_target(url, *, resolve=True):
    try:
        value = urlsplit(safe_text(url, 4096))
        host = value.hostname
        if (value.scheme not in {'http', 'https'} or not host or value.username is not None
                or value.password is not None or value.port not in {None, 80, 443}
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError
        host = host.encode('idna').decode('ascii').lower().rstrip('.')
        auth_parameters = {'api_key', 'apikey', 'access_token', 'refresh_token', 'token', 'auth',
                           'authorization', 'password', 'secret', 'signature', 'sig', 'x-amz-signature', 'x-amz-credential'}
        if any(key.casefold() in auth_parameters for key,_ in parse_qsl(value.query)):
            raise ValueError
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        port = value.port or (443 if value.scheme == 'https' else 80)
        addresses = []
        if resolve:
            addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise ValueError
        return value, host, port, addresses
    except ResearchToolError:
        raise
    except Exception:
        raise ResearchToolError('PUBLIC_HTTP_URL_REQUIRED') from None


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self):
        super().connect()
        try:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.close()
            raise


def public_get(url, *, timeout=15, max_bytes=MAX_BYTES):
    """Pin validated DNS addresses; revalidate every redirect, preserving TLS SNI."""
    original = url
    for _ in range(5):
        target, host, port, addresses = public_target(url)
        connection = (_PinnedHTTPS if target.scheme == 'https' else _PinnedHTTP)(host, port, addresses[0], timeout)
        try:
            path = urlunsplit(('', '', target.path or '/', target.query, ''))
            connection.request('GET', path, headers={
                'Host': host if port in {80, 443} else f'{host}:{port}',
                'User-Agent': 'LitKG-Research/1.0', 'Accept-Encoding': 'identity',
                'Accept': 'text/html,text/plain,application/json;q=0.9',
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ResearchToolError('REDIRECT_WITHOUT_LOCATION')
                redirected = urljoin(url, location)
                if target.scheme == 'https' and urlsplit(redirected).scheme != 'https':
                    raise ResearchToolError('HTTPS_DOWNGRADE_BLOCKED')
                url = redirected
                continue
            if response.status != 200:
                raise ResearchToolError(f'HTTP_{response.status}')
            media = (response.getheader('Content-Type') or '').split(';', 1)[0].lower()
            if media not in {'text/html', 'application/xhtml+xml', 'text/plain', 'application/json'}:
                raise ResearchToolError('UNSUPPORTED_MEDIA_TYPE')
            if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
                raise ResearchToolError('COMPRESSED_RESPONSE_BLOCKED')
            started = time.monotonic()
            raw = bytearray()
            while len(raw) <= max_bytes:
                if time.monotonic() - started > 20:
                    raise ResearchToolError('PAGE_READ_DEADLINE')
                part = response.read1(min(16384, max_bytes + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            if len(raw) > max_bytes:
                raise ResearchToolError('PAGE_TOO_LARGE')
            charset = response.headers.get_content_charset() or 'utf-8'
            text = bytes(raw).decode(charset, errors='replace')
            return {'requested_url': original, 'url': url, 'content_type': media, 'text': text}
        except ResearchToolError:
            raise
        except Exception:
            raise ResearchToolError('PAGE_FETCH_FAILED') from None
        finally:
            connection.close()
    raise ResearchToolError('TOO_MANY_REDIRECTS')


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignore = []
        self.parts = []
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'nav', 'footer', 'header', 'form', 'svg'}:
            self.ignore.append(tag)
        if tag == 'title':
            self.in_title = True
        if tag in {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'article', 'section'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.ignore and tag == self.ignore[-1]:
            self.ignore.pop()
        if tag == 'title':
            self.in_title = False
        if tag in {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'article', 'section'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.ignore:
            self.parts.append(data)


def read_web_page(url, max_chars=12000):
    if type(max_chars) is not int or not 1000 <= max_chars <= 20000:
        raise ResearchToolError('INVALID_PAGE_LIMIT')
    page = public_get(url)
    if page['content_type'] in {'text/html', 'application/xhtml+xml'}:
        parser = PageText()
        parser.feed(page['text'])
        title = ' '.join(parser.title)
        text = '\n'.join(filter(None, (re.sub(r'\s+', ' ', line).strip() for line in ''.join(parser.parts).splitlines())))
    else:
        title, text = '', page['text']
    safe_text(text, MAX_BYTES)  # Scan the entire fetched body before truncation.
    return {'kind': 'external_page', 'url': safe_text(page['url'], 4096),
            'requested_url': safe_text(page['requested_url'], 4096),
            'title': safe_text(title, 500), 'retrieved_at': now(),
            'text': text[:max_chars], 'truncated': len(text) > max_chars,
            'scope': 'background_only_not_pdf_evidence'}


def search_web(query, max_results=5):
    query = validate_query(query)
    if type(max_results) is not int or not 1 <= max_results <= 8:
        raise ResearchToolError('INVALID_RESULT_LIMIT')
    endpoint = os.environ.get('SEARXNG_URL', '').rstrip('/')
    if not endpoint:
        raise ResearchToolError('SEARCH_BACKEND_NOT_CONFIGURED')
    # This explicitly configured internal backend is distinct from the public page reader.
    import requests
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(endpoint + '/search', params={'q': query, 'format': 'json'},
                               timeout=(5, 20), allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ResearchToolError('SEARCH_BACKEND_HTTP_ERROR')
        raw = bytearray()
        for part in response.iter_content(16384):
            raw.extend(part)
            if len(raw) > MAX_BYTES:
                raise ResearchToolError('SEARCH_RESPONSE_TOO_LARGE')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ResearchToolError('INVALID_SEARCH_RESPONSE')
        results = []
        for item in data['results']:
            if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                continue
            try:
                public_target(item['url'])
                results.append({'url': safe_text(item['url'], 4096),
                                'title': safe_text(item.get('title', ''), 400),
                                'snippet': safe_text(item.get('content', ''), 1500)})
            except ResearchToolError:
                continue
            if len(results) >= max_results:
                break
        return {'kind': 'search_candidates', 'query': query, 'retrieved_at': now(), 'results': results,
                'instruction': 'Discovery only. Read the original page before using a claim. Not PDF evidence.'}
    except ResearchToolError:
        raise
    except Exception:
        raise ResearchToolError('SEARCH_BACKEND_FAILED') from None
    finally:
        session.close()


def propose_entity_link(mention, identifier, source_url, source_quote):
    """Return a proposal, never modify the graph or assert an identity match."""
    mention, identifier = safe_text(mention, 250), safe_text(identifier, 100)
    if not mention or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,30}:[A-Za-z0-9_.-]{1,70}', identifier):
        raise ResearchToolError('INVALID_IDENTIFIER_PROPOSAL')
    public_target(source_url)
    quote = safe_text(source_quote, 1500)
    if not quote.strip():
        raise ResearchToolError('SOURCE_QUOTE_REQUIRED')
    return {'kind': 'external_identifier_candidate', 'mention': mention, 'identifier': identifier,
            'source_url': source_url, 'source_quote': quote, 'retrieved_at': now(),
            'identity_status': 'unreviewed', 'applied_to_graph': False}
