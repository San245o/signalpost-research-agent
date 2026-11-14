"""Candidate-only search recovery. Provider output never proves ownership."""
from __future__ import annotations
import json
import html
import re
import math
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from .net import clean_url
from .web import company_words, directory_url, former_names


def priority(record, envelope):
    address = record.get('forretningsadresse') or {}
    name = str(record.get('navn') or '')
    active = [record.get('konkurs'), record.get('underAvvikling')] == [False, False] and not record.get('slettedato')
    readiness = (30 if len(name)>=4 else 0)+(20 if address.get('adresse') else 0)+(10 if len(str(address.get('postnummer') or ''))==4 else 0)+(20 if active else 0)+(20 if len(str(record.get('organisasjonsnummer') or ''))==9 else 0)
    count = record.get('antallAnsatte')
    employees = count if isinstance(count,int) and not isinstance(count,bool) else 0
    revenue = next((c['value']['amount'] for c in envelope['claims'] if c['field']=='revenue' and c['availability']=='available'),0)
    dormant = any(t in name.casefold() for t in ('holding','invest','eiendom','sameie','borettslag'))
    potential = min(30,employees//2)+(25 if revenue>0 else 0)+(15 if record.get('hjemmeside') else 0)-(20 if dormant else 0)
    return readiness,max(0,potential),employees


def query_plan(record):
    name = str(record['navn'])
    town = str((record.get('forretningsadresse') or {}).get('kommune') or 'Norway')
    yield 'legal_name',f'"{name}" {town} official website',None
    for leader in record.get('_leaders',[])[:1]:
        yield 'leader',f'"{leader}" "{name}"',None
    yield 'org_number',str(record['organisasjonsnummer']),None
    for old in former_names(record)[:1]:
        yield 'former_name',f'"{old}" official website Norway',old


class Serper:
    """Compatibility name for the provider chain. Reserve spend on demand.

    Free successes do not strand paid credits. Readiness and priority retain
    the original project's policy. Auth/quota failure disables one provider.
    """
    def __init__(self, records, envelopes, *, mode='auto', credits=None, per_company=4, public_search=False, spend_cap=None,
                 categories=('websites','news','places'),website_rounds=1):
        names = ['serper','exa','parallel'] if mode=='auto' else [mode] if mode not in {'none','free'} else []
        self.keys = {p:os.environ.get(p.upper()+'_API_KEY','').strip() for p in names}
        self.keys = {p:k for p,k in self.keys.items() if k}
        self.key = self.keys.get('serper','')
        self.cap = max(0,int(credits if credits is not None else len(envelopes)//2)) if self.keys else 0
        self.per_company,self.public_search = per_company,public_search and mode!='none'
        self.lock=threading.Lock();self.used=0;self.calls=0;self.successes=0
        self.by_company=Counter();self.by_provider=Counter();self.failures=Counter()
        self.disabled=False;self.disabled_providers={};self.scores={};self.allocations={};self.attempts=[]
        self.spend_cap=spend_cap;self.spent=0.;self.cost_unknown=False
        self.categories=set(categories);self.website_rounds=website_rounds
        self.by_kind_company=Counter();self.cost_by_company=Counter();self.unknown_cost_companies=set()
        self.rates={}
        for p in self.keys:
            try:
                rate=float(os.environ.get(p.upper()+'_USD_PER_CREDIT',''))
                if rate>=0 and math.isfinite(rate):self.rates[p]=rate
            except ValueError:pass
        ranked=[]
        for org,record in records.items():
            readiness,potential,employees=priority(record,envelopes[org])
            self.scores[org]={'readiness':readiness,'potential':potential,'employees':employees}
            if readiness>=75:
                ranked.append((-potential,-employees,-readiness,org))
                self.allocations[org]=per_company
        self.ranked=[row[-1] for row in sorted(ranked)]

    def _reserve(self,org,provider='serper',kind='search'):
        with self.lock:
            if provider not in self.keys or provider in self.disabled_providers or self.used>=self.cap or self.by_company[org]>=self.allocations.get(org,0):return False
            if provider=='serper' and kind=='search' and self.by_kind_company[(org,kind)]>=self.website_rounds:return False
            rate=self.rates.get(provider)
            if self.spend_cap is not None and (rate is None or self.spent+rate>self.spend_cap+1e-9):
                self.disabled_providers[provider]='USD cap reached or rate undeclared'
                return False
            self.used+=1;self.calls+=1;self.by_company[org]+=1;self.by_provider[provider]+=1
            self.by_kind_company[(org,kind)]+=1
            self.spent+=rate or 0;self.cost_unknown|=rate is None
            self.cost_by_company[org]+=rate or 0
            if rate is None:self.unknown_cost_companies.add(org)
            return True

    def _search(self,provider,query,org,client,kind='search'):
        if provider=='serper':
            endpoint='https://google.serper.dev/'+kind
            payload={'q':query,'gl':'no','hl':'no','num':5};header='X-API-KEY';result_key={'search':'organic','news':'news','places':'places'}[kind]
        elif provider=='exa':
            endpoint='https://api.exa.ai/search'
            payload={'query':query,'numResults':5,'type':'auto'};header='x-api-key';result_key='results'
        else:
            endpoint='https://api.parallel.ai/v1/search'
            payload={'objective':'Find the exact Norwegian company official website: '+query,
                     'search_queries':[query],'mode':'fast','advanced_settings':{'max_results':5}}
            header='x-api-key';result_key='results'
        day=datetime.now(timezone.utc).date().isoformat()
        cache_key='SEARCH:'+day+':'+endpoint+':'+json.dumps(payload,sort_keys=True)
        result=client.disk_cache.get(cache_key,86400) if client.disk_cache and not client.fresh else None
        cached=result is not None
        if not result:
            if not self._reserve(org,provider,kind):return []
            result=client.post_json(endpoint,payload,{header:self.keys[provider]})
        receipt={'organisation_number':org,'provider':provider,'query':query,'status':result.status,
                 'kind':kind,
                 'cache_hit':cached,'content_sha256':result.digest,'retrieved_at':result.retrieved_at}
        with self.lock:self.attempts.append(receipt)
        if result.status!=200:
            with self.lock:
                self.failures[provider+':'+str(result.status or 'transport')]+=1
                if result.status in {401,402,403,429}:self.disabled_providers[provider]='HTTP '+str(result.status)
                self.disabled=bool(self.keys) and all(p in self.disabled_providers for p in self.keys)
            return []
        try:
            body=result.json()
            if not isinstance(body,dict) or not isinstance(body.get(result_key),list):raise ValueError('Invalid provider response')
        except (ValueError,TypeError):
            with self.lock:self.failures[provider+':invalid_response']+=1
            return []
        if cached:client.capture(result)
        elif client.disk_cache:client.disk_cache.put(cache_key,result)
        with self.lock:
            self.successes+=1
            reported=body.get('credits',1) if provider=='serper' else 1
            if isinstance(reported,(int,float)) and not isinstance(reported,bool) and math.isfinite(reported) and reported>1 and not cached:
                self.used+=reported-1;self.by_company[org]+=reported-1
                self.spent+=(reported-1)*self.rates.get(provider,0)
                self.cost_by_company[org]+=(reported-1)*self.rates.get(provider,0)
                self.disabled_providers[provider]='Unexpected credit charge; further calls stopped'
                self.disabled=all(p in self.disabled_providers for p in self.keys)
                self.failures['unexpected_credit_charge']+=1
        return body[result_key][:5]

    def website_candidates(self,record,client):
        seen=set();org=str(record['organisasjonsnummer'])
        for tier,query,alias in query_plan(record):
            if alias:
                from .web import namesake
                if namesake({**record,'navn':alias},client):continue
            for provider in self.keys:
                if 'websites' not in self.categories:continue
                for row in self._search(provider,query,org,client):
                    if not isinstance(row,dict):continue
                    url=clean_url(row.get('link') or row.get('url'))
                    if not url or directory_url(url):continue
                    if tier=='legal_name':
                        wanted=set(company_words(record['navn']))
                        context=set(company_words(str(row.get('title') or '')+' '+str(row.get('snippet') or '')+' '+str(row.get('text') or '')))
                        if wanted and len(wanted&context)/len(wanted)<.6:continue
                    parts=urlsplit(url);root=f'{parts.scheme}://{parts.netloc}/'
                    if root in seen:continue
                    seen.add(root)
                    method='former_name_search:'+alias if alias else provider+'_lead_requires_owner_proof'
                    yield root,method
            if self.public_search and tier=='legal_name':
                url='https://api.duckduckgo.com/?'+urlencode({'q':' '.join(company_words(record['navn'])),'format':'json','no_html':1})
                response=client.get(url,retries=1)
                if response.status not in {200,202}:continue
                try:
                    data=response.json();rows=[]
                    for item in (data.get('Infobox') or {}).get('content') or []:
                        if isinstance(item,dict) and str(item.get('label','')).casefold() in {'website','official website'}:
                            value=html.unescape(str(item.get('value') or ''))
                            match=re.search(r'https?://[^\s<>"\x27]+',value)
                            if match:rows.append(match.group())
                            elif '<' not in value and ' ' not in value:rows.append(value)
                    rows.append(data.get('AbstractURL'))
                    def topics(items):
                        for item in items:
                            if isinstance(item,dict):
                                if item.get('FirstURL'):rows.append(item['FirstURL'])
                                topics(item.get('Topics') or [])
                    topics(data.get('RelatedTopics') or [])
                    for lead in rows[:8]:
                        lead=clean_url(lead)
                        if lead and not directory_url(lead):
                            parts=urlsplit(lead);root=f'{parts.scheme}://{parts.netloc}/'
                            if root not in seen:
                                seen.add(root);yield root,'public_search_lead'
                except (ValueError,TypeError,AttributeError):continue

    def enrich(self,profile,record,client):
        """Optional Desktop-style news/Places discovery under the SAME allowance.

        Places rows remain candidate records; no rating is published from a
        search listing. News requires a fetched article and exact subject proof.
        """
        if 'serper' not in self.keys:return
        org=str(record['organisasjonsnummer']);name=str(record['navn'])
        if 'news' in self.categories:
            rows=self._search('serper',f'"{name}" Norway',org,client,kind='news')
            from .external import independent_article
            for row in rows[:2]:
                if not isinstance(row,dict):continue
                url=clean_url(row.get('link'))
                if not url or directory_url(url):continue
                result=client.get(url,retries=0,max_age=300)
                if result.status==200:independent_article(profile,result,record)
        address=record.get('forretningsadresse') or {}
        street=address.get('adresse') or []
        street=' '.join(street) if isinstance(street,list) else str(street)
        postcode=str(address.get('postnummer') or '')
        if 'places' in self.categories and street and len(postcode)==4 and self.scores.get(org,{}).get('potential',0)>0:
            rows=self._search('serper',f'"{name}" "{street}" {postcode}',org,client,kind='places')
            for row in rows[:5]:
                if isinstance(row,dict):
                    profile.data.setdefault('external_discovery',[]).append({'kind':'place_candidate','title':row.get('title'),
                         'address':row.get('address'),'website':clean_url(row.get('website')),
                         'state':'unverified','reason':'API listing only; no rating or review published without source rights and exact-entity corroboration'})

    def report(self):
        return {'configured':bool(self.keys),'providers':list(self.keys),'credits_used':self.used,
                'credit_limit':self.cap,'calls':self.calls,'successes':self.successes,
                'calls_by_provider':dict(self.by_provider),'disabled_providers':dict(self.disabled_providers),
                'failures':dict(self.failures),'third_party_cost_usd':None if self.cost_unknown else round(self.spent,6),
                'usd_per_credit':self.rates.get('serper') if set(self.keys)=={'serper'} else None,
                'spend_cap_usd':self.spend_cap,'public_search':self.public_search,
                'paid_categories':sorted(self.categories),'website_paid_rounds':self.website_rounds,
                'policy':'Free candidates first; readiness >=75; on-demand shared allowance; search is never owner proof'}
