"""Follow the verified site's own links and publishing formats within one cap."""
from __future__ import annotations
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from .net import clean_url, host_of
from .registry import json_document
from .web import Page, same_site, parse_date, add_article, add_jobs, canonical_social, company_words, words, stated_numbers, valid_org, _types
from .news import about_company,publication_kind,eligible

ATS = {'greenhouse.io','lever.co','workable.com','teamtailor.com','myworkdayjobs.com','recman.no','jobylon.com','webcruiter.no','easycruit.com','hr-manager.net','varbi.com','personio.com','personio.de','bamboohr.com','smartrecruiters.com','ashbyhq.com','recruitee.com','finn.no','arbeidsplassen.nav.no','jobbnorge.no'}
ACTIVITY = re.compile(r'news|nyhet|aktuelt|press|blog|artikler|articles|karriere|career|ledig|stilling|jobb|jobs',re.I)
JOBS = re.compile(r'career|karriere|ledig|stilling|jobb|jobs',re.I)
NON_POST = re.compile(r'/(?:contact|kontakt|about|om-oss|privacy|cookies|careers|karriere|products?|produkt|category|tag|author)(?:/|$)',re.I)


def on_ats(url):
    host=host_of(url)
    return any(host==allowed or host.endswith('.'+allowed) for allowed in ATS)


def date_value(value):
    result=parse_date(value)
    if result:return result
    try:return parsedate_to_datetime(str(value)).date()
    except (TypeError,ValueError,OverflowError):return None


def post_ok(title, published, url, home, today, founded=None):
    return isinstance(title,str) and 8<=len(title.strip())<=300 and published and (not founded or published>=founded) and 0<=(today-published).days<=730 and same_site(url,home) and bool(urlsplit(url).path.strip('/')) and not NON_POST.search(urlsplit(url).path) and not re.search(r'hello world|lorem ipsum|casino|viagra|welcome to wordpress',title,re.I)


def feed_posts(profile,response,home,today,founded,record=None):
    if re.search(r'/comments?/(?:feed/?)?(?:[?]|$)|[?&]feed=comments',response.url,re.I):return
    try:root=ET.fromstring(response.text)
    except ET.ParseError:return
    tag=lambda node:node.tag.rsplit('}',1)[-1].lower()
    nodes=[node for node in root.iter() if tag(node) in {'item','entry'}]
    raw_blocks=re.findall(r'<(?:\w+:)?(?:item|entry)\b[^>]*>.*?</(?:\w+:)?(?:item|entry)>',response.text,re.I|re.S)
    for index,item in enumerate(nodes[:10]):
        def child(name):return next((n for n in item if tag(n)==name),None)
        title_node=child('title');date_node=child('pubdate')
        if date_node is None:date_node=child('published')
        # Atom updated and sitemap lastmod are not publication dates.
        link_node=child('link')
        if title_node is None or date_node is None or link_node is None:continue
        if re.search(r'#comment[-\d]|[?&]replytocom=',link_node.get('href') or link_node.text or '',re.I):continue
        title=' '.join(''.join(title_node.itertext()).split())
        url=clean_url(urljoin(response.url,link_node.get('href') or link_node.text or ''))
        published=date_value(date_node.text)
        kind=publication_kind(title,raw_blocks[index],record or {},home,allow_topic=True) if index<len(raw_blocks) else None
        if post_ok(title,published,url,home,today,founded) and eligible(title,published,url,home,today,founded) and kind:
            profile.add('dated_activity',{'title':title,'date':published.isoformat(),'url':url,'publisher_type':'company_published','activity_kind':kind},response,raw_blocks[index],'company_owned',period=published.isoformat(),confidence=.99)


def wordpress_posts(profile,response,home,today,founded,record=None):
    try:data,spans=json_document(response)
    except (ValueError,IndexError,TypeError):return
    if not isinstance(data,list):return
    for i,item in enumerate(data[:6]):
        if not isinstance(item,dict):continue
        raw_title=(item.get('title') or {}).get('rendered') if isinstance(item.get('title'),dict) else None
        title=html.unescape(re.sub(r'<[^>]*>',' ',raw_title or '')).strip()
        url=clean_url(item.get('link'))
        published=date_value(item.get('date'))
        kind=publication_kind(title,raw_title or '',record or {},home,allow_topic=True)
        if post_ok(title,published,url,home,today,founded) and eligible(title,published,url,home,today,founded) and kind:
            profile.add('dated_activity',{'title':title,'date':published.isoformat(),'url':url,'publisher_type':'company_published','activity_kind':kind},response,spans[(i,)],'company_owned',period=published.isoformat())


