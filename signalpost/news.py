"""Dated own-site article and listing extraction with local, verbatim spans."""
import re
from urllib.parse import urljoin,urlsplit
from .net import clean_url
from .web import parse_date,same_site,company_words,words
from .net import host_of

BAD_TITLE=re.compile(r'hello world|hei[\s,!]+verden|lorem ipsum|casino|viagra|welcome to wordpress|velkommen til wordpress|^(?:comments? on|kommentar(?:er)? til)\b|^(?:related articles|liknende artikler|news|nyheter|blog|aktuelt)(?:\s*[|:\-].*)?$',re.I)
NON_ARTICLE=re.compile(r'/(?:products?|produkt|contact|kontakt|about|om-oss|privacy|cookies|careers|karriere|jobs?|stillinger|tags?|category|author)(?:/|$)',re.I)
INDEX_PATH=re.compile(r'^/(?:[a-z]{2}/)?(?:news|nyheter|blog|blogg|aktuelt|articles|artikler)/?$',re.I)


def about_company(title,body,record,home):
    """A dated page must concern the entity, not just repeat its site footer."""
    if BAD_TITLE.search(title):return False
    if re.search(r'hello world|elementor\s*#|lorem ipsum|how to|tips (?:for|til)|offentlige frister|oversikt over .*frister|åpen søknad|open application|søker .*leder|ledig stilling',title,re.I):return False
    raw=re.sub(r'<(footer|header|nav|head|title|script|style)\b[^>]*>.*?</\1>',' ',body,flags=re.I|re.S)
    raw=re.split(r'appeared first on|innlegget .* først publisert',raw,flags=re.I)[0]
    plain=re.sub(r'<[^>]+>',' ',raw)
    # Do not count a generic site's name appended to a headline as its subject.
    headline=re.split(r'\s[|›»]\s',title)[0]
    hay=words(headline+' '+plain)
    needles=[company_words(record.get('navn')),words(host_of(home).split('.')[0])]
    for needle in needles:
        if len(''.join(needle))<4:continue
        joined=''.join(needle)
        if any(''.join(hay[i:i+n])==joined for n in range(1,max(4,len(needle))+1) for i in range(len(hay)-n+1)):return True
    return False


def eligible(title,published,url,home,today,founded=None):
    return bool(isinstance(title,str) and 8<=len(title.strip())<=300 and not BAD_TITLE.search(title) and published
                and 0<=(today-published).days<=730 and (not founded or published>=founded)
                and same_site(url,home) and urlsplit(url).path.strip('/') and not NON_ARTICLE.search(urlsplit(url).path)
                and not INDEX_PATH.search(urlsplit(url).path))


def publication_kind(title,body,record,home,*,allow_topic=False):
    if BAD_TITLE.search(title):return None
    if about_company(title,body,record,home):
        if re.search(r'announc|launch|lanser|inngår|acquir|joins|appointed|opens|åpner|etabler|fusjon|nyansatt',title,re.I):return 'company_announcement'
        return 'company_publication'
    if not allow_topic:return None
    if re.search(r'open application|åpen søknad|ledig stilling|hello world|lorem ipsum',title,re.I):return None
    if re.search(r'how to|\b(?:tips|guide|slik|tegn|råd)\b|fuktskader',title,re.I):return 'company_published_advice'
    if re.search(r'produkt|product|model|kompatib|DJI|Matrice',title,re.I):return 'company_published_product_article'
    return 'company_publication'


