"""Independent registry stages: identity, accounts, roles and workplaces."""
from __future__ import annotations
import json
import math
import re
from datetime import date
from urllib.parse import urlencode
from .net import Response, clean_url

BASE = "https://data.brreg.no/enhetsregisteret/api/"
ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/"


def json_document(response):
    """Decode JSON and index exact source spans by path (no reconstructed quotes)."""
    text = response.text
    decoder = json.JSONDecoder()
    spans = {}
    def skip(i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i
    def walk(i, path):
        i = skip(i)
        start = i
        if text[i] == "{":
            result = {}
            i = skip(i + 1)
            while text[i] != "}":
                key, i = decoder.raw_decode(text, i)
                i = skip(i)
                if text[i] != ":":
                    raise ValueError("Malformed JSON object")
                value, i = walk(i + 1, path + (key,))
                result[key] = value
                i = skip(i)
                if text[i] == "}":
                    break
                if text[i] != ",":
                    raise ValueError("Malformed JSON object")
                i = skip(i + 1)
            i += 1
        elif text[i] == "[":
            result = []
            i = skip(i + 1)
            while text[i] != "]":
                value, i = walk(i, path + (len(result),))
                result.append(value)
                i = skip(i)
                if text[i] == "]":
                    break
                if text[i] != ",":
                    raise ValueError("Malformed JSON array")
                i = skip(i + 1)
            i += 1
        else:
            result, i = decoder.raw_decode(text, i)
        spans[path] = text[start:i]
        return result, i
    value, end = walk(0, ())
    if text[end:].strip():
        raise ValueError("Trailing JSON content")
    return value, spans


def _at(data, path):
    for key in path:
        if not isinstance(data, (dict, list)):
            return None
        try:
            data = data[key]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def add_identity(profile, record, response, *, snapshot=False):
    spans = {}
    if not snapshot:
        _, spans = json_document(response)
    def add(field, value, path):
        if value is None or value == "" or value == {}:
            return
        span = spans.get(path)
        if not span and snapshot:
            # Frozen row provenance is explicit; the raw row itself supports its values.
            span = response.text
        if span:
            pointer = f"snapshot row {record['organisasjonsnummer']} at line {getattr(response, 'snapshot_line', 1)}" if snapshot else "/" + "/".join(map(str, path))
            profile.add(field, value, response, span, "official_registry_snapshot" if snapshot else "official_registry", pointer=pointer)
    add("legal_name", record.get("navn"), ("navn",))
    add("legal_form", (record.get("organisasjonsform") or {}).get("kode"), ("organisasjonsform",))
    industry = record.get("naeringskode1") or {}
    if industry.get("kode") != "00.000" and str(industry.get("beskrivelse") or "").casefold() not in {"uoppgitt", "not stated"}:
        add("industry", industry.get("beskrivelse") or industry.get("kode"), ("naeringskode1",))
    else:
        profile.missing("industry", reason="The registry does not state an industry classification")
    count = record.get("antallAnsatte")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        add("employee_count", count, ("antallAnsatte",))
    add("founded_date", record.get("stiftelsesdato"), ("stiftelsesdato",))
    founded = str(record.get('stiftelsesdato') or '')[:10]
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', founded) and founded <= date.today().isoformat():
        span = response.text if snapshot else spans.get(('stiftelsesdato',))
        if span:
            profile.add('dated_activity', {'title':'Company founded (registry record)', 'date':founded,
                        'url':response.url, 'publisher_type':'official_registry', 'event_type':'founded'},
                        response,span,'official_registry_snapshot' if snapshot else 'official_registry',period=founded)
    for i, entry in enumerate(record.get('historiskeNavn') or []):
        if not isinstance(entry,dict) or not entry.get('navn') or entry['navn'].casefold()==str(record.get('navn')).casefold():
            continue
        ended = str(entry.get('tilDato') or '')[:10]
        span = response.text if snapshot else spans.get(('historiskeNavn',i))
        if span:
            add('former_name',entry['navn'],('historiskeNavn',i))
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}',ended) and ended<=date.today().isoformat():
                profile.add('dated_activity',{'title':'Former registered name ended: '+entry['navn'],
                            'date':ended,'url':response.url,'publisher_type':'official_registry','event_type':'former_name_ended'},
                            response,span,'official_registry_snapshot' if snapshot else 'official_registry',period=ended)
    activity = record.get("aktivitet") or record.get("vedtektsfestetFormaal")
    if isinstance(activity, list):
        activity = " ".join(str(v) for v in activity)
    add("business_description", activity, ("aktivitet",) if record.get("aktivitet") else ("vedtektsfestetFormaal",))
    address = record.get("forretningsadresse") or {}
    if any(v for v in address.values()):
        add("registered_business_address", address, ("forretningsadresse",))
    # Do not call absent status fields 'false'.
    status = {k: record[k] for k in ("konkurs", "underAvvikling", "underTvangsavviklingEllerTvangsopplosning") if isinstance(record.get(k), bool)}
    if status:
        state = "Bankrupt" if status.get("konkurs") else "In liquidation" if status.get("underAvvikling") or status.get("underTvangsavviklingEllerTvangsopplosning") else "No adverse status in the reported registry flags"
        span = response.text if snapshot else spans[()]
        profile.add("operating_status", state, response, span, "official_registry_snapshot" if snapshot else "official_registry", pointer="/")