def socials(profile,page,record):
    from .social_identity import own_handle
    if stated_numbers(page.root.text())-{str(record['organisasjonsnummer'])}:return
    links=[(url,page.span(node)) for url,node in page.links()]
    for node in page.root.all({'div','span','iframe'}):
        for attr in ('data-href','data-src','src'):
            if node.attrs.get(attr):
                url=clean_url(urljoin(page.response.url,html.unescape(node.attrs[attr])))
                if url:links.append((url,page.span(node)))
    bound=set()
    for item,span in page.structured():
        own_url=item.get('url') or item.get('@id')
        if own_url and same_site(str(own_url),page.response.url):
            same_as=item.get('sameAs') or []
            raw_ids=json.dumps([item.get(k) for k in ('taxID','vatID','identifier')])
            ids={''.join(m.groups()) for m in re.finditer(r'(?<!\d)(\d{3})[ .]?(\d{3})[ .]?(\d{3})(?!\d)',raw_ids)}
            ids={org for org in ids if valid_org(org)}
            own_entity=bool(_types(item)&{'Organization','Corporation','LocalBusiness'}) and ids=={str(record['organisasjonsnummer'])}
            for url in same_as if isinstance(same_as,list) else [same_as]:
                if isinstance(url,str):
                    links.append((url,span))
                    found=canonical_social(url)
                    if own_entity and found:bound.add(found[0])
    # Footer URLs may be shipped as script data, without rendering the page.
    for match in re.finditer(r'https?(?::|\\u003[aA])(?:/|\\/){2}[^\s\"<>]+',page.raw):
        raw=match.group().rstrip("',;)")
        url=raw.replace('\\/','/').replace('\\u003a',':').replace('\\u003A',':')
        links.append((html.unescape(url),raw))
    have={c['value'] for c in profile.data['claims'] if c['field']=='company_profile' and c['availability']=='available'}
    def regional(handle):
        return handle.casefold().endswith(('norge','norway','noreg')) or bool(re.search(r'[._-]no$',handle,re.I))
    has_regional=any(regional(found[1]) for url,_ in links if (found:=canonical_social(url)) and own_handle(found[1],record,page.response.url))
    for url,span in links:
        try:found=canonical_social(url)
        except ValueError:continue
        if not found:continue
        canonical,handle=found
        opaque=bool(re.fullmatch(r'\d{5,20}|UC[A-Za-z0-9_-]{22}',handle))
        exact_bound=opaque and canonical in bound
        if has_regional and not regional(handle) and not exact_bound:continue
        if not (own_handle(handle,record,page.response.url) or exact_bound) or canonical in have:continue
        have.add(canonical)
        profile.add('company_profile',canonical,page.response,span,'company_owned',confidence=.98)


def listed_roles(profile,page,record,home=None,linked_from=None):
    from .careers import listed_roles as extract
    parts=urlsplit(page.response.url)
    origin=home or f'{parts.scheme}://{parts.netloc}/'
    extract(profile,page,record,origin,linked_from=linked_from,on_ats=lambda u:career_destination(u,origin))


def career_destination(url,home):
    host=host_of(url);base=host_of(home)
    return on_ats(url) or bool(base and host.endswith('.'+base) and re.match(r'^(?:careers?|karriere|jobs?|jobb)\.',host,re.I))