def add_news(profile,page,today,record):
    founded=parse_date(record.get('stiftelsesdato'))
    def publish(title,published,url,span,method,allow_topic=False):
        kind=publication_kind(title,span,record,page.response.url,allow_topic=allow_topic)
        if eligible(title,published,url,page.response.url,today,founded) and kind:
            profile.add('dated_activity',{'title':title,'date':published.isoformat(),'url':url,'publisher_type':'company_published','activity_kind':kind},
                        page.response,span,'company_owned',period=published.isoformat(),confidence=.97,method=method)
    # Article/listing cards: the title, destination and publication date must
    # belong to the SAME block, avoiding a footer date paired to an unrelated title.
    def content_node(node,block):
        current=node
        while current is not block and current is not None:
            if current.tag in {'footer','header','nav'} or re.search('copyright|footer',str(current.attrs),re.I):return False
            current=current.parent
        return True
    index_page=bool(INDEX_PATH.search(urlsplit(page.response.url).path))
    for block in page.root.all({'article','li','section','div','a'}):
        if block.tag=='a' and not index_page:continue
        raw=page.span(block)
        if not 25<len(raw)<12000:continue
        headings=list(block.all({'h1','h2','h3','h4'}))
        if not headings:
            headings=[n for n in block.all({'span'}) if re.search(r'(?:^|[\s_-])(?:title|headline)(?:$|[\s_-])',n.attrs.get('class',''),re.I)]
        if len(headings)!=1:continue
        heading=headings[0];title=heading.text()
        times=[n for n in block.all({'time'}) if content_node(n,block) and not re.search('modif|updat',str(n.attrs),re.I)]
        published=None
        if len(times)==1:published=parse_date(times[0].attrs.get('datetime') or times[0].text())
        elif not times:
            dates=[n for n in block.all({'span','p','div'}) if content_node(n,block) and re.search(r'publish|posted|date|dato',str(n.attrs),re.I) and not re.search('modif|updat',str(n.attrs),re.I) and len(n.text())<=100]
            parsed=[parse_date(n.text()) for n in dates]
            if len([d for d in parsed if d])==1:published=next(d for d in parsed if d)
            elif not dates and block.tag=='a':
                # A complete linked card can use a plain date label. Require a
                # standalone date, never a date mentioned inside its prose.
                labels=[n.text().strip() for n in block.all({'span','p','div'}) if content_node(n,block) and not any(hasattr(c,'tag') for c in n.children)]
                labels=[s for s in labels if re.fullmatch(r'(?:[A-Za-zæøå]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[./ ]\s*[A-Za-zæøå0-9]+[./ ,]+20\d{2}|20\d{2}-\d{2}-\d{2})',s,re.I)]
                parsed={d for s in labels if (d:=parse_date(s))}
                if len(parsed)==1:published=next(iter(parsed))
        if not published:continue
        targets=[block.attrs['href']] if block.tag=='a' and block.attrs.get('href') else [n.attrs['href'] for n in heading.all({'a'}) if n.attrs.get('href')]
        if not targets:
            parent=heading.parent
            if parent and parent.tag=='a' and parent.attrs.get('href'):targets=[parent.attrs['href']]
        if not targets and block.tag=='article':
            # Stretched-link cards keep their empty anchor beside the title.
            targets=list(dict.fromkeys(n.attrs['href'] for n in block.all({'a'}) if n.attrs.get('href')))
        url=clean_url(urljoin(page.response.url,targets[0])) if len(targets)==1 else page.response.url if heading.tag=='h1' else ''
        if url:
            topical=bool(INDEX_PATH.search(urlsplit(page.response.url).path)) and url!=page.response.url
            publish(title,published,url,raw,'dom_local_article_date',allow_topic=topical)
    # A detail page may put its publication date in OpenGraph outside <article>.
    headings=list(page.root.all({'h1'}))
    is_static=bool(re.search(r'<body[^>]+class=[\"\'][^\"\']*\bpage-id-|"@type"\s*:\s*"(?:AboutPage|ContactPage|FAQPage)"',page.raw,re.I))
    is_article=bool(list(page.root.all({'article'}))) or bool(re.search(r'(?:property|name)=[\"\']og:type[\"\'][^>]*content=[\"\']article',page.raw,re.I))
    if len(headings)==1 and is_article and not is_static:
        for meta in page.root.all({'meta'}):
            key=(meta.attrs.get('property') or meta.attrs.get('name') or '').lower()
            if key not in {'article:published_time','datepublished','date','pubdate'}:continue
            published=parse_date(meta.attrs.get('content'))
            # One span captures both headline and metadata, preserving their context.
            body=re.sub(r'<(footer|header|nav|script|style)\b[^>]*>.*?</\1>',' ',page.raw,flags=re.I|re.S)
            topical=len(re.sub('<[^>]+>',' ',body).strip())>=180
            publish(headings[0].text(),published,page.response.url,page.raw,'dom_article_published_meta',allow_topic=topical)
