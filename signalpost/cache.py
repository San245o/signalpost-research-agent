"""Optional immutable response cache. Failures are never persisted as absences."""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


def write_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_bytes(body)
    temp.replace(path)


class ResponseCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key, max_age):
        if max_age <= 0:
            return None
        try:
            meta = json.loads(self._path(key).read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(meta['retrieved_at'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if not 0 <= age <= max_age:
                return None
            body = (self.directory / (meta['content_sha256'] + '.bin')).read_bytes()
            if hashlib.sha256(body).hexdigest() != meta['content_sha256']:
                return None
            from .net import Response
            return Response(meta['url'], meta['status'], body, meta['headers'],
                            meta['retrieved_at'], state="available",
                            requested_url=meta.get('requested_url', meta['url']),
                            redirect_chain=meta.get('redirect_chain', []), cache_hit=True)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key, response):
        if response.status != 200:
            return
        write_bytes(self.directory / (response.digest + '.bin'), response.body)
        meta = {k: getattr(response, k) for k in ('url', 'status', 'headers', 'retrieved_at', 'requested_url', 'redirect_chain')}
        meta['content_sha256'] = response.digest
        write_bytes(self._path(key), json.dumps(meta, ensure_ascii=False).encode())
