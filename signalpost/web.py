"""Conservative company-site identity, dated news, social profiles and real jobs."""
from __future__ import annotations
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from .net import clean_url, host_of
from .registry import json_document

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
IGNORED = {"script", "style", "noscript", "svg", "template"}
DIRECTORY_HOSTS = {"proff.no", "1881.no", "gulesider.no", "purehelp.no", "brreg.no", "facebook.com", "linkedin.com", "instagram.com", "youtube.com", "northdata.de", "northdata.com", "norgelei.no", "utdanning.no", "largestcompanies.com", "dnb.com", "creditsafe.com", "virksomhet.brreg.no", "yelp.com"}
PARKED = re.compile(r"domain (?:is )?for sale|buy this domain|hugedomains|sedo domain|domenet er (?:til salgs|parkert)|parked domain|website coming soon|^\s*coming soon\b|future home of something|under construction|default web site page|welcome to nginx", re.I)
LEGAL = {"as", "asa", "ans", "da", "sa", "enk", "nuf", "ks", "ba", "bbl", "brl", "iks", "sf"}


def directory_url(url):
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS) or bool(re.search(r"/(?:bedrift|detaljert-informasjon|solskjermingsleverandorer|company-directory)/|(?<!\d)\d{9}(?!\d)", urlsplit(url).path, re.I))


def words(value):
    text = str(value or "").casefold().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", text)


def company_words(name):
    tokens = words(name)
    while tokens and tokens[-1] in LEGAL:
        tokens.pop()
    return tokens


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    start: int = 0
    end: int = 0
    parent: object = None

    def text(self):
        if self.tag in IGNORED:
            return ""
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def all(self, tags=None):
        if tags is None or self.tag in tags:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.all(tags)


