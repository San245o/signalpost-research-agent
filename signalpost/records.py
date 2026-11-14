"""The evaluator contract is the internal format, with content-addressed evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
import os
import threading
import time
from pathlib import Path
from .net import utcnow

STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
FAMILIES = ("legal_name", "official_website", "annual_accounts_latest", "leader", "workplace", "company_profile", "hiring_signal", "dated_activity")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        # Windows sync/indexing readers can briefly deny atomic replacement.
        # Preserve the old output while retrying; permanent failures still raise.
        for attempt in range(8):
            try:
                temp.replace(path)
                return
            except PermissionError:
                if attempt==7:raise
                time.sleep(min(.05*2**attempt,.4))
    finally:
        try:temp.unlink(missing_ok=True)
        except OSError:pass


def public_envelope(envelope):
    """Candidate/rejection ledgers are audit artifacts, never published claims."""
    import copy
    result=copy.deepcopy(envelope)
    result.pop('website_discovery',None)
    result.pop('crawl_attempts',None)
    result.pop('external_discovery',None)
    def scrub(text):
        text=re.sub(r'https?://[^\s<>\"\']+','[source URL withheld]',str(text),flags=re.I)
        return re.sub(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?\b','[source host withheld]',text,flags=re.I)
    for c in result['claims']:
        if c['availability']!='available' and c.get('reason'):c['reason']=scrub(c['reason'])
    for error in result['errors']:
        if error.get('message'):error['message']=scrub(error['message'])
    return result


class Profile:
    def __init__(self, org, run_id, captures):
        self.org = org
        self.captures = Path(captures)
        self.data = {"organisation_number": org,
                     "run": {"run_id": run_id, "started_at": utcnow(), "terminal_status": "completed"},
                     "claims": [], "evidence": [], "changes": [], "errors": [],
                     "operations": {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0}}
        self._evidence = {}
        self._claims = set()
        self._verified_captures = set()

    @classmethod
    def resume(cls, envelope, captures):
        import copy
        profile = cls(envelope["organisation_number"], envelope["run"]["run_id"], captures)
        profile.data = copy.deepcopy(envelope)
        # Unavailable placeholders from the official phase are replaced by the
        # final discovery outcome. Accepted official facts stay untouched.
        profile.data["claims"] = [c for c in profile.data["claims"] if c["availability"] == "available" or c["field"] not in {"official_website", "company_profile", "hiring_signal", "dated_activity"}]
        profile._claims = {(c["field"], canonical(c["value"]), c["availability"], c.get("reporting_period")) for c in profile.data["claims"]}
        profile._evidence={(e['content_sha256'],e['source_url'],e['claim_span'],e.get('reporting_period'),e.get('source_pointer')):e['id'] for e in profile.data['evidence']}
        return profile

    def evidence(self, response, span, source_class, *, reporting_period=None, pointer=None, method=None):
        # Extraction must point at real bytes or real decoded source text, never a made-up quote.
        if not span or span not in response.text:
            raise ValueError("Evidence span does not occur in captured source")
        key = (response.digest, response.url, span, reporting_period, pointer)
        if key in self._evidence:
            return self._evidence[key]
        self.captures.mkdir(parents=True, exist_ok=True)
        path = self.captures / (response.digest + ".bin")
        # Bytes with the same digest are immutable, safe for concurrent writers.
        if not path.exists():
            temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temp.write_bytes(response.body)
            temp.replace(path)
        elif response.digest not in self._verified_captures and hashlib.sha256(path.read_bytes()).hexdigest() != response.digest:
            raise ValueError("Existing source capture failed hash verification")
        self._verified_captures.add(response.digest)
        eid = "ev-" + hashlib.sha256(canonical(key).encode()).hexdigest()[:20]
        row = {"id": eid, "source_url": response.url, "source_class": source_class,
               "retrieved_at": response.retrieved_at, "content_sha256": response.digest,
               "claim_span": span, "snapshot_path": "sources/" + path.name}
        from .strategies import VERSION
        row.update(requested_url=response.requested_url or response.url,final_url=response.url,
                   redirect_chain=response.redirect_chain,http_status=response.status,
                   parser_version=VERSION,extractor_version=VERSION,
                   extraction_method=method or (response.extraction_method if response.extraction_method!='http' else 'registry_json' if source_class.startswith('official_') else 'static_dom'),
                   content_type=(response.headers or {}).get('content-type',''),
                   access_policy='official_public_api' if source_class.startswith('official_') else 'public_knowledge_graph_cc0; robots_checked' if source_class=='public_knowledge_graph' else 'public_company_source; robots_checked',
                   span_start=response.text.index(span),span_end=response.text.index(span)+len(span),
                   cache_hit=response.cache_hit)
        if response.derived_from:
            row['derived_from_sha256']=response.derived_from
        if reporting_period:
            row["reporting_period"] = reporting_period
        if pointer:
            row["source_pointer"] = pointer
        self.data["evidence"].append(row)
        self._evidence[key] = eid
        return eid

    def add(self, field, value, response=None, span=None, source_class="official_registry", *, period=None, pointer=None, confidence=1.0, state="available", reason=None, method=None, linked_from=None):
        if state not in STATES:
            raise ValueError("Invalid availability state")
        if state == "available" and (value is None or response is None):
            raise ValueError("Available claim requires a value and captured source")
        key = (field, canonical(value), state, period)
        if key in self._claims:
            return
        if field in {'registered_workplace','active_job_count'}:
            raise ValueError('Use evaluator field families workplace / hiring_signal with individual evidence')
        if state=='available' and field in {'dated_activity','hiring_signal','careers_listing','company_profile'}:
            ck=claim_key({'field':field,'value':value,'reporting_period':period})
            if any(c['availability']=='available' and claim_key(c)==ck for c in self.data['claims']):return
        ids = [self.evidence(response, span, source_class, reporting_period=period, pointer=pointer,method=method)] if response is not None and span else []
        if state == "available" and not ids:
            raise ValueError("Available claim requires a verbatim evidence span")
        claim = {"field": field, "value": value if state == "available" else None,
                 "availability": state, "confidence": confidence if state == "available" else 0.0, "evidence_ids": ids}
        if period:
            claim["reporting_period"] = period
        if reason:
            claim["reason"] = reason
        claim['claim_key']=claim_key(claim)
        if state=='available':
            claim['first_observed_at']=claim['last_observed_at']=response.retrieved_at
        if linked_from:
            claim['linked_from']=linked_from
        self.data["claims"].append(claim)
        self._claims.add(key)

    def missing(self, field, state="not_available", reason="No supported fact found in the sources checked"):
        if not any(c["field"] == field for c in self.data["claims"]):
            self.add(field, None, state=state, reason=reason)

    def error(self, stage, message, state="failed"):
        self.data["errors"].append({"stage": stage, "state": state, "message": str(message)[:240]})

    def finish(self, *, requests=0, runtime_ms=0, status="completed"):
        for field in FAMILIES:
            self.missing(field, "failed" if status == "failed" else "not_available",
                         "Processing did not complete" if status == "failed" else "No supported fact found in the sources checked")
        self.data["run"].update(completed_at=utcnow(), terminal_status=status)
        self.data["operations"].update(requests=requests, runtime_ms=round(runtime_ms))
        self.data["summary"] = summarize(self.data)
        validate(self.data)
        return self.data


def validate(envelope, sources=None):
    if envelope["run"]["terminal_status"] not in {"completed", "failed"}:
        raise ValueError("Non-terminal output")
    evidence = {e["id"]: e for e in envelope["evidence"]}
    if len(evidence)!=len(envelope['evidence']):raise ValueError('Duplicate evidence identifiers')
    for row in evidence.values():
        if not row.get("source_url") or not row.get("retrieved_at") or not re.fullmatch(r"[0-9a-f]{64}", row.get("content_sha256", "")):
            raise ValueError("Incomplete evidence provenance")
        if sources is not None:
            body = (Path(sources) / (row["content_sha256"] + ".bin")).read_bytes()
            if hashlib.sha256(body).hexdigest() != row["content_sha256"]:
                raise ValueError("Source capture hash mismatch")
            from .net import Response
            decoded=Response(row['source_url'],body=body,headers={'content-type':row.get('content_type','')}).text
            if not row.get('claim_span') or row['claim_span'] not in decoded:
                raise ValueError('Evidence quote does not occur in captured source')
    for claim in envelope["claims"]:
        if claim["availability"] not in STATES or not set(claim["evidence_ids"]).issubset(evidence):
            raise ValueError("Invalid claim or dangling evidence reference")
        if claim["availability"] == "available" and (claim["value"] is None or not claim["evidence_ids"]):
            raise ValueError("Unsupported available claim")
        if claim["availability"] != "available" and claim["value"] is not None:
            raise ValueError("Unavailable value must be null")
        if claim['field'] in {'registered_workplace','active_job_count'}:
            raise ValueError('Legacy field name would hide evaluator field coverage')
    if not isinstance(envelope.get('summary'),dict) or not isinstance(envelope['summary'].get('sentences'),list):
        raise ValueError('Missing structured synthesis')
    for sentence in envelope['summary']['sentences']:
        if not sentence.get('evidence_ids') or not set(sentence['evidence_ids']).issubset(evidence):
            raise ValueError('Summary sentence lacks valid evidence references')
    for key in ('sections','metrics','signals'):
        for item in envelope['summary'].get(key,[]):
            if not set(item.get('evidence_ids',[])).issubset(evidence):
                raise ValueError('Summary detail has dangling evidence references')
            if (key!='sections' or item.get('availability') in {'available','stale'}) and not item.get('evidence_ids'):
                raise ValueError('Summary detail lacks supporting evidence')
    canonical(envelope)


def claim_key(claim):
    field=claim['field'];value=claim.get('value');period=claim.get('reporting_period','')
    if field in {'leader','workplace','company_profile','dated_activity','hiring_signal','careers_listing','former_name','site_location'}:
        if isinstance(value,dict):
            if field=='leader':ident=[value.get('name'),value.get('role_code') or value.get('role')]
            elif field in {'workplace','site_location'}:ident=value.get('organisation_number') or value.get('address') or value
            elif field=='dated_activity' and value.get('publisher_type')=='official_registry':ident=[value.get('url'),value.get('date'),value.get('event_type'),value.get('title')]
            else:ident=value.get('url') or value
        else:ident=value
    else:ident=period
    return field+':'+hashlib.sha256(canonical(ident).encode()).hexdigest()[:20]


def summarize(env):
    from .synthesis import summarize as build_summary
    return build_summary(env)


def semantic_claims(env):
    # Failed re-fetches are omissions, not proof that a prior fact disappeared.
    return {(c["field"], c.get("reporting_period", ""), canonical(fact_value(c))) for c in env["claims"] if c["availability"] == "available"}


def fact_value(claim):
    value=claim['value']
    if claim['field']=='careers_listing' and isinstance(value,dict):return {k:v for k,v in value.items() if k!='observed_at'}
    return value


def refresh(env, directory, captures=None):
    """Preserve last supported values and cite both sides of material changes."""
    import copy
    from .cache import write_bytes
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    key=re.sub(r'[^0-9A-Za-z_-]','_',env['organisation_number']) or 'invalid'
    history=directory/(key+'.jsonl');state=directory/(key+'.current.json')
    captures=Path(captures) if captures else directory.parent/'sources'
    previous=None;old_sources=directory.parent/'sources'
    if state.exists():
        stored=json.loads(state.read_text(encoding='utf-8'))
        previous=stored['envelope'];old_sources=Path(stored.get('source_directory') or old_sources)
    elif history.exists():
        with history.open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():previous=json.loads(line)
    old_claims={claim_key(c):c for c in (previous or {}).get('claims',[]) if c['availability']=='available'}
    current=[c for c in env['claims'] if c['availability']=='available']
    current_by_key={claim_key(c):c for c in current}
    evidence={e['id']:e for e in env['evidence']}
    old_evidence={e['id']:e for e in (previous or {}).get('evidence',[])}
    invalid_site_hosts=set()
    from .web import Page,holding_conflict
    from .net import Response,host_of
    owner=next((c['value'] for c in env['claims'] if c['field']=='legal_name' and c['availability']=='available'),None)
    if not owner:owner=next((c['value'] for c in old_claims.values() if c['field']=='legal_name'),None)
    if owner:
        for prior in old_claims.values():
            if prior['field']!='official_website':continue
            for eid in prior['evidence_ids']:
                e=old_evidence.get(eid,{})
                if e.get('source_class')!='company_owned':continue
                try:
                    raw=(old_sources/(e['content_sha256']+'.bin')).read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=e['content_sha256']:continue
                    page=Page(Response(e['source_url'],200,raw,{'content-type':e.get('content_type') or 'text/html'}))
                    if holding_conflict(page,{'navn':owner,'organisasjonsnummer':env['organisation_number']}):
                        invalid_site_hosts.add(host_of(prior['value']))
                except (OSError,ValueError,TypeError,KeyError):continue
    def import_evidence(ids):
        for eid in ids:
            if eid in evidence:continue
            row=old_evidence.get(eid)
            if not row:return False
            source=old_sources/(row['content_sha256']+'.bin')
            target=captures/(row['content_sha256']+'.bin')
            try:
                body=source.read_bytes() if not target.exists() else target.read_bytes()
                if hashlib.sha256(body).hexdigest()!=row['content_sha256']:return False
                if not target.exists():write_bytes(target,body)
            except OSError:return False
        for eid in ids:
            if eid not in evidence:
                row=copy.deepcopy(old_evidence[eid]);evidence[eid]=row;env['evidence'].append(row)
        return True
    changes=[]
    types={'leader':'new_role','workplace':'new_location','annual_accounts_latest':'new_filing',
           'business_description':'changed_description','site_description':'changed_description',
           'hiring_signal':'new_job','dated_activity':'new_activity','official_website':'changed_website'}
    for ck,claim in current_by_key.items():
        claim['claim_key']=ck
        prior=old_claims.get(ck)
        if not prior and claim['field']=='annual_accounts_latest':
            prior=next((c for c in old_claims.values() if c['field']=='annual_accounts_latest'),None)
        claim['first_observed_at']=(prior or {}).get('first_observed_at') or claim.get('first_observed_at') or env['run']['started_at']
        if prior and canonical(fact_value(prior))==canonical(fact_value(claim)):continue
        if previous:
            before_ids=(prior or {}).get('evidence_ids',[])
            imported=import_evidence(before_ids)
            changes.append({'field':claim['field'],'claim_key':ck,'reporting_period':claim.get('reporting_period'),
                            'type':types.get(claim['field'],'updated' if prior else 'added'),'materiality':'material',
                            'previous_value':prior['value'] if prior else None,'value':claim['value'],
                            'previous_evidence_ids':before_ids if imported else [],'evidence_ids':claim['evidence_ids']})
    stale_fields=set()
    for ck,prior in old_claims.items():
        if ck in current_by_key:continue
        if prior['field'] in {'official_website','company_profile','site_description','dated_activity','hiring_signal','careers_listing','site_location'} and any(
            host_of(old_evidence.get(eid,{}).get('source_url','')) in invalid_site_hosts for eid in prior['evidence_ids']):
            # Withdraw invalid assertions without repeating their rejected URL
            # in the public envelope. Original captures remain in local history.
            changes.append({'field':prior['field'],'claim_key':ck,'type':'invalidated_claim',
                            'materiality':'material','reason':'Prior site evidence names another legal entity; assertion withdrawn',
                            'evidence_ids':[],'previous_evidence_ids':[]})
            continue
        # A newly filed year supersedes the latest marker, but old financial years stay.
        if prior['field']=='annual_accounts_latest' and any(c['field']=='annual_accounts_latest' for c in current):continue
        # Different singleton value supersedes the old one. Lists require explicit closure proof.
        singleton=prior['field'] not in {'leader','workplace','company_profile','dated_activity','hiring_signal','careers_listing','former_name','site_location','revenue','operating_result','annual_result','equity','total_assets','total_debt','result_before_tax','annual_accounts_history','annual_report_pdf'}
        if singleton and any(c['field']==prior['field'] for c in current):continue
        why='Prior fact was not re-observed; removal is unproven'
        env.setdefault('refresh_unknowns',[]).append({'field':prior['field'],'reporting_period':prior.get('reporting_period'),'reason':why})
        if not import_evidence(prior['evidence_ids']):
            env['errors'].append({'stage':'refresh','state':'failed','message':'Prior capture unavailable; retained history but withheld unsupported carry-forward'})
            continue
        retained=copy.deepcopy(prior)
        retained.update(claim_key=ck,stale=True,freshness='not_reobserved',refresh_state='failed_or_incomplete',reason=why)
        env['claims'].append(retained);stale_fields.add(prior['field'])
    env['claims']=[c for c in env['claims'] if c['availability']=='available' or c['field'] not in stale_fields]
    current_facts=semantic_claims(env)
    digest=hashlib.sha256(canonical(sorted(current_facts)).encode()).hexdigest()
    unchanged=previous is not None and current_facts==semantic_claims(previous)
    env['refresh']={'content_sha256':digest,'previous_content_sha256':(previous or {}).get('refresh',{}).get('content_sha256'),
                    'previous_run_id':(previous or {}).get('run',{}).get('run_id'),'unchanged':unchanged,
                    'stale_claim_count':sum(bool(c.get('stale')) for c in env['claims'])}
    env['changes']=changes;env['summary']=summarize(env)
    validate(env,captures)
    if not unchanged:
        with history.open('a',encoding='utf-8') as handle:handle.write(canonical(env)+'\n')
    atomic_write(state,canonical({'source_directory':str(captures.resolve()),'envelope':env}))
    return env
