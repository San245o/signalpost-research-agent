"""Aggregate public registry contacts; never export per-company contact answers."""
import csv
import gzip
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from signalpost.net import clean_url,host_of


def build(source,output):
    source,output=Path(source),Path(output)
    owners={key:defaultdict(set) for key in ('phones','email_domains','website_hosts')}
    rows=0
    with gzip.open(source,'rt',encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if not {'organisasjonsnummer','hjemmeside','telefon','epostadresse'}<=set(reader.fieldnames or []):raise ValueError('Unexpected public registry CSV schema')
        for row in reader:
            org=row.get('overordnetEnhet') or row.get('organisasjonsnummer')
            if not re.fullmatch(r'\d{9}',str(org or '')):continue
            rows+=1
            values={'phones':set(),'email_domains':set(),'website_hosts':set()}
            for key in ('telefon','mobil'):
                number=re.sub(r'\D','',row.get(key) or '')
                if len(number)==8 or len(number)==10 and number.startswith('47'):values['phones'].add(number[-8:])
            email=row.get('epostadresse') or ''
            if '@' in email:
                host=host_of(clean_url(email.rsplit('@',1)[1]))
                if host:values['email_domains'].add(host)
            host=host_of(clean_url(row.get('hjemmeside')))
            if host:values['website_hosts'].add(host)
            for key,items in values.items():
                for value in items:
                    # Saturated lower bounds avoid exporting identifiers or huge
                    # parent-holder sets. Five is the weak-evidence screening gate.
                    if len(owners[key][value])<5:owners[key][value].add(org)
    payload={key:{v:len(orgs) for v,orgs in sorted(index.items()) if len(orgs)>=5} for key,index in owners.items()}
    raw=gzip.compress(json.dumps(payload,sort_keys=True,separators=(',',':')).encode(),mtime=0)
    output.write_bytes(raw)
    with source.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'filename':output.name,'sha256':hashlib.sha256(raw).hexdigest(),'source_file':source.name,
            'source_sha256':digest,'registry_rows':rows,'licence':'NLOD-2.0','counts_are':'distinct-parent lower bounds capped at five; screening only',
            'screened_values':{key:len(index) for key,index in payload.items()}}
