"""Role cards observed on verified careers pages, with explicit listing semantics."""
import re
from urllib.parse import urlsplit
from .web import same_site,stated_numbers,parse_date,words

CAREERS=re.compile(r'career|karriere|\bjobs?\b|jobb|ledig|stilling|vacanc|open positions',re.I)
NO_OPENINGS=re.compile(r'ingen (?:ledige|åpne)|no (?:current |open |available )?(?:vacancies|positions|openings|jobs)|not (?:currently )?hiring',re.I)
DETAIL=re.compile(r'/(?:stillinger?|positions?|vacancies|openings?|annonser?)/[^/?#]+|/(?:jobs?|careers?|karriere)/\d{4,}[^/?#]*|[?&](?:jobid|job_id|finnkode|positionid)=\d+|/stilling/[a-f0-9-]{36}',re.I)
BAD=re.compile(r'^(?:read more|les mer|apply now|søk nå|see details|se alle|view all|careers?|jobs?)$|open application|åpen søknad|talent pool|privacy|cookie',re.I)
NON_ROLE=re.compile(r'/(?:people|employees|team|departments|locations|news|blog|stories|events|login|admin)(?:/|$)',re.I)


def is_recruiter(record):
    return str((record.get('naeringskode1') or {}).get('kode','')).startswith('78.')