def crawl_verified_site(profile,client,record,home,*,page_cap=8,owner_page=None):
    today=datetime.now(timezone.utc).date()
    founded=parse_date(record.get('stiftelsesdato'))
    origin=f'{urlsplit(home.response.url).scheme}://{urlsplit(home.response.url).netloc}'
    queue=[];seen={home.response.url};seen_sitemaps=set();used=0
    career_proofs={home.response.url:[]}
    def context(url,node):
        # A careers hostname must not turn every cookie/team link into a job.
        parsed=urlsplit(url)
        label=host_of(url).split('.')[0] if not parsed.path.strip('/') else ''
        return parsed.path+' '+node.text()+' '+label
    def enqueue(url,kind='html',linked_from=None,priority=2):
        url=clean_url(url)
        if not url or url in seen or any(row[1]==url for row in queue):return
        if not same_site(url,home.response.url) and not (kind in {'ats','job_feed'} and linked_from and career_destination(url,home.response.url)):return
        queue.append((priority,url,kind,linked_from));queue.sort(key=lambda row:row[0])
    def job_feeds(page):
        from .ats import feed_links
        for url,span in feed_links(page):
            career_proofs[url]=career_proofs.get(page.response.url,[])+[(page.response,span)]
            enqueue(url,'job_feed',home.response.url,priority=0)
    def visit(page):
        from .news import add_news
        path=urlsplit(page.response.url).path.strip('/')
        if not path or re.fullmatch(r'(?:(?:[a-z]{2}(?:-[a-z]{2})?)/)?(?:home|hjem|forside|index\.html|contact|kontakt|om-oss|about|about-us|impressum)?/?',path,re.I):
            socials(profile,page,record)
        add_article(profile,page,today);add_jobs(profile,page,record,today);listed_roles(profile,page,record,home.response.url)
        add_news(profile,page,today,record)
        job_feeds(page)
        for url,node in page.links():
            label=context(url,node)
            if ACTIVITY.search(label):
                if JOBS.search(label):career_proofs[url]=career_proofs.get(page.response.url,[])+[(page.response,page.span(node))]
                enqueue(url,'html' if same_site(url,home.response.url) else 'ats',page.response.url,0 if JOBS.search(label) else 1)
            elif re.search(r'kontakt|contact|om-oss|about|ledelse|leadership|locations|kontor',label,re.I):
                enqueue(url,priority=3)
        for node in page.root.all({'link'}):
            if node.attrs.get('href') and 'alternate' in str(node.attrs.get('rel')) and re.search('rss|atom',str(node.attrs.get('type')),re.I):
                if re.search('comments?|kommentar',node.attrs['href']+' '+str(node.attrs.get('title') or ''),re.I):continue
                enqueue(urljoin(page.response.url,node.attrs['href']),'feed',priority=0)
    visit(home)
    if owner_page and owner_page.response.url not in seen and same_site(owner_page.response.url,home.response.url):
        seen.add(owner_page.response.url)
        socials(profile,owner_page,record)
        visit(owner_page)
    # Conventional routes are low-priority recovery, after advertised links/feeds.
    for path in ('/kontakt','/om-oss','/karriere','/nyheter'):
        if not any(kind=='html' and ACTIVITY.search(url) for _,url,kind,_ in queue):enqueue(origin+path,priority=5)
    if '/wp-content/' in home.raw or re.search(r'<meta[^>]+WordPress',home.raw,re.I):
        enqueue(origin+'/wp-json/wp/v2/posts?per_page=6&_fields=date,title,link','wordpress',priority=0)
    # Use the robots response already acquired by Client; do not buy it twice.
    robots=client.cache.get((origin+'/robots.txt',False))
    maps=re.findall(r'^\s*Sitemap:\s*(\S+)',robots.text,re.I|re.M) if robots else []
    for sitemap in maps[:2] or [origin+'/sitemap.xml']:
        enqueue(sitemap,'sitemap',priority=4)
    consecutive_failures=0
    while queue and used<page_cap and client.budget.remaining()>0:
        _,url,kind,linked_from=queue.pop(0)
        if url in seen:continue
        seen.add(url);used+=1
        result=client.get(url,retries=0)
        profile.data.setdefault('crawl_attempts',[]).append({'url':url,'kind':kind,'status':result.status,
                  'state':result.state,'linked_from':linked_from,'content_sha256':result.digest,'retrieved_at':result.retrieved_at})
        if result.status!=200:
            if result.status in {0,429} or result.status>=500:consecutive_failures+=1
            else:consecutive_failures=0
            if consecutive_failures>=3:break
            continue
        consecutive_failures=0
        if kind in {'ats','job_feed'}:
            if not same_site(result.url,home.response.url) and not career_destination(result.url,home.response.url):continue
        elif not same_site(result.url,home.response.url):continue
        career_proofs[result.url]=career_proofs.get(url,[])
        if kind=='job_feed':
            from .ats import collect_feed
            collect_feed(profile,result,record,home.response.url,provenance=career_proofs.get(result.url,[]))
        elif kind=='feed':feed_posts(profile,result,home.response.url,today,founded,record)
        elif kind=='wordpress':wordpress_posts(profile,result,home.response.url,today,founded,record)
        elif kind=='sitemap':
            try:root=ET.fromstring(result.text)
            except ET.ParseError:continue
            locations=[node.text for node in root.iter() if node.tag.rsplit('}',1)[-1]=='loc' and node.text]
            index=root.tag.rsplit('}',1)[-1]=='sitemapindex'
            for location in locations:
                if index and location not in seen_sitemaps and len(seen_sitemaps)<2:
                    seen_sitemaps.add(location);enqueue(location,'sitemap',priority=4)
                elif not index and not urlsplit(location).path.lower().endswith('.xml') and ACTIVITY.search(location):enqueue(location,priority=1)
        else:
            page=Page(result)
            if client.browser:
                rendered=client.browser.render_if_needed(page,client)
                if rendered:page=Page(rendered)
            if kind=='ats':
                add_jobs(profile,page,record,today)
                listed_roles(profile,page,record,home.response.url,linked_from=linked_from)
                job_feeds(page)
                for target,node in page.links():
                    if JOBS.search(context(target,node)) and host_of(target)==host_of(page.response.url):
                        career_proofs[target]=career_proofs.get(page.response.url,[])+[(page.response,page.span(node))]
                        enqueue(target,'ats',linked_from,priority=2)
            else:visit(page)
    for family in ('company_profile','hiring_signal','dated_activity'):
        profile.missing(family,reason='No qualifying item in the verified pages, declared feeds, sitemaps and linked job sources checked')