class Page(HTMLParser):
    def __init__(self, response):
        super().__init__(convert_charrefs=True)
        self.response = response
        self.raw = response.text
        self.lines = [0]
        for match in re.finditer("\n", self.raw):
            self.lines.append(match.end())
        self.root = Node("document", end=len(self.raw))
        self.stack = [self.root]
        self.count = 0
        self.feed(self.raw)
        for node in self.stack[1:]:
            node.end = len(self.raw)

    def source_offset(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 30000:
            raise ValueError("HTML node limit exceeded")
        # Recover common omitted closing tags without building a huge nested list.
        if tag in {"li", "p", "article"} and self.stack[-1].tag == tag:
            self.stack[-1].end = self.source_offset()
            self.stack.pop()
        node = Node(tag, dict(attrs), start=self.source_offset(), end=self.source_offset() + len(self.get_starttag_text()), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                end = self.raw.find(">", self.source_offset()) + 1
                for node in self.stack[i:]:
                    node.end = end
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def span(self, node):
        return self.raw[node.start:node.end]

    def links(self):
        for node in self.root.all({"a"}):
            if node.attrs.get("href"):
                try:url = clean_url(urljoin(self.response.url, html.unescape(node.attrs["href"])))
                except ValueError:continue
                if url:
                    yield url, node

    def structured(self):
        from .content import structured
        yield from structured(self)


def same_site(a, b):
    return bool(host_of(a)) and host_of(a) == host_of(b)


def _has_name(text, record):
    if any(_has_name(text,{'navn':name}) for name in record.get('_operating_names',[]) if isinstance(name,str) and company_words(name)):
        return True
    wanted = company_words(record.get("navn"))
    got = words(text)
    # Legal names and site brands often differ only in word boundaries:
    # "74 ENTERTAINMENT" vs "74Entertainment". Require the WHOLE core name.
    key = ''.join(wanted)
    return bool(key) and any(''.join(got[i:i+n]) == key for n in range(1,len(wanted)+1) for i in range(len(got)-n+1))


def ownership(page, record):
    visible = page.root.text()
    if PARKED.search(visible):
        return None
    org = str(record["organisasjonsnummer"])
    # Many Norwegian sites print the legal identifier in an ordinary contact
    # paragraph, not a semantic <footer>. Require an own-name context as well.
    all_ids=stated_numbers(visible)
    owner_name=name_claim(page,record)
    if owner_name and all_ids=={org}:
        small=[node for node in page.root.all({'p','small','address','li','span'})
               if len(node.text())<=1500 and org in owner_numbers(node.text())]
        if small:
            node=min(small,key=lambda n:len(page.span(n)))
            return {'method':'labelled_legal_identifier_and_owner_name','span':page.span(node),
                    'corroborating_spans':[owner_name],'confidence':1.0}
    for node in page.root.all({"footer", "address", "section", "div"}):
        if node.tag not in {"footer", "address"} and not re.search(r"footer|contact|kontakt|legal|impressum", str(node.attrs), re.I):
            continue
        text = node.text()
        if len(text) > 4000:
            continue
        identifiers = owner_numbers(text,allow_bare=_has_name(text,record))
        if org in identifiers and len(identifiers) == 1:
            return {"method": "exact_owner_org_number", "span": page.span(node), "confidence": 1.0}
    for item, span in page.structured():
        types = item.get("@type")
        types = types if isinstance(types, list) else [types]
        from .content import is_organisation
        if not is_organisation(item):
            continue
        ident = json.dumps([item.get(k) for k in ("taxID", "vatID", "identifier")], ensure_ascii=False)
        digits = {re.sub(r"\D", "", m.group()) for m in re.finditer(r"(?<!\d)\d{3}[ .]?\d{3}[ .]?\d{3}(?!\d)", ident)}
        if org in digits:
            own_url = item.get("url") or item.get("@id")
            if not own_url or not same_site(page.response.url, str(own_url)):
                continue
            if digits!={org} or re.search(r'customer|client|partner|supplier|review|author',str(item.get('@id') or ''),re.I):continue
            return {"method": "structured_owner_exact_org_number", "span": span, "confidence": 1.0}
    # Weak geography/name matches are leads only. Full address corroboration
    # requires both street and postcode, and a matching name-derived domain.
    address = record.get("forretningsadresse") or {}
    street = address.get("adresse") or []
    street = street[0] if isinstance(street, list) and street else street
    postal = str(address.get("postnummer") or "")
    domain = host_of(page.response.url).split(".")[0].replace("-", "")
    name = "".join(company_words(record.get("navn")))
    if street and postal and domain == name and len(name) >= 5:
        for node in page.root.all({"footer", "address", "section", "div"}):
            text = node.text()
            if len(text) <= 3000 and _has_name(text, record) and postal in text and " ".join(words(street)) in " ".join(words(text)):
                return {"method": "whole_name_domain_and_exact_street_postcode", "span": page.span(node), "confidence": .97}
    return None


def valid_org(number):
    if not re.fullmatch(r"[89]\d{8}", number):
        return False
    check = (11 - sum(int(d) * w for d, w in zip(number[:8], (3, 2, 7, 6, 5, 4, 3, 2))) % 11) % 11
    return check < 10 and check == int(number[-1])


def stated_numbers(text):
    pattern = r"\borg(?:anisasjons)?\.?\s*-?\s*(?:nr|nummer|no)?\.?\s*:?\s*(?:NO\s*)?(\d{3})[\s.\u00a0]?(\d{3})[\s.\u00a0]?(\d{3})(?!\d)"
    numbers={''.join(m.groups()) for m in re.finditer(pattern, text, re.I) if valid_org(''.join(m.groups()))}
    vat=r'\bNO\s*(\d{3})[\s.]?(\d{3})[\s.]?(\d{3})\s*MVA\b'
    numbers|={''.join(m.groups()) for m in re.finditer(vat,text,re.I) if valid_org(''.join(m.groups()))}
    return numbers


def owner_numbers(text,allow_bare=False):
    labelled=stated_numbers(text);result=set()
    for match in re.finditer(r'(?<!\d)(\d{3})[\s.\u00a0]?(\d{3})[\s.\u00a0]?(\d{3})(?!\d)',text):
        org=''.join(match.groups())
        if not valid_org(org) or org not in labelled and not allow_bare:continue
        before=re.split(r'[|•·©\n]',text[max(0,match.start()-100):match.start()])[-1]
        if re.search(r'utviklet av|designet av|website by|webdesign by|levert av|(?:kunde|customer|klient|supplier|leverandør|revisor|auditor|partner)\s*:',before,re.I):continue
        result.add(org)
    return result


def name_claim(page, record):
    """Owner name on title/heading/metadata/footer, rather than a customer mention."""
    for node in page.root.all({'title', 'h1', 'footer', 'address', 'meta'}):
        value = node.attrs.get('content', '') if node.tag == 'meta' and (node.attrs.get('property') or node.attrs.get('name') or '').lower() in {'og:site_name', 'application-name'} else node.text()
        if value and len(value) <= 3000 and _has_name(value, record):
            return page.span(node)
    for item, span in page.structured():
        from .content import is_organisation
        if is_organisation(item):
            own_url = item.get('url') or item.get('@id')
            if own_url and same_site(str(own_url), page.response.url) and _has_name(item.get('legalName') or item.get('name'), record):
                return span
    return None


def holding_conflict(page, record):
    strong=ownership(page,record)
    if strong and strong['confidence']==1:return False
    visible=page.root.text()
    holding={'holding','holdings','invest','investering','investment','investments','eiendom','eiendommer','property','properties','capital','kapital','finans','forvaltning','management'}
    target=company_words(record.get('navn'))
    if holding.intersection(target) and target and not _has_name(visible,{'navn':record.get('navn')}):
        # Shared offices/phones do not transfer an operating company's site to
        # its holding/property company. Exact own-org evidence returned above.
        plain=' '.join(words(re.sub(r'\ba\s*/\s*s\b','as',visible,flags=re.I)))
        pattern=r'\b'+re.escape(target[0])+r'(?:\s+[a-z0-9]+){0,3}?\s+(?:as|asa)\b'
        if any(company_words(m.group())!=target for m in re.finditer(pattern,plain)):
            return True
    return False


def owner_and_registered_person(page,record):
    """A complete current owner name plus an independently registered leader.

    Identity must be stated by the page, never inferred from a domain label or
    customer mention. Raw page spans and official role evidence stay separate.
    """
    core=company_words(record.get('navn'));key=''.join(core)
    if len(key)<5 or re.search(r'borettslag|sameie|velforening',str(record.get('navn')),re.I):return None
    legal=words(record.get('navn'));form=legal[-1] if legal and legal[-1] in LEGAL else None
    needs_form=record.get('erIKonsern') or bool(set(core)&{'holding','holdings','invest','eiendom','property','capital','management'})
    def own_segment(text):
        for segment in re.split(r'[|•·]|\s[-–—:]\s',text):
            segment=re.sub(r'^\s*(?:©|copyright)?\s*(?:20\d{2})?\s*','',segment,flags=re.I)
            got=words(segment)
            for count in range(1,min(len(got),len(core))+1):
                if ''.join(got[:count])!=key:continue
                tail=got[count:]
                if not tail:return not needs_form
                if tail[0] in LEGAL:return bool(form and tail[0]==form)
        return False
    owner=None
    for node in page.root.all({'title','h1','footer','address','meta'}):
        text=node.text()
        if node.tag=='meta':
            if (node.attrs.get('property') or node.attrs.get('name')) not in {'og:site_name','application-name'}:continue
            text=node.attrs.get('content','')
        if node.tag=='footer' and '©' in text:text=text.rsplit('©',1)[-1]
        if len(text)<=2500 and own_segment(text):owner=page.span(node);break
    if not owner:return None
    # A different explicit copyright owner overrides a title/name/person match.
    for footer in page.root.all({'footer'}):
        text=footer.text()
        if '©' not in text:continue
        copyright=re.split(r'[|•·]',text.rsplit('©',1)[-1])[0]
        if re.search(r'\b(?:AS|ASA|ANS|DA|NUF|IKS)\b',copyright) and not own_segment(copyright):return None
    for person in record.get('_identity_people',[]):
        wanted=words(person['name'])
        if len(wanted)<2 or len(''.join(wanted))<8:continue
        for node in sorted(page.root.all({'footer','address','p','div','section','li','span'}),key=lambda n:len(page.span(n))):
            text=node.text()
            if not text or len(text)>2500 or re.search(r'hosting|utviklet av|webdesign|kundeomtale|testimonial',text,re.I):continue
            current=node;allowed=True
            while current:
                if current.tag in {'article','blockquote','nav'} or re.search(r'testimonial|customer|client|partner|reference',str(current.attrs),re.I):allowed=False;break
                current=current.parent
            if not allowed:continue
            got=words(text)
            if any(got[i:i+len(wanted)]==wanted for i in range(len(got)-len(wanted)+1)):
                return {'method':'whole_current_owner_name_and_registered_leader','span':owner,
                        'corroborating_spans':[page.span(node)],'registry_evidence_ids':person['evidence_ids'],'confidence':.97}
    return None


def verify_site(page, record, method, client, requested):
    """Different candidate origins need different corroboration, with the same vetoes."""
    strong = ownership(page, record)
    if strong and strong['confidence'] == 1:
        return strong
    visible = page.root.text()
    if holding_conflict(page,record):return None
    if PARKED.search(visible) or directory_url(page.response.url):
        return None
    other_ids = stated_numbers(visible) - {str(record['organisasjonsnummer'])}
    if other_ids:
        return None
    if (record.get('_shared_contacts') or {}).get('website_hosts',{}).get(host_of(page.response.url),0)>=5:
        return None  # Shared registry domains need the exact-org proof above.
    name_span = name_claim(page, record)
    bridge=record.get('_live_identity_bridges',{}).get(host_of(page.response.url))
    if bridge:
        address=record.get('forretningsadresse') or {}
        street=' '.join(str(v) for v in address.get('adresse',[]))
        postcode=str(address.get('postnummer') or '')
        if street and postcode:
            for node in page.root.all({'address','footer','p','div','section'}):
                text=node.text()
                if len(text)<=2500 and postcode in text and ' '.join(words(street)) in ' '.join(words(text)):
                    return {'method':'live_exact_org_graph_and_registered_contact','span':page.span(node),
                            'confidence':.98,'extra_evidence':[bridge]}
    # Registry-listed URL + the COMPLETE legal name in owner context is stronger
    # than a broad registry search (which ORs common words and may have 50 pages).
    # Retain legal form here: COMPANY AS must not match COMPANY ASA.
    if method=='registered':
        legal=words(record.get('navn'))
        for node in page.root.all({'title','h1','footer','address','p'}):
            text=node.text();got=words(text)
            if not legal or len(text)>1200 or len(got)>100:continue
            if any(got[i:i+len(legal)]==legal for i in range(len(got)-len(legal)+1)):
                return {'method':'registry_website_and_full_legal_owner_name','span':page.span(node),'confidence':.99}
    if method == 'registered' and name_span and not namesake(record,client):
        return {'method':'registry_website_and_owner_name','span':name_span,'confidence':.98}
    person_proof=owner_and_registered_person(page,record)
    if person_proof and not namesake(record,client) and not held_by_other(page.response.url,record,client):
        if record.get('_name_clearance'):person_proof['extra_evidence']=[record['_name_clearance']]
        return person_proof
    if strong:
        if not namesake(record, client):
            return strong
        return None
    # An exact legal-owner statement on a domain spelling the whole name is
    # distinct from a partial brand/title match. Clear namesakes and domain
    # ownership live, and retain that official clearance as evidence.
    stem=''.join(company_words(record.get('navn')))
    label=host_of(page.response.url).split('.')[0].replace('-','')
    legal=words(record.get('navn'))
    owner_nodes=[]
    if len(stem)>=8 and stem==label and legal and legal[-1] in LEGAL and len(visible)>=150:
        for node in page.root.all({'title','h1','footer','address'}):
            got=words(node.text())
            if len(node.text())<=1200 and any(got[i:i+len(legal)]==legal for i in range(len(got)-len(legal)+1)):
                owner_nodes.append(node)
        if owner_nodes and not namesake(record,client) and not held_by_other(page.response.url,record,client):
            clearance=record.get('_name_clearance')
            if clearance:
                node=min(owner_nodes,key=lambda n:len(page.span(n)))
                return {'method':'whole_name_domain_full_legal_owner_and_live_unique_register_name',
                        'span':page.span(node),'confidence':.97,'extra_evidence':[clearance]}
    domain_label=host_of(page.response.url).split('.')[0].replace('-','')
    distinctive=[t for t in company_words(record.get('navn')) if len(t)>=4 and t not in {'norge','norway','holding','invest','eiendom','group','gruppen','service','drift','utvikling','hotel','hotell','studio'}]
    brand_match=any(t in domain_label for t in distinctive)
    if not name_span and not brand_match:
        return None
    # A registered phone plus owner-name evidence can corroborate a brand domain.
    phones = {re.sub(r'\D','',str(record.get(k) or ''))[-8:] for k in ('telefon','mobil')}
    phones.discard('')
    shared=(record.get('_shared_contacts') or {}).get('phones',{})
    phones={p for p in phones if shared.get(p,0)<5}
    for node in page.root.all({'footer','address','p','div','section'}):
        text = node.text()
        if len(text) > 2500:
            continue
        numbers = {re.sub(r'\D','',m.group(1)) for m in re.finditer(r'(?<!\d)(?:\+?47[ .-]?)?((?:\d[ .-]?){7}\d)(?!\d)',text)}
        if phones & numbers and not namesake(record, client) and not held_by_other(page.response.url, record, client):
            return {'method':'owner_name_or_brand_and_registered_phone','span':page.span(node),'corroborating_spans':[name_span] if name_span else [],'confidence':.97}
    # Whole-name guesses may use name+registered town only after a namesake check.
    stem = ''.join(company_words(record.get('navn')))
    asked = host_of(requested).split('.')[0].replace('-', '')
    landed = host_of(page.response.url).split('.')[0].replace('-', '')
    address = record.get('forretningsadresse') or {}
    towns = [str(address.get(k) or '') for k in ('poststed','kommune')]
    town = next((t for t in towns if len(t)>=3 and re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)',visible,re.I)),None)
    if name_span and len(stem)>=6 and stem==asked==landed and town and not namesake(record,client) and not held_by_other(page.response.url,record,client):
        return {'method':'whole_name_domain_owner_name_and_registered_town','span':name_span,'confidence':.95}
    if brand_match or method in {'email_domain_lead','nav_employer','public_seed','public_search_lead'} or method.endswith('_lead_requires_owner_proof'):
        address_text = ' '.join(str(x) for x in address.get('adresse',[])) if isinstance(address.get('adresse'),list) else str(address.get('adresse') or '')
        postcode = str(address.get('postnummer') or '')
        if address_text and postcode and postcode in visible and ' '.join(words(address_text)) in ' '.join(words(visible)) and not namesake(record,client) and not held_by_other(page.response.url,record,client):
            for node in sorted(page.root.all({'p','address','footer','div','section'}),key=lambda n:len(page.span(n))):
                text=node.text()
                if postcode in text and ' '.join(words(address_text)) in ' '.join(words(text)):
                    return {'method':'owner_name_or_brand_and_registered_address','span':page.span(node),'corroborating_spans':[name_span] if name_span else [],'confidence':.97}
    return None


def namesake(record, client):
    from urllib.parse import urlencode
    # Brreg ORs words in a multiword query. A distinctive token retrieves a
    # much smaller superset of possible EXACT names; scan it completely.
    generic={'norsk','norge','norway','holding','invest','eiendom','utvikling','studio','hotel','hotell',
             'regnskap','tannlegesenter','processing','natur','service','bygg','group','gruppen','pizza'}
    original=re.findall(r'[^\W_]+',str(record.get('navn') or '').casefold(),re.U)
    tokens=[t for t in original if t not in LEGAL and len(t)>=3]
    ranked=sorted(tokens,key=lambda t:(t in generic,-len(t),t))[:2]
    target=company_words(record.get('navn'))
    record.pop('_name_clearance',None)
    def cleared(response,body):
        rows=(body.get('_embedded') or {}).get('enheter',[])
        if (body.get('page') or {}).get('totalPages',1)==1 and any(str(row.get('organisasjonsnummer'))==str(record['organisasjonsnummer']) and company_words(row.get('navn'))==target for row in rows):
            record['_name_clearance']={'response':response,'span':response.text,'source_class':'official_registry'}
        return False
    for token in ranked:
        completed=False
        for page_number in range(3):
            params={'navn':token,'size':100,'page':page_number}
            response=client.get('https://data.brreg.no/enhetsregisteret/api/enheter?'+urlencode(params),robots=False,retries=0)
            if response.status!=200:break
            try:
                body=response.json();pages=(body.get('page') or {}).get('totalPages',1)
                if not isinstance(body.get('page'),dict):break
                if any(company_words(row.get('navn'))==target and str(row.get('organisasjonsnummer'))!=str(record['organisasjonsnummer']) for row in (body.get('_embedded') or {}).get('enheter',[])):return True
                if page_number+1>=pages:completed=True;break
                if pages>3:break
            except (ValueError,TypeError,AttributeError):break
        if completed:return cleared(response,body)
    # A consecutive full-name query avoids broad OR/token result sets while
    # still including every legal form. Only a complete response clears it.
    phrase=original[:]
    while phrase and phrase[-1] in LEGAL:phrase.pop()
    if len(phrase)>1:
        full_query=re.sub(r'\s+(?:'+ '|'.join(sorted(LEGAL,key=len,reverse=True)) +r')\s*$','',str(record.get('navn') or ''),flags=re.I).strip()
        for page_number in range(3):
            params={'navn':full_query,'navnMetodeForSoek':'FORTLOEPENDE','size':100,'page':page_number}
            response=client.get('https://data.brreg.no/enhetsregisteret/api/enheter?'+urlencode(params),robots=False,retries=0)
            if response.status!=200:break
            try:
                body=response.json();paging=body.get('page')
                if not isinstance(paging,dict):break
                pages=paging.get('totalPages',1)
                if any(company_words(row.get('navn'))==target and str(row.get('organisasjonsnummer'))!=str(record['organisasjonsnummer']) for row in (body.get('_embedded') or {}).get('enheter',[])):return True
                if page_number+1>=pages:return cleared(response,body)
                if pages>3:break
            except (ValueError,TypeError,AttributeError):break
    return True


def held_by_other(url, record, client):
    from urllib.parse import urlencode
    host = host_of(url)
    label = host.split('.')[0]
    response = client.get('https://data.brreg.no/enhetsregisteret/api/enheter?' + urlencode({'navn':label,'size':100}),robots=False,retries=0)
    if response.status != 200:
        return True
    try:
        body=response.json()
        if (body.get('page') or {}).get('totalPages',1)>1:return True
        for row in (body.get('_embedded') or {}).get('enheter',[]):
            other = clean_url(row.get('hjemmeside'))
            other_name = ''.join(company_words(row.get('navn')))
            if str(row.get('organisasjonsnummer'))!=str(record['organisasjonsnummer']) and host_of(other)==host and label.replace('-','')==other_name:
                return True
        return False
    except (ValueError,TypeError,AttributeError):
        return True


def candidates(record):
    from .site_discovery import name_urls
    result = []
    website = clean_url(record.get("hjemmeside"))
    if website:
        result.append((website, "registered"))
    result.extend((clean_url(u),'nav_employer') for u in record.get('_nav_homepages',[]) if clean_url(u))
    result.extend((clean_url(u),'subunit_lead_requires_owner_proof') for u in record.get('_subunit_website_leads',[]) if clean_url(u))
    name = str(record.get("navn") or "")
    if not re.search(r"borettslag|sameiet|boligsameie|bedriftsidrett|\bb\.i\.l\b", name, re.I):
        email = str(record.get("epostadresse") or "")
        if "@" in email:
            domain = email.rsplit("@", 1)[1].lower()
            if domain.split('.')[0] in {'mail','post','smtp','epost','mx'} and len(domain.split('.'))>2:
                domain = domain.split('.',1)[1]
            if (record.get('_shared_contacts') or {}).get('email_domains',{}).get(domain,0)<5 and domain not in {"gmail.com", "outlook.com", "hotmail.com", "hotmail.no", "online.no", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "msn.com", "live.no", "live.com", "start.no", "broadpark.no", "telenor.no"}:
                result.append(("https://" + domain + "/", "email_domain_lead"))
        names = [(name,'name_domain_lead')] + [(old,'former_name_domain_lead:'+old) for old in former_names(record)]
        names.extend((n,'operating_name_domain_lead') for n in record.get('_operating_names',[]) if isinstance(n,str) and company_words(n)!=company_words(name))
        for source_name, method in names:
            result.extend((url,method) for url in name_urls(source_name))
    # NAV and registered-email leads precede optional cached leads; retain the
    # map before speculative name guesses so useful unrelated brands survive.
    position=next((i for i,(_,m) in enumerate(result) if m not in {'registered','nav_employer','email_domain_lead','subunit_lead_requires_owner_proof'}),len(result))
    result[position:position]=[(clean_url(u),'public_seed') for u in record.get('_website_seeds',[]) if clean_url(u)]
    seen = set()
    for url, method in result:
        key=host_of(url)
        if key not in seen and not directory_url(url):
            seen.add(key)
            yield url, method


def former_names(record):
    entries = [e for e in record.get('historiskeNavn',[]) if isinstance(e,dict) and isinstance(e.get('navn'),str) and company_words(e['navn'])!=company_words(record.get('navn'))]
    entries.sort(key=lambda e:str(e.get('tilDato') or ''),reverse=True)
    return list(dict.fromkeys(e['navn'] for e in entries))[:1]


def _types(item):
    from .content import type_names
    return type_names(item)


def parse_date(value):
    value = str(value or "").strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    numeric=re.search(r'(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d{2})(?!\d)',value)
    months={'januar':1,'january':1,'jan':1,'februar':2,'february':2,'feb':2,'mars':3,'march':3,'mar':3,
            'april':4,'apr':4,'mai':5,'may':5,'juni':6,'june':6,'jun':6,'juli':7,'july':7,'jul':7,
            'august':8,'aug':8,'september':9,'sep':9,'sept':9,'oktober':10,'october':10,'okt':10,'oct':10,
            'november':11,'nov':11,'desember':12,'december':12,'des':12,'dec':12}
    written=re.search(r'(?<!\d)(\d{1,2})\.?\s+([a-zæøå]+)\.?\s+(20\d{2})(?!\d)',value,re.I)
    month_first=re.search(r'\b([a-z]+)\s+(\d{1,2}),?\s+(20\d{2})(?!\d)',value,re.I)
    try:
        if numeric:return date(int(numeric[3]),int(numeric[2]),int(numeric[1]))
        if written and written[2].lower() in months:return date(int(written[3]),months[written[2].lower()],int(written[1]))
        if month_first and month_first[1].lower() in months:return date(int(month_first[3]),months[month_first[1].lower()],int(month_first[2]))
    except ValueError:pass
    return None


def add_article(profile, page, today):
    from .news import publication_kind,eligible
    if re.search(r'<body[^>]+class=[\"\'][^\"\']*\bpage-id-|"@type"\s*:\s*"(?:AboutPage|ContactPage|FAQPage)"',page.raw,re.I):return False
    name=next((c['value'] for c in profile.data['claims'] if c['field']=='legal_name' and c['availability']=='available'),None)
    record={'navn':name or ''}
    founded = next((parse_date(c["value"]) for c in profile.data["claims"] if c["field"] == "founded_date" and c["availability"] == "available"), None)
    for item, span in page.structured():
        if not _types(item) & {"NewsArticle", "Article", "BlogPosting"}:
            continue
        title = item.get("headline") or item.get("name")
        published = parse_date(item.get("datePublished"))
        described = item.get("url") or item.get("mainEntityOfPage")
        if isinstance(described, dict):
            described = described.get("@id") or described.get("url")
        # A home/list page's nested article schema is not the article itself.
        if not described or clean_url(urljoin(page.response.url, str(described))).rstrip("/") != page.response.url.rstrip("/"):
            continue
        if not title or not published or not (0 <= (today - published).days <= 730):
            continue
        if founded and published < founded:
            continue
        if re.search(r"hello world|lorem ipsum|velkommen til wordpress|casino|viagra", str(title), re.I):
            continue
        body=re.sub(r'<(footer|header|nav|head|title|script|style)\b[^>]*>.*?</\1>',' ',page.raw,flags=re.I|re.S)
        kind=publication_kind(str(title),page.raw,record,page.response.url,allow_topic=len(re.sub('<[^>]+>',' ',body).strip())>=180)
        if not kind or not eligible(str(title),published,page.response.url,page.response.url,today,founded):continue
        profile.add("dated_activity", {"title": str(title), "date": published.isoformat(), "url": page.response.url, "publisher_type": "company_published",'activity_kind':kind}, page.response, span, "company_owned", period=published.isoformat(), confidence=.99)
        return True
    # HTML article header + its own <time> on an article URL; no homepage dates.
    if urlsplit(page.response.url).path.strip("/"):
        for article in page.root.all({"article"}):
            headings = list(article.all({"h1"}))
            times = list(article.all({"time"}))
            if len(headings) != 1 or len(times) != 1:
                continue
            if "modified" in str(times[0].attrs).casefold() or "updated" in str(times[0].attrs).casefold():
                continue
            published = parse_date(times[0].attrs.get("datetime"))
            if published and 0 <= (today - published).days <= 730 and (not founded or published >= founded):
                raw=page.span(article);body=re.sub(r'<(footer|header|nav|script|style)\b[^>]*>.*?</\1>',' ',raw,flags=re.I|re.S)
                kind=publication_kind(headings[0].text(),raw,record,page.response.url,allow_topic=len(re.sub('<[^>]+>',' ',body).strip())>=180)
                if not kind or not eligible(headings[0].text(),published,page.response.url,page.response.url,today,founded):continue
                profile.add("dated_activity", {"title": headings[0].text()[:250], "date": published.isoformat(), "url": page.response.url, "publisher_type": "company_published",'activity_kind':kind}, page.response, raw, "company_owned", period=published.isoformat(), confidence=.97)
                return True
    return False


def add_jobs(profile, page, record, today):
    count = 0
    for item, span in page.structured():
        if "JobPosting" not in _types(item):
            continue
        employer = item.get("hiringOrganization") or {}
        if not isinstance(employer, dict):
            continue
        identifiers = json.dumps([employer.get(k) for k in ("identifier", "taxID", "vatID")], ensure_ascii=False)
        orgs = {re.sub(r"\D", "", m.group()) for m in re.finditer(r"(?<!\d)\d{3}[ .]?\d{3}[ .]?\d{3}(?!\d)", identifiers)}
        exact_number=str(record['organisasjonsnummer']) in orgs
        exact_name=bool(employer.get('name')) and words(employer.get('name'))==words(record.get('navn'))
        if not exact_number and (orgs or not exact_name):
            continue
        posted = parse_date(item.get("datePosted"))
        expires = parse_date(item.get("validThrough"))
        title = item.get("title")
        url = clean_url(urljoin(page.response.url, str(item.get("url") or page.response.url)))
        if not title or not posted or not expires or not posted <= today <= expires or (today - posted).days > 120:
            continue
        if not same_site(url, page.response.url):
            continue
        employer_proof={'employer_organisation_number':profile.org} if exact_number else {'employer_name':employer['name'],'identity_basis':'exact legal employer name on verified linked source'}
        from .careers import is_recruiter,advertised_role
        if is_recruiter(record):
            advertised_role(profile,record,title,url,page.response,span,posted=posted,expires=expires,
                            source_class='company_owned',method='structured_recruiter_advertisement')
        else:
            profile.add("hiring_signal", {"title": title, "date": posted.isoformat(), "valid_through": expires.isoformat(), "url": url, **employer_proof}, page.response, span, "company_owned", period=posted.isoformat())
        count += 1
    return count


def canonical_social(url):
    try:p = urlsplit(url)
    except (ValueError,TypeError):return None
    host = host_of(url)
    for platform in ('facebook.com','instagram.com','linkedin.com','youtube.com','tiktok.com','twitter.com','x.com'):
        if host.endswith('.'+platform):host=platform;break
    segments = [unquote(s) for s in p.path.split("/") if s]
    if not segments:
        return None
    reserved = {"share", "sharer.php", "intent", "p", "reel", "reels", "stories", "watch", "posts", "videos", "status", "login", "accounts", "search", "hashtag", "groups", "events", "privacy", "policies", "help"}
    if segments[0].lower() in reserved:
        return None
    if host.endswith(".linkedin.com"):
        host = "linkedin.com"
    if host=='twitter.com':host='x.com'
    if host == "linkedin.com" and len(segments) == 2 and segments[0] in {"company", "school"}:
        return "https://www.linkedin.com/" + "/".join(segments), segments[1]
    if host == 'facebook.com' and len(segments)==3 and segments[0]=='people' and re.fullmatch(r'\d{8,20}',segments[2]):
        return 'https://facebook.com/'+'/'.join(segments), segments[1]
    if host=='facebook.com' and len(segments)==3 and segments[0]=='pages' and segments[2].isdigit():
        return 'https://facebook.com/'+'/'.join(segments),segments[1]
    if host=='facebook.com' and len(segments)>=2 and segments[0]=='pg':
        return 'https://facebook.com/'+segments[1],segments[1]
    if host=='tiktok.com' and len(segments)==1 and segments[0].startswith('@'):
        return 'https://www.tiktok.com/'+segments[0],segments[0]
    if host in {"facebook.com", "instagram.com", "x.com", "twitter.com"} and len(segments) == 1:
        return "https://" + host + "/" + segments[0], segments[0]
    if host == "youtube.com" and ((len(segments) == 1 and segments[0].startswith("@")) or len(segments) == 2 and segments[0] in {"channel", "c", "user"}):
        return "https://www.youtube.com/" + "/".join(segments), segments[-1]
    return None


def collect_site(profile, client, record, *, candidate_cap=3, page_cap=8, search=None):
    failures = []
    home, proof, proof_page = None, None, None
    tried = set()
    http_candidates = 0
    diagnostics = profile.data.setdefault('website_discovery',[])
    record['_leaders']=[c['value']['name'] for c in profile.data['claims'] if c['field']=='leader' and c['availability']=='available' and c['value'].get('role_code') in {'DAGL','LEDE'}]
    record['_identity_people']=[{'name':c['value']['name'],'evidence_ids':c['evidence_ids']} for c in profile.data['claims']
                                if c['field']=='leader' and c['availability']=='available' and c['value'].get('role_code') in {'DAGL','LEDE'} and not c.get('stale')]
    def leads():
        for candidate in candidates(record):
            if http_candidates >= candidate_cap:
                break
            yield candidate
        if search:
            yield from search.website_candidates(record, client)
    for url, method in leads():
        if host_of(url) in tried:
            continue
        tried.add(host_of(url))
        proof_record = record
        if method.startswith(('former_name_domain_lead:','former_name_search:')):
            proof_record = {**record,'navn':method.split(':',1)[1]}
            if namesake(proof_record,client):
                diagnostics.append({'url':url,'origin':'former_name','outcome':'withheld','reason':'Former name is held by another entity or could not be checked'})
                continue
        before = client.budget.used
        from .site_discovery import fetch_candidate
        response,fetches=fetch_candidate(client,url)
        if client.budget.used > before:
            http_candidates += 1
        for attempted,failed in fetches[:-1]:
            diagnostics.append({'url':attempted,'origin':method,'final_url':failed.url,'http_status':failed.status,
                                'outcome':failed.state,'reason':failed.error,'recovery':'alternate_https_hostname'})
        diagnostic = {'url':fetches[-1][0],'candidate_url':url,'origin':method,'final_url':response.url,'http_status':response.status,'outcome':response.state,'reason':response.error}
        diagnostics.append(diagnostic)
        origins=(record.get('_public_hints') or {}).get('website_origins',{}).get(url)
        if origins:diagnostic['cache_source_indices']=origins
        provenance=(record.get('_public_hints') or {}).get('provenance',{}).get(url)
        if provenance:diagnostic['cache_provenance']=provenance
        if response.status != 200:
            failures.append(response.state)
            continue
        import threading
        profile.captures.mkdir(parents=True,exist_ok=True)
        capture = profile.captures/(response.digest+'.bin')
        if not capture.exists():
            temporary = capture.with_suffix('.'+str(threading.get_ident())+'.tmp')
            temporary.write_bytes(response.body)
            temporary.replace(capture)
        diagnostic.update(content_sha256=response.digest,retrieved_at=response.retrieved_at)
        if directory_url(response.url):
            diagnostic.update(outcome='withheld',reason='Directory/aggregator destination')
            failures.append("ambiguous")
            continue
        page = Page(response)
        if client.browser:
            rendered=client.browser.render_if_needed(page,client)
            if rendered:
                page=Page(rendered)
        own = verify_site(page, proof_record, method, client, url)
        owner_page = page
        # A legal/contact page can prove the same site's owner. Never bridge hosts.
        if not own and not (stated_numbers(page.root.text())-{str(record['organisasjonsnummer'])}):
            contacts = [(u, n) for u, n in page.links() if same_site(u, response.url) and re.search(r"kontakt|contact|om-oss|about|impressum|personvern|privacy|vilkår|terms|legal", u + " " + n.text(), re.I)]
            hints=[clean_url(u) for u in record.get('_website_proof_pages',[])]
            hints=[u for u in hints if u and u!=response.url and same_site(u,response.url) and not (urlsplit(response.url).scheme=='https' and urlsplit(u).scheme!='https')]
            contact_urls=list(dict.fromkeys(hints+[u for u,_ in contacts]))
            for contact_url in contact_urls[:3 if hints else 2]:
                fetched = client.get(contact_url, retries=0)
                if fetched.status == 200 and same_site(fetched.url, response.url):
                    contact = Page(fetched)
                    own = verify_site(contact, proof_record, method, client, url)
                    if own:
                        owner_page = contact
                        break
        if own and not PARKED.search(page.root.text()):
            diagnostic.update(outcome='accepted',reason=own['method'])
            home, proof, proof_page = page, own, owner_page
            break
        visible=page.root.text()
        reason='Parking/placeholder page' if PARKED.search(visible) else 'Contradictory organisation number' if stated_numbers(visible)-{str(record['organisasjonsnummer'])} else 'No owner-name evidence in a legal/contact context' if not name_claim(page,proof_record) else 'Insufficient contact corroboration or unresolved namesake/domain ownership'
        diagnostic.update(outcome='withheld',reason=reason)
        failures.append("ambiguous")
    if not home:
        state = "ambiguous" if "ambiguous" in failures else "failed" if "failed" in failures else "blocked" if "blocked" in failures else "not_available"
        profile.missing("official_website", state, "No reachable candidate passed source-specific legal-owner verification; details are in the separate discovery audit log")
        return
    site_url = home.response.url
    profile.add("official_website", site_url, proof_page.response, proof["span"], "company_owned", confidence=proof["confidence"])
    website_claim=next(c for c in profile.data['claims'] if c['field']=='official_website' and c['availability']=='available')
    for eid in proof.get('registry_evidence_ids',[]):
        if eid not in website_claim['evidence_ids']:website_claim['evidence_ids'].append(eid)
    for span in proof.get('corroborating_spans',[]):
        eid=profile.evidence(proof_page.response,span,'company_owned')
        if eid not in website_claim['evidence_ids']:website_claim['evidence_ids'].append(eid)
    for extra in proof.get('extra_evidence',[]):
        eid=profile.evidence(extra['response'],extra['span'],extra.get('source_class','public_knowledge_graph'))
        if eid not in website_claim['evidence_ids']:website_claim['evidence_ids'].append(eid)
    profile.data["website_identity"] = {"method": proof["method"], "source_url": proof_page.response.url, "confidence": proof["confidence"]}
    for node in home.root.all({"meta"}):
        if (node.attrs.get("name") or node.attrs.get("property") or "").lower() in {"description", "og:description"}:
            description = node.attrs.get("content") or ""
            if 30 <= len(description) <= 700 and not re.search(r"cookie|lorem ipsum|coming soon|welcome to our website", description, re.I):
                profile.add("site_description", description, home.response, home.span(node), "company_owned")
                break
    if not any(c['field']=='site_description' and c['availability']=='available' for c in profile.data['claims']):
        from .content import description
        fallback=description(home)
        if fallback:profile.add('site_description',fallback[0],home.response,fallback[1],'company_owned',method='main_content_description')
    from .site_crawl import crawl_verified_site
    crawl_verified_site(profile,client,record,home,page_cap=page_cap,owner_page=proof_page)
