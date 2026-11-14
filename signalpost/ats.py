"""Public job feeds discovered through the verified site's own careers links."""
import re
from datetime import datetime,timezone
from urllib.parse import urljoin,urlsplit,parse_qs
from .net import clean_url,host_of
from .registry import json_document
from .web import company_words,words,LEGAL,parse_date,valid_org
from .careers import BAD,DETAIL,NON_ROLE,is_recruiter,advertised_role


def feed_links(page):
    seen=set()
    for node in page.root.all({'link','a'}):
        href=node.attrs.get('href')
        if href and ('feed+json' in str(node.attrs.get('type','')) or re.search(r'/jobs\.json(?:[?]|$)',href)):
            url=clean_url(urljoin(page.response.url,href))
            if url and host_of(url)==host_of(page.response.url):
                seen.add(url);yield url,page.span(node)
    # Teamtailor exposes the standard JSON Feed extension on its own tenant.
    marker=re.search(r'<(?:script|link|img|meta)\b[^>]*(?:teamtailor-cdn\.com|teamtailor-production|static\.teamtailor\.com)[^>]*>',page.raw,re.I)
    if marker or host_of(page.response.url).endswith('.teamtailor.com'):
        p=urlsplit(page.response.url);url=f'{p.scheme}://{p.netloc}/jobs.json'
        if url not in seen:yield url,marker.group() if marker else page.raw[:min(1000,len(page.raw))]
    p=urlsplit(page.response.url);host=host_of(page.response.url);parts=p.path.strip('/').split('/')
    tenant=parts[0] if parts else ''
    if page.raw and host in {'boards.greenhouse.io','job-boards.greenhouse.io'}:
        tenant=(parse_qs(p.query).get('for') or [tenant])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{1,80}',tenant):
            yield 'https://boards-api.greenhouse.io/v1/boards/'+tenant+'/jobs',page.raw[:min(1500,len(page.raw))]
    if page.raw and host in {'jobs.lever.co','jobs.eu.lever.co'} and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',tenant):
        api='api.eu.lever.co' if host=='jobs.eu.lever.co' else 'api.lever.co'
        yield 'https://'+api+'/v0/postings/'+tenant+'?mode=json',page.raw[:min(1500,len(page.raw))]


def issuer_matches(name,record):
    got=words(name);wanted=words(record.get('navn'))
    if not got or not wanted:return False
    if got[-1] in LEGAL and wanted[-1] in LEGAL and got[-1]!=wanted[-1]:return False
    return company_words(name)==company_words(record.get('navn'))


