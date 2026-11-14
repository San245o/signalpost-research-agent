"""Structured markup and readable page content with original-source spans."""
import json
import re
from itertools import islice
from urllib.parse import urljoin

ORGANISATIONS={'Organization','Corporation','LocalBusiness','Store','ProfessionalService','FinancialService',
               'AccountingService','LegalService','MedicalBusiness','Dentist','AutomotiveBusiness','AutoRepair',
               'Restaurant','FoodEstablishment','LodgingBusiness','Hotel','RealEstateAgent','EmploymentAgency',
               'EducationalOrganization','NGO','SportsOrganization','HomeAndConstructionBusiness','Electrician',
               'Plumber','GeneralContractor','TravelAgency','InsuranceAgency'}
NOISE=re.compile(r'cookie|consent|navigation|breadcrumb|newsletter|subscribe|personvern|privacy|copyright|testimonial|customer|client',re.I)


def type_names(item):
    values=item.get('@type') or []
    return {v.rstrip('/').rsplit('/',1)[-1].rsplit('#',1)[-1].rsplit(':',1)[-1]
            for v in (values if isinstance(values,list) else [values]) if isinstance(v,str)}


def is_organisation(item):return bool(type_names(item)&ORGANISATIONS)


def structured(page):
    def walk(value,depth=0):
        if depth>16:return
        if isinstance(value,list):
            for child in value:yield from walk(child,depth+1)
        elif isinstance(value,dict):
            yield value
            for key,child in value.items():
                if key!='@context':yield from walk(child,depth+1)
    for node in page.root.all({'script'}):
        if node.attrs.get('type','').lower().split(';')[0]!='application/ld+json':continue
        raw=''.join(c for c in node.children if isinstance(c,str)).strip()
        raw=re.sub(r'^\s*<!--|-->\s*$','',raw).strip().rstrip(';')
        try:objects=list(islice(walk(json.loads(raw)),1000))
        except (ValueError,RecursionError):continue
        by_id={str(obj['@id']):obj for obj in objects if obj.get('@id') and len(obj)>1}
        def resolve(value,seen=(),depth=0):
            if depth>5:return value
            if isinstance(value,list):return [resolve(x,seen,depth+1) for x in value[:200]]
            if not isinstance(value,dict):return value
            ident=value.get('@id');item=value
            if ident and ident not in seen and ident in by_id:item={**by_id[ident],**value}
            return {k:resolve(v,seen+(ident,) if ident else seen,depth+1) if k not in {'@context','@id'} else v for k,v in item.items()}
        for item in objects:
            if item.get('@type'):yield resolve(item),page.span(node)
    # Microdata and RDFa have independent scopes. A nested organisation must
    # remain attached to its parent property instead of leaking into siblings.
    def markup(scope,kind,depth=0):
        if depth>8:return {},page.span(scope)
        type_attr='itemtype' if kind=='micro' else 'typeof'
        prop_attr='itemprop' if kind=='micro' else 'property'
        scope_attr='itemscope' if kind=='micro' else 'typeof'
        types=scope.attrs.get(type_attr,'').split();item={'@type':types if len(types)>1 else types[0] if types else ''}
        ident=scope.attrs.get('itemid') if kind=='micro' else scope.attrs.get('about')
        if ident:item['@id']=urljoin(page.response.url,ident)
        roots=[scope]
        if kind=='micro':
            for ref in scope.attrs.get('itemref','').split():
                roots.extend(n for n in page.root.all() if n.attrs.get('id')==ref)
        for root in roots:
            for node in root.all():
                if node is scope:continue
                prop=node.attrs.get(prop_attr)
                if not prop:continue
                if node is not root:
                    ancestor=node.parent
                    while ancestor is not root and ancestor and scope_attr not in ancestor.attrs:ancestor=ancestor.parent
                    if ancestor is not root:continue
                value=markup(node,kind,depth+1)[0] if scope_attr in node.attrs else node.attrs.get('content') or node.attrs.get('datetime') or node.attrs.get('resource') or node.attrs.get('href') or node.attrs.get('src') or node.text()
                for key in prop.split():
                    key=key.rstrip('/').rsplit('/',1)[-1].rsplit('#',1)[-1].rsplit(':',1)[-1]
                    if key in {'url','sameAs'} and isinstance(value,str):value=urljoin(page.response.url,value)
                    if key in item:item[key]=item[key]+[value] if isinstance(item[key],list) else [item[key],value]
                    else:item[key]=value
        start=min(n.start for n in roots);end=max(n.end for n in roots)
        return item,page.raw[start:end]
    for node in page.root.all():
        if 'itemscope' in node.attrs and node.attrs.get('itemtype'):yield markup(node,'micro')
        elif node.attrs.get('typeof'):
            current=node;schema='schema:' in node.attrs['typeof'] or 'schema.org/' in node.attrs['typeof']
            while current and not schema:
                schema='schema.org' in current.attrs.get('vocab','') or 'schema.org' in current.attrs.get('prefix','');current=current.parent
            if schema:yield markup(node,'rdfa')


def content_nodes(page,tags=None):
    mains=list(page.root.all({'main'}));roots=mains or list(page.root.all({'body'})) or [page.root]
    for root in roots:
        for node in root.all(tags or {'p','li','h1','h2','h3'}):
            current=node;valid=True
            while current:
                if current.tag in {'script','style','nav','footer','header','aside','form','blockquote'} or NOISE.search(str(current.attrs)):
                    valid=False;break
                current=current.parent
            if valid and node.text():yield node


def description(page):
    choices=[]
    for node in content_nodes(page,{'p'}):
        text=node.text()
        if not 65<=len(text)<=650 or NOISE.search(text) or node.attrs.get('itemprop')=='articleBody':continue
        current=node.parent
        if any(ancestor.tag=='article' for ancestor in _ancestors(current)):continue
        score=bool(re.search(r'\b(?:we|our|vi|vår|vårt)\b',text,re.I))+2*bool(re.search(r'leverer|tilbyr|utvikler|produser|provid|speciali|design|manufactur|help|hjelper',text,re.I))
        if score>=2:choices.append((score,len(text),node))
    if choices:
        _,_,node=max(choices,key=lambda x:(x[0],-abs(x[1]-220)))
        return node.text(),page.span(node)
    return None


def _ancestors(node):
    while node:yield node;node=node.parent
