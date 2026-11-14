"""Export an explicit allowlist of original public caches as URL leads only.

SQLite inputs are read-only. No credentials, profiles or cached facts are read.
Source indices on each URL resolve through the companion manifest.
"""
import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict, Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from urllib.parse import urlsplit, urlunsplit
from signalpost.net import clean_url, host_of
from signalpost.web import directory_url, canonical_social, company_words
from scripts.build_original_screening import build as build_screening


def build(source, output):
    source,output=Path(source),Path(output)
    leads=defaultdict(dict);pages=defaultdict(list);graphs=defaultdict(list)
    socials=defaultdict(dict);sources=[];counts=Counter();guesses=set()
    def register(path, method):
        if not path.exists():return None
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        sources.append({'filename':path.name,'sha256':digest,'extraction':method,
                        'source_file_modified_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()})
        return len(sources)-1
    def add(org,value,sid):
        org=str(org or '')
        if not re.fullmatch(r'\d{9}',org) or not isinstance(value,str):return
        url=clean_url(value)
        if not url:return
        social=canonical_social(url)
        if social:
            socials[org].setdefault(social[0],set()).add(sid);return
        if directory_url(url):return
        p=urlsplit(url);root=urlunsplit((p.scheme,p.netloc,'/','',''))
        leads[org].setdefault(root,set()).add(sid)
        if url!=root and url not in pages[org]:pages[org].append(url)
        counts[sid]+=1
    for filename in ('official_411k_domains.json','universe-websites.json','wikidata-websites.json',
                     'fagfolkguiden_harvested.json','universe_linkedin_profiles.json','universe_social_profiles.json'):
        path=source/filename
        sid=register(path,'URL fields only; keyed by organisation number')
        if sid is None:continue
        data=json.loads(path.read_text(encoding='utf-8'))
        for org,value in data.items():
            if isinstance(value,str):add(org,value,sid)
            elif isinstance(value,dict):
                for key in ('h','e','website','linkedin_url'):add(org,value.get(key),sid)
                same=value.get('sameAs') or []
                for url in same if isinstance(same,list) else [same]:add(org,url,sid)
                for url in (value.get('platforms') or {}).values():add(org,url,sid)
    path=source.parent/'wikidata_enriched.json'
    sid=register(path,'website, social URL hints and Wikidata entity IDs only')
    if sid is not None:
        for org,item in json.loads(path.read_text(encoding='utf-8')).items():
            if not re.fullmatch(r'\d{9}',str(org)) or not isinstance(item,dict):continue
            add(org,item.get('website'),sid)
            qid=str(item.get('wikidata_url') or '').rsplit('/',1)[-1]
            if re.fullmatch(r'Q[1-9]\d*',qid):graphs[org].append(qid)
            for url in (item.get('socials') or {}).values():add(org,url,sid)
    names=[];registered_names=defaultdict(set);name_source=None
    for filename,query in (
        ('brreg_companies.sqlite','SELECT organisation_number, website, name FROM companies'),
        ('brreg_subunits.sqlite','SELECT parent_org, website, name FROM subunits'),
        ('verified-sites.sqlite','SELECT org, url, NULL FROM sites')):
        path=source/filename
        sid=register(path,'public website column; subunit URLs are parent leads, not parent ownership')
        if sid is None:continue
        if filename=='brreg_companies.sqlite':name_source=sid
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            for org,url,name in connection.execute(query):
                add(org,url,sid)
                if filename=='brreg_companies.sqlite' and name:names.append((org,name))
                if filename in {'brreg_companies.sqlite','brreg_subunits.sqlite'} and name:
                    key=' '.join(company_words(name))
                    if key:registered_names[key].add(str(org))
    token_holders=Counter(token for _,name in names for token in set(company_words(name)))
    generic={'holding','holdings','invest','investering','eiendom','eiendommer','capital','solutions','consulting','service','services','norge','norway','gruppen','group'}
    path=source/'unique_crawler_domains.json'
    sid=register(path,'whole folded legal-name/domain-label equality joined to brreg_companies; discovery only')
    if sid is not None:
        sources[sid]['extraction']='Whole legal-name or rare name-token/domain-label join to public register; candidate only'
        index=defaultdict(list)
        for value in json.loads(path.read_text(encoding='utf-8')):
            url=clean_url(value)
            if not url or directory_url(url):continue
            host=host_of(url)
            if not re.fullmatch(r'[a-z0-9.-]+',host):continue
            key=''.join(company_words(host.split('.')[0]))
            if len(key)>=5:index[key].append(url)
        for org,name in names:
            tokens=company_words(name)
            for url in sorted(index.get(''.join(tokens),[]))[:3]:add(org,url,sid)
            rare=sorted((t for t in set(tokens) if len(t)>=6 and t not in generic and token_holders[t]<=12),key=lambda t:(token_holders[t],-len(t),t))
            for token in rare[:2]:
                for url in sorted(index.get(token,[]))[:2]:add(org,url,sid)
    # A rare, substantial non-leading name token can be an operating brand.
    # These are explicit guesses from our public register, not observed domains.
    if name_source is not None:
        sources[name_source]['extraction']+='; rare non-leading legal-name token .no guesses, candidate only'
        for org,name in names:
            tokens=company_words(name)
            rare=sorted((t for t in set(tokens[1:]) if len(t)>=8 and t not in generic and token_holders[t]<=12),key=lambda t:(token_holders[t],-len(t),t))
            if rare:
                url='https://'+rare[0]+'.no/'
                if url not in leads[str(org)]:guesses.add((str(org),url))
                add(org,url,name_source)
    nav=defaultdict(list);nav_source=None
    path=source.parent/'nav-feed-snapshot.json'
    if path.exists():
        nav_source=register(path,'ACTIVE UUIDs joined on unique registered company/subunit names; live exact-employer check required')
        snapshot=json.loads(path.read_text(encoding='utf-8'));entries=snapshot.get('entries')
        encoded=json.dumps(entries,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
        if not isinstance(entries,dict) or hashlib.sha256(encoded).hexdigest()!=snapshot.get('entries_sha256'):raise ValueError('Original NAV index failed integrity check')
        for item in entries.values():
            if item.get('status')!='ACTIVE' or not re.fullmatch(r'[a-fA-F0-9-]{36}',str(item.get('uuid') or '')):continue
            owners=registered_names.get(' '.join(company_words(item.get('business_name'))),set())
            if len(owners)==1:
                org=next(iter(owners));nav[org].append({'uuid':item['uuid'],'business_name':item.get('business_name'),'source_index':nav_source})
    social_owners=defaultdict(set)
    for org,urls in socials.items():
        for url in urls:social_owners[url].add(org)
    shared_socials={url for url,owners in social_owners.items() if len(owners)>1}
    rows=[]
    for org in sorted(leads.keys()|graphs.keys()|socials.keys()|nav.keys()):
        urls=sorted(leads[org],key=lambda u:((org,u) in guesses,-len(leads[org][u]),min(leads[org][u]),u))[:6]
        row={'organisation_number':org,'urls':urls,'origins':[sorted(leads[org][u]) for u in urls]}
        proof=[p for p in pages[org] if any(host_of(p)==host_of(u) for u in urls)]
        if proof:row['verification_pages']=proof[:6]
        if graphs[org]:row['wikidata']=list(dict.fromkeys(graphs[org]))[:2]
        if nav[org]:row['nav_ads']=nav[org][:12]
        if socials[org]:
            selected=sorted(u for u in socials[org] if u not in shared_socials)[:8];row['social_urls']=selected
            row['social_origins']=[sorted(socials[org][u]) for u in selected]
        rows.append(row)
    body=''.join(json.dumps(row,separators=(',',':'),ensure_ascii=False)+'\n' for row in rows).encode('utf-8')
    archive=gzip.compress(body,mtime=0)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(archive)
    screening=None
    bulk=source.parent/'brreg-enheter.csv.gz'
    if bulk.exists():screening=build_screening(bulk,output.with_suffix('.screening.json.gz'))
    manifest={'schema_version':2,'generated_at':datetime.now(timezone.utc).isoformat(),
              'archive_sha256':hashlib.sha256(archive).hexdigest(),'uncompressed_sha256':hashlib.sha256(body).hexdigest(),
              'uncompressed_bytes':len(body),'companies':len(rows),'website_urls':sum(len(r['urls']) for r in rows),
              'website_companies':sum(bool(r['urls']) for r in rows),
              'social_url_hints':sum(len(r.get('social_urls',[])) for r in rows),'wikidata_companies':sum(bool(r.get('wikidata')) for r in rows),
              'shared_social_urls_excluded':len(shared_socials),
              'nav_companies':sum(bool(r.get('nav_ads')) for r in rows),'screening':screening,
              'sources':sources,'source_candidate_occurrences':dict(counts),
              'acquisition_timestamps':'Unknown; file modification times are not retrieval times',
              'use':'Public URL candidates and provenance only. Re-fetch and prove exact entity before publication.'}
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8',newline='\n')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-dir',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args();print(json.dumps(build(args.source_dir,args.out),indent=2))