def advertised_role(profile,record,title,url,response,span,*,listing_url=None,posted=None,expires=None,
                    source_class='company_ats_page',method='public_ats_feed',provenance=(),advertised_employer=None):
    """An advertiser relationship never asserts employment at the advertiser."""
    relationship='recruiter_advertisement' if is_recruiter(record) else 'advertised_role; direct_employer_not_independently_confirmed'
    value={'title':title,'url':url,'listing_url':listing_url or response.url,
           'advertiser_organisation_number':profile.org,'advertiser_name':record.get('navn'),
           'observed_at':response.retrieved_at[:10],'relationship':relationship,'direct_employer_verified':False}
    if posted:value['date']=posted.isoformat()
    if expires:value['valid_through']=expires.isoformat()
    if advertised_employer:value['advertised_employer_name']=advertised_employer
    profile.add('hiring_signal',value,response,span,source_class,method=method,period=posted.isoformat() if posted else None)
    claim=next(c for c in profile.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available' and c['value'].get('url')==url)
    # A DOM card may precede the richer feed for the same URL. Keep one role,
    # retaining the feed's dates and verbatim evidence without weakening a
    # previously verified direct-employer relationship.
    if claim['value'].get('relationship') and not claim['value'].get('direct_employer_verified'):
        claim['value'].update(value)
        if posted:claim['reporting_period']=posted.isoformat()
        claim['last_observed_at']=response.retrieved_at
    ids=claim['evidence_ids']
    eid=profile.evidence(response,span,source_class,method=method,reporting_period=posted.isoformat() if posted else None)
    if eid not in ids:ids.append(eid)
    for proof_response,proof_span in provenance:
        eid=profile.evidence(proof_response,proof_span,'company_owned',method='verified_careers_link_chain')
        if eid not in ids:ids.append(eid)
    for c in profile.data['claims']:
        if c['field'] in {'legal_name','official_website'} and c['availability']=='available':
            for eid in c['evidence_ids']:
                if eid not in ids:ids.append(eid)


def _card(page,node):
    title=node.text();headings=list(node.all({'h2','h3','h4','strong'}))
    if len(headings)==1:title=headings[0].text()
    if not title or BAD.search(title):
        parent=node.parent
        for _ in range(3):
            if not parent or parent.tag in {'document','body','main'}:break
            raw=page.span(parent);heads=list(parent.all({'h2','h3','h4'}))
            if len(raw)<=6000 and len(heads)==1:return heads[0].text(),raw
            parent=parent.parent
    return title,page.span(node)


def inline_role(profile,page,record,home):
    """An explicit inline recruitment ad is an observation, not confirmed status."""
    from .content import content_nodes
    if not same_site(page.response.url,home):return
    nodes=list(content_nodes(page,{'p','li','h1','h2','h3','a','button'}))
    body=' '.join(n.text() for n in nodes)
    if NO_OPENINGS.search(body) or not re.search(r'qualifications|responsibilities|kvalifikasjon|arbeidsoppgaver|fagbrev',body,re.I):return
    apply=[n for n in nodes if re.search(r'send (?:din )?søknad|søk (?:på stillingen|her|nå)|apply (?:now|here)|send (?:your )?(?:application|cv)',n.text(),re.I)]
    if not apply:return
    roles=[]
    for node in nodes:
        if node.tag not in {'p','h1','h2','h3'} or not 12<=len(node.text())<=1000:continue
        found=re.search(r'\b(?:vi søker|vi trenger|we are hiring|we are looking for|join us as)\s+(?:en |an? )?(.{3,100}?)(?=\s+(?:som|med|til|with|who|to join)\b|[,.!]|$)',node.text(),re.I)
        if not found:continue
        title=found[1].strip(' :–-')
        if not 3<=len(title)<=100 or BAD.search(title) or re.fullmatch(r'deg|folk|people|talenter|nye medarbeidere|kolleger',title,re.I):continue
        if title.casefold() not in {t.casefold() for t,n in roles}:roles.append((title,node))
    if len(roles)!=1:return
    deadline=re.search(r'(?:søknadsfrist|application deadline|apply by)\s*:?\s*([^\n]{6,40})',body,re.I)
    if deadline:
        from datetime import datetime,timezone
        expiry=parse_date(deadline[1])
        if expiry and expiry<datetime.now(timezone.utc).date():return
    title,node=roles[0]
    proofs=[(page.response,page.span(apply[0]))]
    qualifier=next((n for n in nodes if re.search(r'qualifications|responsibilities|kvalifikasjon|arbeidsoppgaver|fagbrev',n.text(),re.I)),None)
    if qualifier:proofs.append((page.response,page.span(qualifier)))
    advertised_role(profile,record,title,page.response.url,page.response,page.span(node),source_class='company_owned',
                    method='inline_careers_advertisement',provenance=proofs)


def listed_roles(profile,page,record,home,*,linked_from=None,on_ats=lambda u:False):
    visible=page.root.text()
    if not CAREERS.search(page.response.url+' '+' '.join(n.text() for n in page.root.all({'title','h1'}))):return
    if NO_OPENINGS.search(visible):return
    ids=stated_numbers(visible)
    if ids-{profile.org}:return
    own=same_site(page.response.url,home)
    via_link=bool(linked_from and same_site(linked_from,home) and on_ats(page.response.url))
    if not own and not via_link:return
    path=[p for p in urlsplit(home).path.split('/') if p]
    branch=any(not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?',p,re.I) for p in path)
    legal=words(record.get('navn'));got=words(visible)
    full_name=bool(legal) and any(got[i:i+len(legal)]==legal for i in range(len(got)-len(legal)+1))
    if (branch or not own) and ids!={profile.org} and not full_name:return
    inline_role(profile,page,record,home)
    roles=[];seen=set()
    for url,node in page.links():
        if url in seen or NON_ROLE.search(urlsplit(url).path):continue
        if not DETAIL.search(url):
            if re.search(r'/jobs?/[^/]+',url,re.I):
                profile.data.setdefault('crawl_attempts',[]).append({'url':url,'kind':'unconfirmed_career_role','title':node.text(),'outcome':'withheld','reason':'Link alone does not establish an individual vacancy'})
            continue
        if not same_site(url,page.response.url) and not on_ats(url):continue
        title,span=_card(page,node)
        if not 6<=len(title)<=180 or BAD.search(title):continue
        deadline=re.search(r'(?:søknadsfrist|application deadline|apply by)\s*:?\s*([^<\n]{6,40})',re.sub('<[^>]+>',' ',span),re.I)
        if deadline:
            expiry=parse_date(deadline[1])
            from datetime import datetime,timezone
            if expiry and expiry<datetime.now(timezone.utc).date():continue
        seen.add(url);roles.append((title,url,span))
    offsite=any(not same_site(url,page.response.url) for _,url,_ in roles)
    for title,url,span in roles[:8]:
        if len(roles)<2 and not offsite:
            profile.data.setdefault('crawl_attempts',[]).append({'url':url,'kind':'unconfirmed_career_role','title':title,'outcome':'withheld','reason':'Insufficient evidence of a role listing'})
            continue
        if is_recruiter(record):
            advertised_role(profile,record,title,url,page.response,span,listing_url=page.response.url,
                            source_class='company_owned' if own else 'company_ats_page',method='dom_recruiter_role_card')
            continue
        profile.add('hiring_signal',{'title':title,'url':url,'listing_url':page.response.url,
                    'observed_at':page.response.retrieved_at[:10],
                    'relationship':'role_listed_on_verified_careers_page; individual_ad_status_not_confirmed'},
                    page.response,span,'company_owned' if own else 'company_ats_page',
                    confidence=.95,method='dom_career_role_card',linked_from=linked_from or home)
