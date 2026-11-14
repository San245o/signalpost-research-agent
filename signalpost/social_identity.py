"""Named account ownership on an already verified company site."""
import json
import re
from functools import lru_cache
from pathlib import Path
from .web import words,company_words
from .net import host_of

GROUP=re.compile(r'group|gruppen|konsern|holding',re.I)
FOREIGN=re.compile(r'(?:^|[\W_])(?:ltd|limited|gmbh|oy|oyj|aps|inc|llc|bv|plc|srl|sarl|corp|pty)(?:$|[\W_])',re.I)

@lru_cache(maxsize=1)
def towns():
    try:return [''.join(words(n)) for n in json.loads((Path(__file__).resolve().parents[1]/'data/municipalities.json').read_text(encoding='utf-8'))['names']]
    except (OSError,ValueError,KeyError,TypeError):return []

def own_handle(handle,record,site):
    account=''.join(words(handle.lstrip('@')))
    legal=company_words(record.get('navn'));core=''.join(legal)
    brand=''.join(words(host_of(site).split('.')[0]))
    if not account or account.isdigit():return False
    if (FOREIGN.search(handle) or re.search(r'(?:gmbh|ltd|llc|oyj|sarl|corp)$',account)) and not FOREIGN.search(str(record.get('navn') or '')):return False
    if GROUP.search(account) and not GROUP.search(core) and GROUP.sub('',account)!=core:return False
    address=record.get('forretningsadresse') or {}
    own_place=''.join(words(str(record.get('navn'))+' '+brand+' '+str(address.get('poststed') or '')+' '+str(address.get('kommune') or '')))
    if any(len(t)>=4 and account.endswith(t) and len(account)>len(t)+3 and t not in own_place for t in towns()):return False
    # Prefixes avoid accepting an unrelated handle merely containing a common
    # company word in its middle. Only company/domain identities qualify.
    identities={core,brand}
    identities|={''.join(company_words(n)) for n in record.get('_operating_names',[]) if isinstance(n,str)}
    return any(account==key or len(key)>=3 and account.startswith(key) for key in identities if key)