def identity(profile, client, seed=None):
    url = BASE + "enheter/" + profile.org
    response = client.get(url, robots=False)
    if response.status == 200:
        try:
            record = response.json()
            if str(record.get("organisasjonsnummer")) != profile.org or not record.get("navn"):
                raise ValueError("Registry returned a different or incomplete entity")
            add_identity(profile, record, response)
            return record
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            profile.error("identity", f"Invalid registry response: {exc}")
    if seed:
        record, snapshot_response = seed
        add_identity(profile, record, snapshot_response, snapshot=True)
        profile.error("identity", "Live registry unavailable; using explicitly supplied snapshot", response.state)
        return record
    profile.error("identity", response.error or f"HTTP {response.status}", response.state)
    profile.missing("legal_name", response.state, "Registry identity could not be resolved")
    return None


METRICS = {
    "revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    "operating_result": ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    "result_before_tax": ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
    "annual_result": ("resultatregnskapResultat", "aarsresultat"),
    "total_assets": ("eiendeler", "sumEiendeler"),
    "equity": ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
    "total_debt": ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
}


def _filings(response, org):
    data, spans = json_document(response)
    rows = data if isinstance(data,list) else data.get('regnskap',[]) if isinstance(data,dict) else []
    prefix = () if isinstance(data,list) else ('regnskap',)
    result=[]
    for i,row in enumerate(rows):
        if not isinstance(row,dict) or row.get('regnskapstype')!='SELSKAP':continue
        if str((row.get('virksomhet') or {}).get('organisasjonsnummer') or '')!=org:continue
        end=(row.get('regnskapsperiode') or {}).get('tilDato','')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',end):result.append((end,prefix+(i,),row))
    return sorted(result,key=lambda row:row[0],reverse=True),spans


def accounts(profile, client):
    response=client.get(ACCOUNTS+profile.org+'?regnskapstype=SELSKAP',robots=False)
    if response.status!=200:
        profile.missing('annual_accounts_latest',response.state,response.error)
        return
    rows,spans=_filings(response,profile.org)
    seen=set()
    for end,path,row in rows:
        if end in seen:continue
        seen.add(end)
        source=response; source_spans=spans
        # Newest two company-only filings have unambiguous year-specific citations.
        if len(seen)<=2 and client.budget.remaining()>2:
            year=client.get(ACCOUNTS+profile.org+'?'+urlencode({'år':end[:4],'regnskapstype':'SELSKAP'}),robots=False)
            if year.status==200:
                try:
                    matches,year_spans=_filings(year,profile.org)
                    matches=[entry for entry in matches if entry[0]==end]
                    if len(matches)==1:
                        _,path,row=matches[0];source=year;source_spans=year_spans
                except (ValueError,TypeError,IndexError):pass
        period=str((row.get('regnskapsperiode') or {}).get('fraDato') or '')+'/'+end
        profile.add('annual_accounts_latest' if len(seen)==1 else 'annual_accounts_history',
                    {'period_end':end,'account_type':'SELSKAP','currency':row.get('valuta')},
                    source,source_spans[path],period=period,pointer='/'+'/'.join(map(str,path)))
        if len(seen)>2:continue
        for field,metric_path in METRICS.items():
            value=_at(row,metric_path)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
                full=path+metric_path
                profile.add(field,{'amount':value,'currency':row.get('valuta'),'account_type':'SELSKAP'},
                            source,source_spans[full],period=period,pointer='/'+'/'.join(map(str,full)))
        if row.get('journalnr'):
            pdf='https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/'+profile.org+'/'+end[:4]
            profile.add('annual_report_pdf',pdf,source,source_spans[path],period=period,
                        pointer='/'+'/'.join(map(str,path)))
    profile.missing('annual_accounts_latest',reason='No separate-entity filing with exact organisation number found')


def roles(profile, client):
    response = client.get(BASE + "enheter/" + profile.org + "/roller", robots=False)
    if response.status != 200:
        profile.missing("leader", response.state, response.error)
        return
    data, spans = json_document(response)
    for gi, group in enumerate(data.get("rollegrupper", [])):
        for ri, role in enumerate(group.get("roller", [])):
            person, entity = role.get("person") or {}, role.get("enhet") or {}
            if role.get("fratraadt") or role.get("avregistrert") or person.get("erDoed") or entity.get("erSlettet"):
                continue
            name_parts = person.get("navn") or {}
            name = " ".join(str(name_parts[k]) for k in ("fornavn", "mellomnavn", "etternavn") if name_parts.get(k))
            if not name:
                names = entity.get("navn")
                name = names[0] if isinstance(names, list) and names else names if isinstance(names, str) else ""
            if not name:
                continue
            role_type = role.get("type") or {}
            value = {"name": name, "role": role_type.get("beskrivelse") or role_type.get("kode") or "Registered role", "role_code": role_type.get("kode")}
            if entity.get("organisasjonsnummer"):
                value["organisation_number"] = entity["organisasjonsnummer"]
            path = ("rollegrupper", gi, "roller", ri)
            # Avoid persisting dates of birth: use only the name object + type in
            # claims; raw official source bytes remain in the evidence archive.
            profile.add("leader", value, response, spans[path + (("person", "navn") if name_parts else ("enhet", "navn"))], pointer="/" + "/".join(map(str, path)))
    profile.missing("leader", reason="No current registered role holders found")


