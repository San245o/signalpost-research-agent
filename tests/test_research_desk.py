"""Source-bound parsing, real recruitment evidence, and comparable summaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from signalpost.content import description
from signalpost.web import Page,ownership,collect_site
from signalpost.careers import listed_roles
from signalpost.net import Budget,Client
from signalpost.records import Profile,validate
from signalpost.viewer import render
from .helpers import ORG,RECORD,response


class ResearchDesk(unittest.TestCase):
    def profile(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        return Profile(ORG,'fixture',Path(tmp.name))

    def test_graph_references_resolve_inside_the_original_script(self):
        graph={'@graph':[{'@type':'JobPosting','title':'Senior Engineer','hiringOrganization':{'@id':'#employer'}},
                         {'@id':'#employer','@type':'Organization','name':'Fjord Test AS','identifier':ORG}]}
        page=Page(response('https://fjordtest.no/jobs/12345','<script type="application/ld+json">'+json.dumps(graph)+'</script>'))
        job=next((v,span) for v,span in page.structured() if v['@type']=='JobPosting')
        self.assertEqual(job[0]['hiringOrganization']['identifier'],ORG)
        self.assertIn(job[1],page.raw);self.assertIn('"identifier"',job[1])

    def test_microdata_itemref_has_exact_spans_and_nested_scopes_do_not_leak(self):
        html=f'<div itemscope itemtype="https://schema.org/ProfessionalService" itemref="org"><meta itemprop="url" content="https://fjordtest.no/"><span itemprop="name">Fjord Test AS</span><div itemprop="department" itemscope itemtype="https://schema.org/Organization"><span itemprop="name">Other department</span></div></div><meta id="org" itemprop="taxID" content="{ORG}">'
        page=Page(response('https://fjordtest.no/',html));items=list(page.structured());own=items[0][0]
        self.assertEqual(own['taxID'],ORG);self.assertEqual(own['name'],'Fjord Test AS')
        self.assertEqual(own['department']['name'],'Other department')
        self.assertIn('id="org"',items[0][1]);self.assertIn(items[0][1],html)
        self.assertEqual(ownership(page,RECORD)['method'],'structured_owner_exact_org_number')

    def test_rdfa_subtype_ownership_and_relative_links(self):
        html=f'<section vocab="https://schema.org/" typeof="AccountingService"><span property="name">Fjord Test AS</span><meta property="taxID" content="{ORG}"><a property="url" href="/">Home</a></section>'
        page=Page(response('https://fjordtest.no/',html));own=list(page.structured())[0][0]
        self.assertEqual(own['url'],'https://fjordtest.no/')
        self.assertIsNotNone(ownership(page,RECORD))
        wrong=html.replace('href="/"','href="https://unrelated.no/"')
        self.assertIsNone(ownership(Page(response('https://fjordtest.no/',wrong)),RECORD))

    def test_content_description_ignores_cookie_and_customer_boilerplate(self):
        text='Vi utvikler og leverer programvare som hjelper bedrifter med planlegging, rapportering og samarbeid.'
        page=Page(response('https://fjordtest.no/','<nav><p>We provide navigation and help customers with every link on this website.</p></nav><main><p>'+text+'</p><div class="customer"><p>We provide a completely different service for this customer company and their users.</p></div></main>'))
        value,span=description(page);self.assertEqual(value,text);self.assertIn(span,page.raw)

    def test_inline_recruitment_ad_requires_role_qualification_and_apply_instruction(self):
        body='<main><h1>Ledig stilling</h1><p>Vi søker tømrere som er stolte av yrket sitt og ønsker å bygge gode boliger.</p><p>Ønskede kvalifikasjoner: fagbrev som tømrer.</p><a href="mailto:jobb@fjordtest.no">Send søknad</a></main>'
        for extra,expected in [('',1),('<p>Ingen ledige stillinger nå.</p>',0),('<p>Søknadsfrist: 2020-01-01</p>',0)]:
            p=self.profile();page=Page(response('https://fjordtest.no/ledig-stilling',body.replace('</main>',extra+'</main>')))
            listed_roles(p,page,RECORD,'https://fjordtest.no/')
            jobs=[c for c in p.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available'];self.assertEqual(len(jobs),expected)
            if jobs:
                self.assertEqual(jobs[0]['value']['title'],'tømrere');self.assertNotIn('date',jobs[0]['value'])
                self.assertFalse(jobs[0]['value']['direct_employer_verified']);validate(p.finish(),p.captures)
        p=self.profile();listed_roles(p,Page(response('https://fjordtest.no/careers',body.replace('Send søknad','Learn about us'))),RECORD,'https://fjordtest.no/')
        self.assertFalse(any(c['field']=='hiring_signal' for c in p.data['claims']))

    def test_verified_board_chain_supports_greenhouse_and_lever_without_invented_dates(self):
        for board,api,data in [
            ('https://boards.greenhouse.io/embed/job_board?for=fjordtest','https://boards-api.greenhouse.io/v1/boards/fjordtest/jobs',{'jobs':[
                {'title':'Senior Software Engineer','absolute_url':'https://job-boards.greenhouse.io/fjordtest/jobs/12345','internal_job_id':7,'updated_at':'2026-10-02'},
                {'title':'Prospect Job Pool','absolute_url':'https://boards.greenhouse.io/fjordtest/jobs/12346','internal_job_id':None},
                {'title':'Wrong Tenant Engineer','absolute_url':'https://boards.greenhouse.io/other/jobs/12347','internal_job_id':8}]}),
            ('https://jobs.eu.lever.co/fjordtest','https://api.eu.lever.co/v0/postings/fjordtest?mode=json',[
                {'text':'Senior Software Engineer','hostedUrl':'https://jobs.eu.lever.co/fjordtest/abc-123'},
                {'text':'Wrong Tenant Engineer','hostedUrl':'https://jobs.eu.lever.co/other/abc-124'}])]:
            with self.subTest(board=board):
                p=self.profile();seen=[]
                def transport(url,headers):
                    seen.append(url)
                    if url.endswith('/robots.txt'):return response(url,'',404)
                    if url=='https://fjordtest.no/':return response(url,f'<footer>Fjord Test AS Org.nr: {ORG}</footer><a href="{board}">Careers</a>')
                    if url==board:return response(url,'<title>Fjord Test AS Careers</title><h1>Open positions at Fjord Test</h1>')
                    if url==api:return response(url,data)
                    return response(url,'',404)
                collect_site(p,Client(Budget(30,10),ORG,transport=transport),{**RECORD,'hjemmeside':'https://fjordtest.no/'},candidate_cap=1,page_cap=3)
                self.assertIn(api,seen)
                jobs=[c for c in p.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available'];self.assertEqual(len(jobs),1)
                self.assertNotIn('date',jobs[0]['value']);self.assertFalse(jobs[0]['value']['direct_employer_verified'])
                spans=[e['claim_span'] for e in p.data['evidence'] if e['id'] in jobs[0]['evidence_ids']]
                self.assertTrue(any('href="'+board+'"' in span for span in spans));validate(p.finish(),p.captures)

    def test_summary_metrics_only_use_matching_currency_and_period_and_keep_citations(self):
        p=self.profile();r=response('https://data.brreg.no/fixture',{'revenue':1000,'annual_result':-50,'equity':200,'total_assets':800})
        for field,amount in [('revenue',1000),('annual_result',-50),('equity',200),('total_assets',800)]:
            p.add(field,{'amount':amount,'currency':'NOK'},r,r.text,period='2025')
        env=p.finish();summary=env['summary'];self.assertEqual(len(summary['sections']),6)
        ratio=next(m for m in summary['metrics'] if m['label']=='Equity / assets');self.assertEqual(ratio['value'],25)
        self.assertTrue(summary['signals']);validate(env,p.captures)
        broken=copy.deepcopy(env);broken['summary']['metrics'][0]['evidence_ids']=['missing']
        with self.assertRaises(ValueError):validate(broken)
        from signalpost.synthesis import summarize
        next(c for c in env['claims'] if c['field']=='total_assets')['value']['currency']='USD'
        self.assertFalse(any(m['label']=='Equity / assets' for m in summarize(env)['metrics']))
        self.assertTrue(next(s for s in summary['sections'] if s['id']=='hiring')['reason'])

    def test_viewer_embedded_payload_cannot_close_script(self):
        html=render([{'value':'</script><script>alert(1)</script>'}],{})
        self.assertNotIn('</script><script>alert(1)',html)
        self.assertIn('\\u003c/script>',html)


if __name__=='__main__':unittest.main()
