import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from signalpost.nav import NavIndex, nav_url
from signalpost.net import Budget, Client
from signalpost.records import Profile, validate
from signalpost.runner import run_batch
from signalpost.web import collect_site
from .helpers import ORG, RECORD, response, official_transport

TOKEN='eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwdWJsaWMifQ.c2lnbmF0dXJl'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.profile=Profile(ORG,'test',self.root/'sources')

    def tearDown(self):self.tmp.cleanup()

    def test_complete_pipeline_uses_paid_lead_only_after_free_candidates_fail(self):
        seen=[]
        def transport(url,headers,method='GET',body=None):
            seen.append((url,method))
            if method=='POST':
                return response(url,{'organic':[{'title':'Fjord Test AS','link':'https://real-fjord.no/'}],'credits':1})
            if url.endswith('/robots.txt'):return response(url,'',404)
            if 'real-fjord.no' in url:return response(url,f'<footer>Fjord Test AS. Org {ORG}</footer>',headers={'content-type':'text/html'})
            if 'data.brreg.no' in url:return official_transport(url,headers)
            return response(url,'',404)
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake-test-key'}):
            rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',transport=transport,nav_pages=0,
                                  search_mode='auto',paid_credits=1,candidate_cap=1,max_seconds=5)
        website=next(c for c in rows[0]['claims'] if c['field']=='official_website')
        self.assertEqual(website['availability'],'available')
        self.assertEqual(website['value'],'https://real-fjord.no/')
        self.assertEqual(report['provider']['credits_used'],1)
        self.assertTrue(any(c['field']=='revenue' for c in rows[0]['claims']))
        validate(rows[0],self.root/'run'/'sources')
        saved=''.join(p.read_text(encoding='utf-8',errors='ignore') for p in (self.root/'run').glob('*.json*'))
        self.assertNotIn('fake-test-key',saved)

    def test_search_snippet_does_not_override_wrong_owner(self):
        def transport(url,headers,method='GET',body=None):
            if method=='POST':return response(url,{'organic':[{'title':f'Fjord Test AS org {ORG}','link':'https://wrong-owner.no/'}]})
            if 'data.brreg.no' in url:return official_transport(url,headers)
            if url.endswith('/robots.txt'):return response(url,'',404)
            return response(url,'<footer>Other AS Org 123456789</footer>',headers={'content-type':'text/html'})
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake'}):
            rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',transport=transport,nav_pages=0,
                                  paid_credits=1,candidate_cap=0,max_seconds=5)
        self.assertFalse(any(c['field']=='official_website' and c['availability']=='available' for c in rows[0]['claims']))

    def test_no_paid_search_when_registered_site_is_verified(self):
        record=copy.deepcopy(RECORD);record['hjemmeside']='https://fjordtest.no/'
        posts=[]
        def transport(url,headers,method='GET',body=None):
            if method=='POST':posts.append(url);raise AssertionError('Unnecessary paid request')
            if 'data.brreg.no' in url:
                if url.endswith('/'+ORG):return response(url,record)
                return official_transport(url,headers)
            if url.endswith('/robots.txt'):return response(url,'',404)
            return response(url,f'<footer>Fjord Test AS {ORG}</footer>',headers={'content-type':'text/html'})
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake'}):
            rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',transport=transport,nav_pages=0,paid_credits=1,max_seconds=5,paid_categories=('websites',))
        self.assertEqual(posts,[])
        self.assertEqual(report['provider']['credits_used'],0)

    def test_contact_bridge_retains_the_contact_source(self):
        record=copy.deepcopy(RECORD);record['hjemmeside']='https://fjordtest.no/'
        def transport(url,headers):
            if url.endswith('/robots.txt'):return response(url,'',404)
            if url.endswith('/contact'):return response(url,f'<footer>Fjord Test AS {ORG}</footer>',headers={'content-type':'text/html'})
            return response(url,'<h1>Public Fjord Brand</h1><a href="/contact">Contact</a>',headers={'content-type':'text/html'})
        collect_site(self.profile,Client(Budget(20,5),ORG,transport=transport),record,page_cap=0)
        website=next(c for c in self.profile.data['claims'] if c['field']=='official_website')
        ev=next(e for e in self.profile.data['evidence'] if e['id']==website['evidence_ids'][0])
        self.assertEqual(ev['source_url'],'https://fjordtest.no/contact')
        self.assertEqual(website['value'],'https://fjordtest.no/')

    def test_nav_url_refuses_external_or_credentialed_targets(self):
        for url in ('https://evil.org/ad','https://pam-stilling-feed.nav.no.evil.org/ad','https://user:pass@pam-stilling-feed.nav.no/ad','http://pam-stilling-feed.nav.no/ad'):
            self.assertIsNone(nav_url(url))

    def test_nav_feed_applies_inactivation(self):
        def transport(url,headers):
            if url.endswith('publicToken'):return response(url,TOKEN)
            if url.endswith('/p2'):return response(url,{'items':[{'id':'a','_feed_entry':{'status':'INACTIVE'}}]})
            return response(url,{'items':[{'id':'a','url':'/api/v1/feedentry/a','_feed_entry':{'status':'ACTIVE','businessName':'Fjord Test AS'}}],'next_url':'/p2'})
        index=NavIndex();index.build(Client(Budget(10,5),'nav',transport=transport))
        self.assertTrue(index.complete)
        self.assertEqual(index.active,{})

    def test_nav_partial_window_is_declared(self):
        def transport(url,headers):
            return response(url,TOKEN) if url.endswith('publicToken') else response(url,{'items':[],'next_url':'/p2'})
        index=NavIndex();index.build(Client(Budget(10,5),'nav',transport=transport),max_pages=1)
        self.assertFalse(index.complete)
        self.assertIn('partial',index.error)

    def test_nav_exact_employer_and_current_state_are_required(self):
        today=datetime.now(timezone.utc).date().isoformat()
        job={'ad_content':{'title':'Engineer','status':'ACTIVE','published':today,'expires':'2099-12-31','employer':{'orgnr':ORG},'uuid':'a'}}
        index=NavIndex();index.token=TOKEN
        index.active={'a':{'id':'a','url':'https://pam-stilling-feed.nav.no/api/v1/feedentry/a','name':'fjord test'}}
        def acquire(data):
            p=Profile(ORG,'test',self.root/'sources')
            index.add_jobs(p,RECORD,Client(Budget(10,5),ORG,transport=lambda u,h:response(u,data)))
            return [c for c in p.data['claims'] if c['availability']=='available']
        self.assertEqual(len(acquire(job)),1)
        job['ad_content']['employer']['orgnr']='123456789'
        self.assertEqual(acquire(job),[])
        job['ad_content']['employer']['orgnr']=ORG;job['ad_content']['status']='INACTIVE'
        self.assertEqual(acquire(job),[])

    def test_nav_parent_subunit_proof_can_attribute_a_job(self):
        self.profile.add('workplace',{'organisation_number':'123456789'},response('https://data.brreg.no/','unit'),'unit')
        index=NavIndex();index.token=TOKEN;index.active={'a':{'id':'a','url':'https://pam-stilling-feed.nav.no/a','name':'fjord test'}}
        today=datetime.now(timezone.utc).date().isoformat()
        job={'ad_content':{'title':'Engineer','status':'ACTIVE','published':today,'expires':'2099-12-31','employer':{'orgnr':'123456789'},'uuid':'a'}}
        index.add_jobs(self.profile,RECORD,Client(Budget(10,5),ORG,transport=lambda u,h:response(u,job)))
        self.assertTrue(any(c['field']=='hiring_signal' for c in self.profile.data['claims']))
