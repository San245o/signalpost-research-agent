"""Declared public candidate maps. No cached URL is publication evidence."""
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
from .net import clean_url,host_of

DEFAULT=Path(__file__).resolve().parents[1]/'data/public-website-seeds.jsonl.gz'
OBSERVED=DEFAULT.parent/'observed-website-seeds.jsonl.gz'


def _read_seeds(wanted, *, path=DEFAULT, url=None, sha256=None, client=None):
    report={'source':url or str(path),'loaded_companies':0,'candidate_only':True}
    try:
        if url:
            if not sha256 or not client:raise ValueError('Hosted maps require a declared SHA-256')
            response=client.get(url,retries=0,max_age=86400)
            if response.status!=200:raise ValueError('Hosted map unavailable')
            raw=response.body;report['retrieved_at']=response.retrieved_at
        else:
            path=Path(path);raw=path.read_bytes()
            manifest_path=path.with_suffix('.manifest.json')
            if not sha256 and manifest_path.exists():
                manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
                sha256=manifest['archive_sha256'];report['manifest']=manifest
        if not sha256 or hashlib.sha256(raw).hexdigest()!=sha256:raise ValueError('Candidate map hash missing or mismatched')
        report['content_sha256']=sha256
        stream=gzip.GzipFile(fileobj=io.BytesIO(raw)) if raw[:2]==b'\x1f\x8b' else io.BytesIO(raw)
        records={};proof_pages={};hints={};size=0
        for line in stream:
            size+=len(line)
            if size>50_000_000 or len(line)>65536:raise ValueError('Candidate map size limit')
            row=json.loads(line);org=str(row.get('organisation_number') or '')
            if org in wanted:
                values=row.get('urls') or []
                records[org]=[u for value in values[:6] if isinstance(value,str) and (u:=clean_url(value))]
                hints[org]={'wikidata':[q for q in row.get('wikidata',[])[:2] if isinstance(q,str) and re.fullmatch(r'Q[1-9]\d*',q)],
                            'social_urls':[u for v in row.get('social_urls',[])[:8] if isinstance(v,str) and (u:=clean_url(v))],
                            'nav_ads':[item for item in row.get('nav_ads',[])[:12] if isinstance(item,dict)],
                            'website_origins':dict(zip(records[org],row.get('origins',[])))}
                scoped=[clean_url(v) for v in row.get('verification_pages',[])[:6] if isinstance(v,str)]
                scoped=[v for v in scoped if v and any(host_of(v)==host_of(u) for u in records[org])]
                if scoped:proof_pages[org]=scoped
                provenance=row.get('provenance') or {}
                if isinstance(provenance,dict):
                    pages=[]
                    safe_provenance={}
                    for candidate in records[org]:
                        entries=provenance.get(candidate,[])
                        for item in entries[:6] if isinstance(entries,list) else []:
                            if not isinstance(item,dict):continue
                            page=clean_url(item.get('source_record'))
                            if page and host_of(page)==host_of(candidate):
                                safe_provenance.setdefault(candidate,[]).append({key:item[key] for key in ('source_record','retrieved_at','content_sha256','rule') if key in item})
                            if page and page!=candidate and host_of(page)==host_of(candidate) and page not in pages:pages.append(page)
                    if pages:proof_pages[org]=list(dict.fromkeys(proof_pages.get(org,[])+pages))[:6]
                    if safe_provenance:hints[org]['provenance']=safe_provenance
        report['loaded_companies']=len(records)
        report['verification_pages']=proof_pages
        report['company_hints']=hints
        screening=(report.get('manifest') or {}).get('screening')
        if screening and not url:
            try:
                name=screening['filename']
                if Path(name).name!=name:raise ValueError('Invalid screening asset path')
                raw=(Path(path).parent/name).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=screening['sha256']:raise ValueError('Screening cache hash mismatch')
                report['screening']=json.loads(gzip.decompress(raw))
            except (OSError,ValueError,KeyError,TypeError,EOFError) as exc:report['screening_error']=str(exc)
        return records,report
    except (OSError,ValueError,TypeError,KeyError,EOFError) as exc:
        report['error']=str(exc)
        return {},report


def load_seeds(wanted, *, path=DEFAULT, url=None, sha256=None, client=None):
    records,report=_read_seeds(wanted,path=path,url=url,sha256=sha256,client=client)
    if not url and Path(path)==DEFAULT and OBSERVED.exists():
        observed,extra=_read_seeds(wanted,path=OBSERVED)
        report['observed_index']={k:v for k,v in extra.items() if k not in {'company_hints','verification_pages'}}
        for org,urls in observed.items():
            records[org]=list(dict.fromkeys(urls+records.get(org,[])))[:6]
            base=report.setdefault('company_hints',{}).setdefault(org,{})
            incoming=extra.get('company_hints',{}).get(org,{})
            base.setdefault('provenance',{}).update(incoming.get('provenance',{}))
            pages=report.setdefault('verification_pages',{})
            pages[org]=list(dict.fromkeys(extra.get('verification_pages',{}).get(org,[])+pages.get(org,[])))[:6]
        report['loaded_companies']=len(records)
    return records,report
