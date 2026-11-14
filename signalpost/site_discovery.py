"""Bounded domain plans and hostname recovery; every result remains a lead."""
import ipaddress
import re
from urllib.parse import urlsplit,urlunsplit
from .net import clean_url


def name_urls(name):
    from .web import company_words,LEGAL
    tokens=company_words(name)
    if not tokens:return []
    joined=''.join(tokens);out=[]
    def add(label,tld='no'):
        if 3<=len(label)<=60 and not label.isdigit():
            url=clean_url('https://'+label+'.'+tld+'/')
            if url and url not in out:out.append(url)
    add(joined)
    if len(tokens)>1:add('-'.join(tokens))
    # A leading brand phrase is more useful than its generic first word.
    if len(tokens)>=3:add(''.join(tokens[:2]))
    connectors=[t for t in tokens if t not in {'og','and'}]
    if connectors!=tokens:add(''.join(connectors))
    generic={'holding','holdings','invest','investment','investering','capital','kapital',
             'eiendom','eiendommer','property','properties','gruppen','group','drift','norge','norway'}
    shortened=[t for t in connectors if t not in generic]
    if shortened and shortened!=connectors:
        add(''.join(shortened))
        if len(shortened)>1:add('-'.join(shortened))
    # Norwegian names use both the short and expanded ASCII spellings.
    expanded=name.casefold().replace('æ','ae').replace('ø','oe').replace('å','aa')
    alt=''.join(company_words(expanded))
    if alt!=joined:add(alt)
    accented=re.findall(r'[^\W_]+',name.casefold(),re.U)
    while accented and accented[-1] in LEGAL:accented.pop()
    if ''.join(accented)!=joined:add(''.join(accented))
    add(joined,'com')
    add(joined+'-as')
    if len(tokens)>1 and len(tokens[0])>=5 and tokens[0] not in generic:
        add(tokens[0])
    return out[:12]


def fetch_candidate(client,url):
    """Retry an unreachable HTTPS hostname with/without www, without downgrading.

    An explicit robots denial or reachable server response stops recovery.
    Each alternative independently passes DNS, TLS and robots checks in Client.
    """
    first=client.get(url,retries=0);attempts=[(url,first)]
    p=urlsplit(url)
    if first.status or p.scheme!='https' or not p.hostname:return first,attempts
    reason=first.error.lower()
    if 'robots policy disallows' in reason or 'downgrade' in reason:return first,attempts
    if client.budget.remaining()<=0 or client.budget.clock()>=client.deadline:return first,attempts
    host=p.hostname
    try:ipaddress.ip_address(host);return first,attempts
    except ValueError:pass
    other=host[4:] if host.startswith('www.') else 'www.'+host
    candidate=clean_url(urlunsplit((p.scheme,other,p.path or '/',p.query,'')))
    if not candidate:return first,attempts
    result=client.get(candidate,retries=0);attempts.append((candidate,result))
    return result,attempts