def collect_feed(profile,response,record,home,*,provenance=()):
    if response.status!=200 or not provenance:return 0
    try:data,spans=json_document(response)
    except (ValueError,TypeError,IndexError):return 0
    provider_host=host_of(response.url)
    if provider_host in {'boards-api.greenhouse.io','api.lever.co','api.eu.lever.co'}:
        return collect_board(profile,response,data,spans,record,provenance)
    if not isinstance(data,dict) or not isinstance(data.get('items'),list) or data.get('expired') is True:return 0
    if not str(data.get('version','')).startswith('https://jsonfeed.org/version/'):return 0
    feed_host=host_of(response.url)
    for key in ('home_page_url','feed_url'):
        if data.get(key) and host_of(str(data[key]))!=feed_host:return 0
    today=datetime.now(timezone.utc).date();count=0
    board_ours=issuer_matches(data.get('title',''),record)
    for index,item in enumerate(data['items'][:30]):
        if not isinstance(item,dict):continue
        job=item.get('_jobposting')
        if not isinstance(job,dict):continue
        title=job.get('title') or item.get('title');url=clean_url(job.get('url') or item.get('url'))
        if not isinstance(title,str) or not 6<=len(title)<=180 or BAD.search(title):continue
        if not url or host_of(url)!=feed_host or not DETAIL.search(url) or NON_ROLE.search(urlsplit(url).path):continue
        if item.get('status') in {'INACTIVE','CLOSED','internal','draft'} or item.get('_internal') is True:continue
        posted=parse_date(job.get('datePosted') or item.get('date_published'));expires=parse_date(job.get('validThrough'))
        if posted and posted>today or expires and expires<today:continue
        if job.get('validThrough') and not expires:continue
        employer=job.get('hiringOrganization') or {}
        if not isinstance(employer,dict):continue
        raw_ids=str([employer.get(k) for k in ('identifier','taxID','vatID')])
        ids={''.join(m.groups()) for m in re.finditer(r'(?<!\d)(\d{3})[ .]?(\d{3})[ .]?(\d{3})(?!\d)',raw_ids)}
        ids={org for org in ids if valid_org(org)}
        issuer=employer.get('name') or ''
        if is_recruiter(record):
            if not board_ours and not issuer_matches(issuer,record) and ids!={profile.org}:continue
        elif ids and ids!={profile.org} or not ids and not issuer_matches(issuer,record):continue
        span=spans[('items',index)]
        if not is_recruiter(record) and ids=={profile.org} and posted and expires:
            value={'title':title,'url':url,'date':posted.isoformat(),'valid_through':expires.isoformat(),
                   'employer_organisation_number':profile.org}
            profile.add('hiring_signal',value,response,span,'company_ats_page',
                        period=posted.isoformat(),method='public_ats_feed_exact_employer')
            claim=next(c for c in profile.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available' and c['value'].get('url')==url)
            claim['value']=value;claim['reporting_period']=posted.isoformat();claim['last_observed_at']=response.retrieved_at
            eid=profile.evidence(response,span,'company_ats_page',reporting_period=posted.isoformat(),method='public_ats_feed_exact_employer')
            if eid not in claim['evidence_ids']:claim['evidence_ids'].append(eid)
        else:
            advertised_role(profile,record,title,url,response,span,posted=posted,expires=expires,
                            provenance=provenance,advertised_employer=issuer if issuer and not issuer_matches(issuer,record) else None)
        claim=next(c for c in profile.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available' and c['value'].get('url')==url)
        for source,proof_span in provenance:
            eid=profile.evidence(source,proof_span,'company_owned',method='verified_careers_link_chain')
            if eid not in claim['evidence_ids']:claim['evidence_ids'].append(eid)
        count+=1
        if count>=20:break
    return count


def collect_board(profile,response,data,spans,record,provenance):
    """Public linked-board listings have no invented posting or expiry date."""
    host=host_of(response.url);count=0
    if host=='boards-api.greenhouse.io':
        rows=data.get('jobs') if isinstance(data,dict) else None;prefix=('jobs',);title_key='title';url_key='absolute_url';allowed={'boards.greenhouse.io','job-boards.greenhouse.io'}
        path=re.fullmatch(r'/v1/boards/([A-Za-z0-9_-]+)/jobs',urlsplit(response.url).path)
    else:
        rows=data;prefix=();title_key='text';url_key='hostedUrl';allowed={'jobs.lever.co','jobs.eu.lever.co'}
        path=re.fullmatch(r'/v0/postings/([A-Za-z0-9_-]+)',urlsplit(response.url).path)
    if not path or not isinstance(rows,list):return 0
    tenant=path[1]
    def linked_tenant(source):
        p=urlsplit(source.url)
        return (parse_qs(p.query).get('for') or [p.path.strip('/').split('/')[0]])[0]
    if not any(host_of(source.url) in allowed and linked_tenant(source)==tenant and span for source,span in provenance):return 0
    for index,item in enumerate(rows[:30]):
        if not isinstance(item,dict):continue
        if host=='boards-api.greenhouse.io' and 'internal_job_id' in item and item['internal_job_id'] is None:continue
        title=item.get(title_key);url=clean_url(item.get(url_key));p=urlsplit(url)
        if not isinstance(title,str) or not 6<=len(title)<=180 or BAD.search(title):continue
        if host_of(url) not in allowed or p.path.strip('/').split('/')[0]!=tenant:continue
        if item.get('state') in {'closed','draft','internal'} or item.get('internal') is True:continue
        advertised_role(profile,record,title,url,response,spans[prefix+(index,)],provenance=provenance,
                        method='linked_public_job_board',source_class='company_ats_page')
        count+=1
        if count>=20:break
    return count
