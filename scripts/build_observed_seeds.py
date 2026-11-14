"""Build our own reverse organisation/domain index from an explicit public host list.

No page text is shipped. Live website ownership and captured source hashes are
retained in local state; output rows are discovery candidates for a fresh run.
"""
import argparse
import gzip
import hashlib
import json
import queue
import random
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from signalpost.net import Budget,Client,clean_url,host_of,utcnow
from signalpost.records import atomic_write
from signalpost.site_discovery import fetch_candidate
from signalpost.web import Page,ownership,stated_numbers,directory_url,PARKED


def owners(page,universe):
    if PARKED.search(page.root.text()) or directory_url(page.response.url):return []
    ids=stated_numbers(page.root.text())
    if len(ids)!=1:return []
    org=next(iter(ids))
    if org not in universe:return []
    proof=ownership(page,universe[org])
    if not proof or proof.get('confidence')!=1:return []
    url=page.response.url;p=urlsplit(url);root=urlunsplit((p.scheme,p.netloc,'/','',''))
    return [{'organisation_number':org,'url':root,'source_record':url,
             'retrieved_at':page.response.retrieved_at,'content_sha256':page.response.digest,'rule':proof['method']}]


def build(args):
    state=Path(args.state);state.mkdir(parents=True,exist_ok=True)
    source=Path(args.domains);universe_path=Path(args.universe)
    open_universe=gzip.open if universe_path.suffix=='.gz' else open
    universe={}
    with open_universe(universe_path,'rt',encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line);org=str(row.get('organisasjonsnummer') or row.get('organisation_number') or '')
            if len(org)==9:universe[org]={**row,'organisasjonsnummer':org}
    raw=source.read_bytes();text=raw.decode('utf-8');values=json.loads(text) if source.suffix=='.json' else text.splitlines()
    if not isinstance(values,list):raise ValueError('Expected a public list of domains or URLs')
    urls=[];seen=set()
    for value in values:
        url=clean_url(value)
        if url and not directory_url(url) and host_of(url) not in seen:
            p=urlsplit(url);seen.add(host_of(url));urls.append(urlunsplit((p.scheme,p.netloc,'/','','')))
    rng=random.Random(args.seed);rng.shuffle(urls);urls=urls[:args.limit]
    previous=[];journal=state/'observations.jsonl'
    if journal.exists():previous=[json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines() if line.strip()]
    completed={item['requested_url'] for item in previous};todo=queue.Queue();done=queue.Queue()
    for url in urls:
        if url not in completed:todo.put(url)
    expected=todo.qsize();budget=Budget(args.requests,args.seconds);stop=threading.Event()
    def worker():
        while not stop.is_set() and budget.remaining()>0:
            try:url=todo.get_nowait()
            except queue.Empty:return
            client=Client(budget,host_of(url),company_seconds=25,timeout=4,captures=state/'sources')
            item={'requested_url':url,'owners':[]}
            try:
                result,attempts=fetch_candidate(client,url);item.update(status=result.status,state=result.state)
                if result.status==200:
                    page=Page(result);item['owners']=owners(page,universe)
                    if not item['owners'] and not stated_numbers(page.root.text()) and not PARKED.search(page.root.text()):
                        contacts=[u for u,n in page.links() if host_of(u)==host_of(result.url) and any(t in (u+' '+n.text()).lower() for t in ['kontakt','contact','personvern','privacy','om-oss','about'])]
                        if contacts:
                            contact=client.get(contacts[0],retries=0)
                            if contact.status==200 and host_of(contact.url)==host_of(result.url):item['owners']=owners(Page(contact),universe)
            except Exception as exc:item['error']=type(exc).__name__
            done.put(item)
    for index in range(min(args.workers,expected)):
        threading.Thread(target=worker,name='public-index-'+str(index),daemon=True).start()
    received=0;started=time.monotonic()
    with journal.open('a',encoding='utf-8') as stream:
        while received<expected and budget.remaining()>0:
            try:item=done.get(timeout=.2)
            except queue.Empty:continue
            previous.append(item);stream.write(json.dumps(item,ensure_ascii=False)+'\n');stream.flush();received+=1
    stop.set();budget.stopped.set()
    grouped={}
    for item in previous:
        for pair in item.get('owners',[]):
            path=state/'sources'/(pair['content_sha256']+'.bin')
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=pair['content_sha256']:continue
            grouped.setdefault(pair['organisation_number'],{})[pair['url']]=pair
    rows=[]
    for org,mapping in sorted(grouped.items()):
        leads=sorted(mapping)[:3]
        rows.append({'organisation_number':org,'urls':leads,
                     'verification_pages':list(dict.fromkeys(mapping[u]['source_record'] for u in leads)),
                     'provenance':{u:[{k:v for k,v in mapping[u].items() if k not in {'organisation_number','url'}}] for u in leads}})
    body=''.join(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n' for row in rows).encode()
    archive=gzip.compress(body,mtime=0);output=Path(args.out);output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(archive)
    manifest={'schema_version':2,'generated_at':utcnow(),'archive_sha256':hashlib.sha256(archive).hexdigest(),
              'companies':len(rows),'website_urls':sum(len(row['urls']) for row in rows),'candidate_only':True,
              'sources':[{'filename':source.name,'sha256':hashlib.sha256(raw).hexdigest(),'kind':'explicit public host list'},
                         {'filename':universe_path.name,'sha256':hashlib.sha256(universe_path.read_bytes()).hexdigest(),'kind':'public registry universe; NLOD-2.0'}],
              'policy':'Live public DNS/TLS/robots checks; exact ownership-labelled identifier; page text stays in local state; all candidates require fresh runtime verification.',
              'requests':budget.used,'selected_hosts':len(urls),'new_completed_hosts':received,'cached_completed_hosts':len(previous)-received,'seconds':round(time.monotonic()-started,3)}
    atomic_write(output.with_suffix('.manifest.json'),json.dumps(manifest,indent=2))
    atomic_write(state/'build-report.json',json.dumps(manifest,indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domains',required=True);parser.add_argument('--universe',required=True)
    parser.add_argument('--state',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--limit',type=int,default=500);parser.add_argument('--requests',type=int,default=1500)
    parser.add_argument('--seconds',type=float,default=300);parser.add_argument('--workers',type=int,default=8);parser.add_argument('--seed',type=int,default=20261114)
    args=parser.parse_args()
    if min(args.limit,args.requests,args.seconds,args.workers)<=0 or args.workers>16:parser.error('Positive limits and 1..16 workers required')
    print(json.dumps(build(args),indent=2))
