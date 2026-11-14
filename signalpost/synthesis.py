"""Deterministic synthesis; calculations retain the underlying filing citations."""
import math
from datetime import datetime, timezone


def summarize(env):
    claims=[c for c in env['claims'] if c['availability']=='available']
    claims.sort(key=lambda c:(not c.get('stale',False),c.get('reporting_period','')),reverse=True)
    def rows(field):return [c for c in claims if c['field']==field]
    def first(field):return next(iter(rows(field)),None)
    sentences=[]
    def sentence(text,used):
        if not used:return
        if any(c.get('stale') for c in used):text+=' Last supported observation; not reverified in this run.'
        sentences.append({'text':text,'fields':list(dict.fromkeys(c['field'] for c in used)),
                          'evidence_ids':list(dict.fromkeys(e for c in used for e in c['evidence_ids']))})
    def amount(c):
        v=c['value'];currency=v.get('currency') or '(currency not stated)'
        return f"{v['amount']:,.0f} {currency}"
    def comparable(a,b):
        return a and b and a.get('reporting_period')==b.get('reporting_period') and a['value'].get('currency') and a['value'].get('currency')==b['value'].get('currency')
    name=first('legal_name');label=str(name['value']) if name else env['organisation_number']
    if name:
        used=[c for c in (name,first('legal_form'),first('industry'),first('operating_status')) if c]
        sentence(label+(': '+'; '.join(str(c['value']) for c in used[1:]) if len(used)>1 else '')+'.',used)
    desc=first('business_description') or first('site_description')
    if desc:sentence(('Registered activity: ' if desc['field']=='business_description' else 'The company describes itself: ')+str(desc['value']),[desc])
    site_desc=first('site_description')
    if site_desc and desc and desc['field']!='site_description' and str(site_desc['value']).casefold()!=str(desc['value']).casefold():
        sentence('On its verified website, the company describes its work: '+str(site_desc['value']),[site_desc])
    revenue=first('revenue');trend=None
    if revenue:
        sentence('Filed separate-entity revenue for '+revenue['reporting_period']+': '+amount(revenue)+'.',[revenue])
        previous=next((c for c in rows('revenue') if c.get('reporting_period','')<revenue.get('reporting_period','') and c['value'].get('currency')==revenue['value'].get('currency')),None)
        if previous and revenue['value'].get('currency') and previous['value']['amount']>0:
            trend=(revenue['value']['amount']/previous['value']['amount']-1)*100
            if math.isfinite(trend):sentence(f"Revenue changed {trend:+.1f}% from the previous available filing ({previous['reporting_period']}); the filings may cover different lengths.",[revenue,previous])
    result=first('annual_result')
    if result:
        kind='profit' if result['value']['amount']>0 else 'loss' if result['value']['amount']<0 else 'break-even result'
        sentence('The filed annual '+kind+' was '+amount(result)+' ('+result['reporting_period']+').',[result])
        if comparable(result,revenue) and revenue['value']['amount']>0:
            margin=100*result['value']['amount']/revenue['value']['amount']
            if math.isfinite(margin):sentence(f'The annual result was {margin:.1f}% of revenue.',[result,revenue])
        prior=next((c for c in rows('annual_result') if c.get('reporting_period','')<result.get('reporting_period','') and c['value'].get('currency')==result['value'].get('currency')),None)
        if prior and result['value'].get('currency'):
            if prior['value']['amount']<0<result['value']['amount']:sentence('The company moved from a filed loss to a profit between the two available periods.',[result,prior])
            elif prior['value']['amount']>0>result['value']['amount']:sentence('The company moved from a filed profit to a loss between the two available periods.',[result,prior])
    operating=first('operating_result')
    if operating and comparable(operating,revenue) and revenue['value']['amount']>0:
        margin=100*operating['value']['amount']/revenue['value']['amount']
        if math.isfinite(margin):sentence(f'The filed operating margin was {margin:.1f}% for '+operating['reporting_period']+'.',[operating,revenue])
    equity=first('equity')
    if equity:sentence('Filed equity was '+amount(equity)+(' (negative equity).' if equity['value']['amount']<0 else '.'),[equity])
    leaders=[c for c in rows('leader') if c['value'].get('role_code') in {'DAGL','LEDE'}][:3]
    if leaders:sentence('Registered leadership: '+'; '.join(c['value']['name']+' ('+c['value']['role']+')' for c in leaders)+'.',leaders)
    for field,prefix in [('official_website','Verified website: '),('employee_count','Registered employees: '),('founded_date','Registered founding date: ')]:
        c=first(field)
        if c:sentence(prefix+str(c['value'])+'.',[c])
    places=rows('workplace')
    if places:
        registered=[c for c in places if 'registered_office' in str(c['value'].get('location_type',''))]
        if len(registered)==len(places):sentence('The registered office is recorded; operating presence at that address was not independently verified.',registered)
        else:sentence(str(len(places))+' registered workplace/location record(s) were supported by the checked sources.',places)
    profiles=rows('company_profile')
    if profiles:sentence('Company-linked public profiles: '+', '.join(str(c['value']) for c in profiles[:4])+'.',profiles[:4])
    jobs=rows('hiring_signal');job=next(iter(jobs),None)
    if job:
        recruiter=[c for c in jobs if c['value'].get('relationship')=='recruiter_advertisement']
        listed=[c for c in jobs if c['value'].get('relationship') and c not in recruiter]
        direct=[c for c in jobs if not c['value'].get('relationship')]
        if recruiter:sentence(f'{len(recruiter)} recruiter-advertised role(s) were observed; these do not establish jobs at the recruiter.',recruiter)
        if listed:sentence(f'{len(listed)} advertised or listed role(s) were observed; individual status and direct employment remain unconfirmed.',listed)
        if direct:sentence(f'{len(direct)} job posting(s) with supported employer and date evidence were found.',direct)
        value=job['value']
        if value.get('relationship')=='recruiter_advertisement':sentence('Recruiter-advertised role: '+value['title']+' (observed '+value['observed_at']+'); employment at the recruiter is not established.',[job])
        elif value.get('relationship'):sentence('Role advertised or listed by the company: '+value['title']+' (observed '+value['observed_at']+'; direct employer not independently confirmed).',[job])
        else:sentence('A supported job posting: '+value['title']+' ('+value['date']+').',[job])
    events=rows('dated_activity')
    events.sort(key=lambda c:(c['value'].get('publisher_type')=='company_published',c['value'].get('date','')),reverse=True)
    if events:
        event=events[0];value=event['value']
        prefix=('Latest checked company announcement: ' if value.get('activity_kind')=='company_announcement' else 'Latest checked company-published article: ') if value.get('publisher_type')=='company_published' else 'Dated registry activity: '
        sentence(prefix+value['title']+' ('+value['date']+').',[event])
    for change in env.get('changes',[])[:3]:
        used=[c for c in claims if c.get('claim_key')==change.get('claim_key')]
        if used:sentence('Refresh detected '+change['type'].replace('_',' ')+' for '+change['field'].replace('_',' ')+'.',used)
    from .records import FAMILIES
    unknowns=[f for f in FAMILIES if not any(c['field']==f and not c.get('stale') for c in claims)]
    headline=label
    if revenue:headline+=' · '+amount(revenue)+' revenue'+(f' ({trend:+.1f}%)' if trend is not None and math.isfinite(trend) else '')
    summary={'as_of':datetime.now(timezone.utc).date().isoformat(),'headline':headline,
            'text':' '.join(s['text'] for s in sentences),'sentences':sentences,'unknowns':unknowns,
            'changes':env.get('changes',[]),'stale_claim_count':sum(bool(c.get('stale')) for c in claims)}
    summary['sections']=business_sections(env,sentences)
    metrics=[]
    for c,title in [(revenue,'Revenue'),(result,'Annual result'),(equity,'Equity')]:
        if c:metrics.append({'label':title,'value':c['value']['amount'],'unit':c['value'].get('currency'),
                             'period':c.get('reporting_period'),'evidence_ids':c['evidence_ids'],'stale':bool(c.get('stale'))})
    assets=first('total_assets')
    if comparable(equity,assets) and assets['value']['amount']>0:
        ratio=100*equity['value']['amount']/assets['value']['amount']
        if math.isfinite(ratio):metrics.append({'label':'Equity / assets','value':round(ratio,1),'unit':'%',
             'period':equity.get('reporting_period'),'evidence_ids':list(dict.fromkeys(equity['evidence_ids']+assets['evidence_ids'])),'stale':bool(equity.get('stale') or assets.get('stale'))})
    summary['metrics']=metrics
    signals=[]
    for c,label in [(result,'The latest filed annual result is a loss.'),(equity,'The latest filed equity is negative.')]:
        if c and c['value']['amount']<0:signals.append({'kind':'filing_observation','text':label,'evidence_ids':c['evidence_ids'],'period':c.get('reporting_period')})
    summary['signals']=signals
    return summary


