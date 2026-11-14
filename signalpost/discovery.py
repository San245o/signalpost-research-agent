"""Connect declared public hints to fresh evidence, before the site identity gate.

Cached social URLs never publish themselves. A Wikidata entity must freshly
resolve to exactly this organisation; deprecated and ended statements are ignored.
"""
import re
from datetime import datetime, timezone
from .net import clean_url,host_of
from .registry import json_document
from .web import canonical_social, directory_url

SOCIAL = {'P2013':'https://facebook.com/{}', 'P2003':'https://instagram.com/{}',
          'P2002':'https://x.com/{}', 'P4264':'https://www.linkedin.com/company/{}',
          'P2397':'https://www.youtube.com/channel/{}'}


def current_statements(entity, prop):
    today = datetime.now(timezone.utc).date().isoformat()
    for index, statement in enumerate(entity.get('claims', {}).get(prop, [])):
        if not isinstance(statement, dict) or statement.get('rank') == 'deprecated':
            continue
        qualifiers = statement.get('qualifiers') or {}
        # Scoped accounts and subsidiaries are not entity-wide accounts.
        if set(qualifiers)-{'P580','P582'}:
            continue
        def dates(key):
            return [q.get('datavalue',{}).get('value',{}).get('time','').lstrip('+')[:10]
                    for q in qualifiers.get(key,[]) if q.get('snaktype')=='value']
        if any(d and d < today for d in dates('P582')) or any(d > today for d in dates('P580')):
            continue
        snak = statement.get('mainsnak') or {}
        if snak.get('snaktype') != 'value':
            continue
        value = (snak.get('datavalue') or {}).get('value')
        if isinstance(value,str):
            yield index,value


def enrich_public_hints(profile, client, record):
    """One bounded graph lookup; return live website leads to the normal verifier."""
    hints = record.get('_public_hints') or {}
    ledger = profile.data.setdefault('external_discovery', [])
    for url in hints.get('social_urls', []):
        ledger.append({'url':url, 'kind':'cached_social_hint', 'outcome':'withheld',
                       'reason':'Requires a fresh verified-site link or exact-entity public graph statement'})
    websites = []
    for qid in hints.get('wikidata', [])[:1]:
        if not re.fullmatch(r'Q[1-9]\d*',qid):
            continue
        url = f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json'
        response = client.get(url, retries=0)
        attempt = {'url':url,'kind':'wikidata_exact_entity','status':response.status,
                   'outcome':'withheld','content_sha256':response.digest,'retrieved_at':response.retrieved_at}
        ledger.append(attempt)
        if response.status != 200:
            continue
        try:
            data,spans = json_document(response)
            entity = data['entities'][qid]
            ids = {value for _,value in current_statements(entity,'P2333')}
            if ids != {profile.org}:
                attempt['reason'] = 'Live organisation identifiers do not resolve uniquely to the target'
                continue
            attempt['outcome'] = 'exact_entity'
            proof_path = ('entities',qid)
            for _,value in current_statements(entity,'P856'):
                site = clean_url(value)
                if site and not directory_url(site) and site not in websites:
                    websites.append(site)
                    record.setdefault('_live_identity_bridges',{})[host_of(site)]={'response':response,'span':spans[proof_path]}
            # Only graph-listed accounts also present in the original public
            # hints are considered; graph identity and account bytes are cited.
            expected = {found[0] for value in hints.get('social_urls',[]) if (found:=canonical_social(value))}
            for prop,template in SOCIAL.items():
                for _,value in current_statements(entity,prop):
                    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',value):
                        continue
                    found = canonical_social(template.format(value))
                    if found and found[0] in expected:
                        profile.add('company_profile',found[0],response,spans[proof_path],
                                    'public_knowledge_graph',confidence=.97,
                                    pointer='/entities/'+qid,method='wikidata_exact_org_and_current_account')
        except (ValueError,KeyError,TypeError,IndexError):
            attempt['reason'] = 'Unusable public entity response'
    return websites[:3]