def workplaces(profile, client):
    from urllib.parse import urljoin, urlsplit
    url=BASE+'underenheter?'+urlencode({'overordnetEnhet':profile.org,'size':100})
    units=[];seen=set();complete=False;observed_rows=0
    while url and url not in seen and len(seen)<100 and client.budget.remaining()>0:
        seen.add(url)
        response=client.get(url,robots=False)
        if response.status!=200:
            profile.error('workplaces',response.error or 'Workplace page failed',response.state)
            profile.missing('workplace',response.state,response.error)
            break
        data,spans=json_document(response)
        entries=(data.get('_embedded') or {}).get('underenheter',[])
        observed_rows+=len(entries)
        for i,row in enumerate(entries):
            if str(row.get('overordnetEnhet'))!=profile.org or row.get('slettedato'):continue
            unit={'organisation_number':row.get('organisasjonsnummer'),'name':row.get('navn'),
                  'address':row.get('beliggenhetsadresse') or row.get('forretningsadresse') or row.get('postadresse') or {},
                  'location_type':'registered_subunit'}
            if isinstance(row.get('antallAnsatte'),int):unit['employees']=row['antallAnsatte']
            path=('_embedded','underenheter',i)
            profile.add('workplace',unit,response,spans[path],pointer='/'+'/'.join(map(str,path)))
            units.append(unit)
            site=clean_url(row.get('hjemmeside'))
            if site:
                profile.data.setdefault('website_discovery',[]).append(
                    {'url':site,'origin':'live_registry_subunit','outcome':'candidate',
                     'subunit_organisation_number':str(row.get('organisasjonsnummer'))})
        next_url=((data.get('_links') or {}).get('next') or {}).get('href')
        if not next_url:
            pages=data.get('page') or {}
            if pages.get('number',0)+1<pages.get('totalPages',1):
                next_url=BASE+'underenheter?'+urlencode({'overordnetEnhet':profile.org,'size':100,'page':pages.get('number',0)+1})
            else:complete=True;break
        candidate=urljoin(url,next_url)
        parts=urlsplit(candidate)
        # Never forward an official lookup to a supplied external pagination link.
        if parts.scheme!='https' or parts.netloc!='data.brreg.no' or parts.path!='/enhetsregisteret/api/underenheter':
            profile.error('workplaces','Unsafe workplace pagination link','blocked');break
        url=candidate
    profile.data['workplace_scan']={'complete':complete,'pages':len(seen),'observed_rows':observed_rows}
    profile.data['operating_names']=[u['name'] for u in units if isinstance(u.get('name'),str)]
    # A registered office is labelled explicitly; it does not prove an operating site.
    if complete and observed_rows==0 and not units:
        own=next((c for c in profile.data['claims'] if c['field']=='registered_business_address' and c['availability']=='available'),None)
        if own:
            import copy
            claim=copy.deepcopy(own)
            claim.update(field='workplace',value={'organisation_number':profile.org,'address':own['value'],
                         'location_type':'registered_office; operating_presence_not_independently_verified'})
            profile.data['claims'].append(claim)
            from .records import claim_key
            claim['claim_key']=claim_key(claim)
    profile.missing('workplace',reason='No active subordinate workplaces found')
    return units


def registry_events(profile,client):
    url=BASE+'oppdateringer/enheter?'+urlencode({'organisasjonsnummer':profile.org,'size':100})
    response=client.get(url,robots=False)
    if response.status!=200:
        profile.error('registry_events',response.error or 'Registry events unavailable',response.state)
        return
    data,spans=json_document(response)
    entries=(data.get('_embedded') or {}).get('oppdaterteEnheter',[])
    dated=[]
    for i,entry in enumerate(entries):
        if not isinstance(entry,dict) or str(entry.get('organisasjonsnummer') or '')!=profile.org:continue
        day=str(entry.get('dato') or '')[:10]
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) and day<=date.today().isoformat():dated.append((day,i,entry))
    for day,i,entry in sorted(dated,key=lambda e:e[0],reverse=True)[:3]:
        path=('_embedded','oppdaterteEnheter',i)
        profile.add('dated_activity',{'title':'Registry record updated ('+str(entry.get('endringstype') or 'Endring')+')',
                    'date':day,'url':url,'event_type':'registry_update','publisher_type':'official_registry'},
                    response,spans[path],period=day,pointer='/'+'/'.join(map(str,path)))