def business_sections(env,sentences):
    groups=[('business','What does the company do?',{'legal_name','legal_form','industry','business_description','site_description','operating_status'}),
            ('financials','How is the company doing?',{'revenue','annual_result','operating_result','equity','total_assets','total_debt'}),
            ('people','Who runs the company?',{'leader'}),
            ('locations','Where does it operate?',{'workplace','registered_business_address'}),
            ('hiring','What hiring evidence is available?',{'hiring_signal','employee_count'}),
            ('activity','What has it published and where is it online?',{'dated_activity','official_website','company_profile'})]
    result=[]
    for ident,question,fields in groups:
        related=[s for s in sentences if fields.intersection(s['fields'])]
        claims=[c for c in env['claims'] if c['field'] in fields and c['availability']=='available']
        fresh=[c for c in claims if not c.get('stale')]
        text=' '.join(s['text'] for s in related)
        reason=None
        if ident=='hiring' and not any(c['field']=='hiring_signal' and not c.get('stale') for c in claims):
            reason='No supported job advertisement was found in the checked sources; this does not establish that the company is not hiring.'
        elif not claims:reason='The checked sources did not establish an answer. Missing evidence is not a negative finding.'
        elif not fresh:reason='Only earlier supported observations are available; they were not reverified in this run.'
        result.append({'id':ident,'question':question,'text':text,'fields':sorted(fields),
                       'availability':'available' if fresh else 'stale' if claims else 'not_available',
                       'evidence_ids':list(dict.fromkeys(eid for c in claims for eid in c['evidence_ids'])),
                       'reason':reason})
    return result
