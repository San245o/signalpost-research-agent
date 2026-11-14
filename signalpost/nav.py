"""Shared public NAV feed leads; publish only a current exact-employer detail record."""
from __future__ import annotations
import re
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlsplit
from .net import Client, host_of
from .registry import json_document
from .web import company_words, parse_date

ORIGIN = "https://pam-stilling-feed.nav.no"


def nav_url(value):
    url = urljoin(ORIGIN, str(value))
    p = urlsplit(url)
    return url if p.scheme == "https" and p.hostname == "pam-stilling-feed.nav.no" and p.port in (None, 443) and not p.username and not p.password else None


class NavIndex:
    def __init__(self,state_path=None):
        self.token = None
        self.active = {}
        self.lock = threading.Lock()
        self.pages = 0
        self.complete = False
        self.error = None
        self.state_path=Path(state_path) if state_path else None
        self.previous_scan=None
        self.retained_complete_index=False
        self.finished=threading.Event()
        if self.state_path:
            try:
                state=json.loads(self.state_path.read_text(encoding='utf-8'))
                if state.get('complete'):
                    self.previous_scan=datetime.fromisoformat(state['scan_started_at'].replace('Z','+00:00'))
                    self.active={k:v for k,v in state['active'].items() if isinstance(v,dict) and nav_url(v.get('url')) and isinstance(v.get('name'),str)}
                    self.retained_complete_index=True
            except (OSError,ValueError,TypeError,KeyError):pass

    def build(self, client, max_pages=80):
        if not max_pages:
            self.error = "Disabled by run configuration"
            self.finished.set()
            return
        started=datetime.now(timezone.utc)
        working=dict(self.active)
        try:
            response = client.get(ORIGIN + "/api/publicToken", robots=False, retries=0)
            match = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", response.text) if response.status == 200 else None
            if not match:
                self.error = "Public NAV token unavailable"
                return
            self.token = match.group()
            since = format_datetime(min(started,self.previous_scan)-timedelta(minutes=5) if self.previous_scan else started-timedelta(days=30), usegmt=True)
            url = ORIGIN + "/api/v1/feed"
            headers = {"Authorization": "Bearer " + self.token, "If-Modified-Since": since}
            visited = set()
            for _ in range(max_pages):
                if url in visited:
                    self.error = "NAV pagination cycle refused"
                    return
                visited.add(url)
                response = client.get(url, robots=False, retries=0, headers=headers)
                if response.status != 200 or host_of(response.url) != "pam-stilling-feed.nav.no":
                    self.error = "NAV feed request failed"
                    return
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                    raise ValueError("Invalid NAV feed response")
                with self.lock:
                    self.pages += 1
                    for item in body["items"]:
                        entry = item.get("_feed_entry") or {}
                        ident = str(item.get("id") or "")
                        detail_url = nav_url(item.get("url")) if item.get("url") else None
                        if not ident:
                            continue
                        if entry.get("status") == "ACTIVE" and entry.get("businessName") and detail_url:
                            working[ident] = {"id": ident, "url": detail_url, "name": " ".join(company_words(entry["businessName"]))}
                        else:
                            working.pop(ident, None)
                    # Publish partial leads only on first bootstrap. An old complete
                    # index stays intact until this whole incremental scan succeeds.
                    if not self.retained_complete_index:self.active=dict(working)
                next_url = body.get("next_url")
                if not next_url:
                    with self.lock:self.active=working;self.complete=True
                    if self.state_path:
                        from .records import atomic_write,canonical
                        atomic_write(self.state_path,canonical({'complete':True,'scan_started_at':started.isoformat(),'active':working}))
                    return
                url = nav_url(next_url)
                if not url:
                    self.error = "NAV pagination left the official feed"
                    return
                headers = {"Authorization": "Bearer " + self.token}
            self.error = "Page cap reached; this is a partial recent-change index"
        except (ValueError, OSError, TypeError, AttributeError):
            self.error = "NAV feed unavailable or invalid"
        finally:
            self.finished.set()

    def add_jobs(self, profile, record, client, max_details=3):
        homepages = []
        if not self.token:
            return homepages
        wanted = " ".join(company_words(record.get("navn")))
        wanted_names={wanted}|{' '.join(company_words(name)) for name in record.get('_operating_names',[])}
        hints=record.get('_public_hints') or {}
        wanted_names|={' '.join(company_words(name)) for name in hints.get('nav_names',[]) if isinstance(name,str)}
        workplaces=[c['value'] for c in profile.data['claims'] if c['field']=='workplace' and c['availability']=='available']
        wanted_names|={' '.join(company_words(w.get('name'))) for w in workplaces}
        wanted_names.discard('')
        with self.lock:
            # Feed employer labels may add a branch or "barnehage" suffix.
            # This broadens leads only: the fetched ad must still carry an exact
            # registry-confirmed parent/subunit number below.
            candidates = [dict(row) for row in self.active.values() if any(
                row['name']==name or len(name.split())>=2 and row['name'].startswith(name+' ')
                for name in wanted_names)][:max_details]
        employers = {profile.org}
        for claim in profile.data["claims"]:
            if claim["field"] == "workplace" and claim["availability"] == "available":
                employers.add(str(claim["value"].get("organisation_number")))
        today = datetime.now(timezone.utc).date()
        # Cached UUIDs recover ads absent from the bounded recent-change scan.
        # Dates/status/parentage below always come from the fresh detail record.
        seeded=[]
        for row in hints.get('nav_ads',[]):
            ident=row.get('uuid','');expiry=parse_date(row.get('expires'))
            if not re.fullmatch(r'[a-fA-F0-9-]{36}',ident) or expiry and expiry<today:continue
            if row.get('employer_orgnr') and str(row['employer_orgnr']) not in employers:continue
            seeded.append({'id':ident,'url':ORIGIN+'/api/v1/feedentry/'+ident,'name':''})
        candidates=list({row['url']:row for row in candidates+seeded}.values())[:max_details]
        already_cited={e['source_url'] for e in profile.data['evidence'] if e.get('source_class')=='official_nav_job_feed'}
        for candidate in candidates:
            if candidate['url'] in already_cited:continue
            response = client.get(candidate["url"], robots=False, retries=0, headers={"Authorization": "Bearer " + self.token})
            if response.status != 200 or host_of(response.url) != "pam-stilling-feed.nav.no":
                continue
            try:
                data, spans = json_document(response)
                content = data.get("ad_content") or {}
                employer = content.get("employer") or {}
                employer_org = str(employer.get("orgnr") or employer.get("organisasjonsnummer") or "")
                status=data.get('status',content.get('status'))
                if employer_org not in employers or status != "ACTIVE":
                    continue
                if employer_org == profile.org and isinstance(employer.get('homepage'),str):
                    from .net import clean_url
                    homepage = clean_url(employer['homepage'])
                    if homepage and homepage not in homepages:
                        homepages.append(homepage)
                published = parse_date(content.get("published") or content.get("publishedAt"))
                expires = parse_date(content.get("expires") or content.get("applicationDue"))
                title = content.get("title")
                if not title or not published or not expires or not published <= today <= expires:
                    continue
                uuid = str(data.get('uuid') or content.get("uuid") or candidate["id"])
                if not re.fullmatch(r"[A-Za-z0-9-]+", uuid):
                    continue
                from .careers import is_recruiter,advertised_role
                if is_recruiter(record):
                    advertised_role(profile,record,title,'https://arbeidsplassen.nav.no/stillinger/stilling/'+uuid,
                                    response,spans[()],posted=published,expires=expires,
                                    source_class='official_nav_job_feed',method='nav_recruiter_advertisement')
                    continue
                profile.add("hiring_signal", {"title": title, "date": published.isoformat(), "valid_through": expires.isoformat(),
                            "url": "https://arbeidsplassen.nav.no/stillinger/stilling/" + uuid, "employer_organisation_number": employer_org},
                            response, spans[()], "official_nav_job_feed", period=published.isoformat())
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return homepages

    def report(self):
        with self.lock:
            return {"pages": self.pages, "active_leads": len(self.active), "complete_recent_window": self.complete,
                    'retained_complete_index':self.retained_complete_index,'persistent':bool(self.state_path),
                    "error": self.error, "scope": "Recent changes only; this is not a full historical vacancy inventory"}
