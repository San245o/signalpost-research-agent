"""Bounded HTTP with public-address pinning, redirects, robots, and wire accounting."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from functools import cached_property
from functools import lru_cache
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "SignalpostRetry/1.0"
OFFICIAL_HOSTS = {"data.brreg.no", "pam-stilling-feed.nav.no"}


@lru_cache(maxsize=1)
def tls_context():
    """System trust plus the shipped, hash-checked Mozilla CA bundle."""
    context=ssl.create_default_context()
    directory=Path(__file__).resolve().parents[1]/'data/trust'
    try:
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        raw=(directory/'cacert.pem').read_bytes()
        if hashlib.sha256(raw).hexdigest()!=manifest['sha256']:raise ValueError('CA bundle hash mismatch')
        context.load_verify_locations(cadata=raw.decode('ascii'))
    except FileNotFoundError:
        pass  # A missing optional bundle retains verified system TLS.
    return context


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Response:
    url: str
    status: int = 0
    body: bytes = b""
    headers: dict | None = None
    retrieved_at: str = ""
    error: str = ""
    state: str = "failed"
    requested_url: str = ""
    redirect_chain: list = field(default_factory=list)
    cache_hit: bool = False
    extraction_method: str = "http"
    derived_from: str = ""

    @cached_property
    def text(self):
        import re
        content_type = (self.headers or {}).get("content-type", "")
        match = re.search(r"charset=([\w-]+)", content_type, re.I)
        encoding = match.group(1) if match else "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    @cached_property
    def digest(self):
        return hashlib.sha256(self.body).hexdigest()

    def json(self):
        return json.loads(self.text)


class Budget:
    def __init__(self, requests=10000, seconds=900, clock=time.monotonic):
        self.limit = requests
        self.clock = clock
        self.deadline = clock() + seconds
        self.lock = threading.Lock()
        self.used = 0
        self.by_company = Counter()
        self.by_host = Counter()
        self.stopped = threading.Event()
        self.host_next = {}

    def pace(self, host, deadline):
        with self.lock:
            now = self.clock()
            slot = max(now, self.host_next.get(host, now))
            self.host_next[host] = slot + (.08 if host == 'data.brreg.no' else .12)
        wait = slot - now
        if slot >= min(self.deadline, deadline):
            raise TimeoutError('Host throttle would exceed deadline')
        if wait:
            self.stopped.wait(wait)

    def remaining(self):
        return max(0, self.deadline - self.clock())

    def reserve(self, company, host, company_deadline):
        with self.lock:
            if self.stopped.is_set() or self.clock() >= min(self.deadline, company_deadline):
                raise TimeoutError("Run or company deadline reached")
            if self.used >= self.limit:
                raise TimeoutError("Outbound request allowance exhausted")
            self.used += 1
            self.by_company[company] += 1
            self.by_host[host] += 1


def clean_url(url):
    value = str(url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return ""
        if p.port not in (None, 80, 443):
            return ""
        host = p.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        netloc = host + (":" + str(p.port) if p.port else "")
        return urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        return ""


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def public_addresses(host, port):
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".corp", ".home.arpa")):
        raise ValueError("Non-public host refused")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(item[4][0] for item in answers))
    if not ips:
        raise OSError("No address found")
    for value in ips:
        address = ipaddress.ip_address(value.split("%")[0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise ValueError("Non-public destination refused")
    return ips


class Client:
    def __init__(self, budget, company, *, timeout=5, company_seconds=60, transport=None,
                 captures=None, disk_cache=None, fresh=False, browser=None):
        self.budget, self.company, self.timeout = budget, company, timeout
        self.deadline = min(budget.deadline, budget.clock() + company_seconds)
        self.transport = transport
        self.cache = {}
        self.robots = {}
        self.robots_delay = {}
        self.robots_errors = {}
        self.last_request = {}
        self.attempts = []
        self.captures, self.disk_cache, self.fresh, self.browser = captures, disk_cache, fresh, browser

    def capture(self, response):
        if not self.captures or not response.body:
            return
        from pathlib import Path
        from .cache import write_bytes
        from .strategies import VERSION
        directory = Path(self.captures)
        path = directory / (response.digest + '.bin')
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != response.digest:
            raise ValueError('Existing source capture failed hash verification')
        if not path.exists():
            write_bytes(path, response.body)
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest()!=response.digest:
                    raise ValueError('New source capture failed hash verification')
            except (OSError,ValueError):
                # Failed writes/readbacks are not valid source captures. Preserve
                # the rejected file for diagnosis, without a successful receipt.
                path.replace(path.with_suffix('.rejected'))
                raise
        metadata = {'requested_url': response.requested_url or response.url, 'final_url': response.url,
                    'redirect_chain': response.redirect_chain, 'http_status': response.status,
                    'retrieved_at': response.retrieved_at, 'content_sha256': response.digest,
                    'parser_version': VERSION, 'extraction_method': response.extraction_method,
                    'response_headers':{k:v for k,v in (response.headers or {}).items() if k.lower() in {'content-type','location','retry-after'}},
                    'derived_from': response.derived_from, 'cache_hit': response.cache_hit,
                    'access_policy': 'public HTTP; robots enforced for company sources'}
        receipt = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()[:20]
        write_bytes(directory / (response.digest + '.' + receipt + '.json'), json.dumps(metadata).encode())

    def _wire(self, url, headers, method="GET", body=None):
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        # Tests inject a transport; real requests always validate AND pin DNS answers.
        addresses = public_addresses(host, port) if self.transport is None else []
        if self.transport is None:
            self.budget.pace(host, self.deadline)
        self.budget.reserve(self.company, host, self.deadline)
        if self.transport is not None:
            return self.transport(url, headers) if method == "GET" else self.transport(url, headers, method, body)
        timeout = min(self.timeout, max(.05, self.deadline - self.budget.clock()))
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port=port, timeout=timeout, **({'context':tls_context()} if parts.scheme=='https' else {}))
        # TLS still verifies the original hostname; the socket cannot resolve it again.
        def connect_pinned(address, socket_timeout, source_address=None):
            last=None
            for ip in sorted(addresses,key=lambda ip:':' in ip)[:3]:
                remaining=self.deadline-self.budget.clock()
                if remaining<=0:raise TimeoutError('Connection deadline reached')
                try:return socket.create_connection((ip,port),min(socket_timeout,remaining),source_address)
                except OSError as exc:last=exc
            raise last or OSError('No usable public address')
        conn._create_connection = connect_pinned
        try:
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            conn.request(method, path, body=body, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **headers})
            raw = conn.getresponse()
            response_headers = {k.lower(): v for k, v in raw.getheaders()}
            chunks, size = [], 0
            while True:
                if self.budget.clock() >= self.deadline:
                    raise TimeoutError("Response exceeded company deadline")
                chunk = raw.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 4_000_000:
                    raise ValueError("Response exceeds 4 MB limit")
            return Response(url, raw.status, b"".join(chunks), response_headers, utcnow())
        finally:
            conn.close()

    def post_json(self, url, payload, headers=None):
        # Credentialed requests have a fixed destination and NEVER follow redirects.
        if url not in {"https://google.serper.dev/search", "https://google.serper.dev/news", "https://google.serper.dev/places", "https://api.exa.ai/search", "https://api.parallel.ai/v1/search"}:
            return Response(url, error="Unapproved provider destination", state="blocked")
        try:
            response = self._wire(url, {"Content-Type": "application/json", **(headers or {})}, "POST", json.dumps(payload).encode())
            response.retrieved_at = response.retrieved_at or utcnow()
            response.state = "available" if response.status == 200 else "failed"
            response.requested_url = url
            self.capture(response)
            self.attempts.append({"url": url, "status": response.status, "state": response.state})
            return response
        except (ValueError, OSError, http.client.HTTPException) as exc:
            # Never log exception text from a provider call that carried a key.
            return Response(url, error="Provider request failed: " + type(exc).__name__)

    def _permitted(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            result = self.get(origin + "/robots.txt", robots=False, retries=1)
            if result.status == 404:
                self.robots[origin] = True
            elif result.status == 200:
                rp = RobotFileParser()
                rp.parse(result.text.splitlines())
                self.robots[origin] = rp
                self.robots_delay[origin] = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0
            else:
                self.robots[origin] = False
                self.robots_errors[origin]='Robots policy could not be retrieved: '+(result.error or str(result.status))
        policy = self.robots[origin]
        allowed = policy if isinstance(policy, bool) else policy.can_fetch(USER_AGENT, url)
        if allowed:
            pause = self.robots_delay.get(origin, 0)
            wait = max(0, self.last_request.get(origin, 0) + pause - self.budget.clock())
            if wait >= max(0, self.deadline - self.budget.clock()):
                return False
            if wait:
                self.budget.stopped.wait(wait)
            self.last_request[origin] = self.budget.clock()
        return allowed

    def get(self, url, *, robots=True, retries=1, headers=None, max_age=None):
        url = clean_url(url)
        if not url:
            return Response("", error="Unsafe or malformed URL", state="blocked")
        key = (url, robots)
        if key in self.cache:
            return self.cache[key]
        if max_age is None:
            max_age = 300 if any(s in url for s in ('stilling', '/jobs', '/careers', '/news', '/nyheter')) else 86400
            if '/regnskapsregisteret/' in url:
                max_age = 604800
        if self.disk_cache and not self.fresh and not headers:
            cached = self.disk_cache.get('GET:' + str(robots) + ':' + url, max_age)
            if cached:
                self.capture(cached)
                self.cache[key] = cached
                self.attempts.append({'url':url, 'final_url':cached.url, 'status':cached.status, 'cache_hit':True,
                                      'content_sha256':cached.digest, 'retrieved_at':cached.retrieved_at})
                return cached
        current = url
        original_host = host_of(url)
        response = Response(url, error="No response")
        chain = []
        for hop in range(5):
            if self.budget.stopped.is_set() or self.budget.clock() >= self.deadline:
                response = Response(current, error="Run or company deadline reached")
                break
            # Every redirect target goes through robots AND outbound validation.
            is_official = host_of(current) in OFFICIAL_HOSTS
            if robots and not is_official and not self._permitted(current):
                origin=f'{urlsplit(current).scheme}://{urlsplit(current).netloc}'
                response = Response(current, error=self.robots_errors.get(origin,"Robots policy disallows this path or crawl-delay exceeds deadline"), state="blocked")
                break
            for attempt in range(retries + 1):
                retryable=False
                try:
                    # Never forward a bearer token to another host.
                    sent_headers = headers or {} if host_of(current) == original_host else {}
                    response = self._wire(current, sent_headers)
                    response.retrieved_at = response.retrieved_at or utcnow()
                    response.headers = {k.lower(): v for k, v in (response.headers or {}).items()}
                    response.state = "available" if response.status == 200 else "not_available" if response.status in {404, 410} else "blocked" if response.status in {401, 403} else "failed"
                    response.requested_url = url
                    response.redirect_chain = list(chain)
                    self.capture(response)
                    retryable=response.status in {429,500,502,503,504}
                except (ValueError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
                    response = Response(current, error=f"{type(exc).__name__}: {str(exc)[:160]}", state="blocked" if isinstance(exc, ValueError) else "failed")
                    retryable=isinstance(exc,(TimeoutError,ConnectionError,http.client.HTTPException)) and not isinstance(exc,ssl.SSLError)
                if not retryable or attempt == retries:
                    break
                retry_after = (response.headers or {}).get("retry-after", "1")
                wait = min(5, float(retry_after)) if str(retry_after).isdigit() else 1
                if wait >= self.deadline - self.budget.clock():
                    break
                self.budget.stopped.wait(wait)
            self.attempts.append({"url": current, "status": response.status, "state": response.state, "reason": response.error,
                                  'requested_url':url, 'redirect_chain':list(chain), 'content_sha256':response.digest,
                                  'retrieved_at':response.retrieved_at, 'cache_hit':False})
            if response.status not in {301, 302, 303, 307, 308}:
                break
            target = clean_url(urljoin(current, (response.headers or {}).get("location", "")))
            if not target or target == current:
                response = Response(current, error="Invalid redirect", state="blocked")
                break
            if urlsplit(current).scheme == "https" and urlsplit(target).scheme == "http":
                response = Response(current, error="HTTPS downgrade refused", state="blocked")
                break
            current = target
            chain.append({'url':response.url, 'status':response.status, 'location':target})
        else:
            response = Response(current, error="Redirect limit reached")
        if response.status != 200 and not response.error:
            response.error = f"HTTP {response.status}"
        self.cache[key] = response
        if self.disk_cache and not headers:
            self.disk_cache.put('GET:' + str(robots) + ':' + url, response)
        return response
