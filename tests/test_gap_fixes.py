import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from signalpost.net import Budget,Client,tls_context
from signalpost.records import Profile,validate
from signalpost.search import Serper
from signalpost.site_crawl import listed_roles
from signalpost.web import Page,verify_site
from .helpers import ORG,RECORD,response


class GapFixes(unittest.TestCase):
    def test_transient_robots_failure_retries_but_explicit_disallow_is_enforced(self):
        calls=[]
        def transport(url,headers):
            calls.append(url)
            if url.endswith('/robots.txt') and calls.count(url)==1:raise TimeoutError('temporary')
            return response(url,'',404) if url.endswith('/robots.txt') else response(url,'company page')
        client=Client(Budget(8,5),ORG,transport=transport)
        self.assertEqual(client.get('https://brand.no/',retries=0).status,200)
        self.assertEqual(calls.count('https://brand.no/robots.txt'),2)
        calls=[]
        def denied(url,headers):calls.append(url);return response(url,'User-agent: *\nDisallow: /')
        client=Client(Budget(8,5),ORG,transport=denied)
        self.assertEqual(client.get('https://brand.no/').state,'blocked')
        self.assertEqual(calls,['https://brand.no/robots.txt'])

    def test_ca_bundle_keeps_chain_and_hostname_verification(self):
        context=tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertGreater(len(context.get_ca_certs()),50)

    def test_free_search_reads_website_infobox_and_ignores_paid_credentials(self):
        with patch.dict('os.environ',{'SERPER_API_KEY':'fixture','EXA_API_KEY':'fixture'},clear=True):
            search=Serper({ORG:RECORD},{ORG:{'claims':[]}},mode='free',public_search=True)
        self.assertEqual(search.keys,{})
        def transport(url,headers):
            if url.endswith('/robots.txt'):return response(url,'',404)
            return response(url,{'Infobox':{'content':[{'label':'Website','value':'<a href="https://brand.no/">Website</a>'}]}})
        leads=list(search.website_candidates(RECORD,Client(Budget(8,5),ORG,transport=transport)))
        self.assertIn(('https://brand.no/','public_search_lead'),leads)
        self.assertEqual(search.used,0)

    def test_career_cards_publish_observations_and_synthesis_without_invented_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Profile(ORG,'fixture',Path(directory))
            raw='<title>Careers</title><article><h2>Senior Engineer</h2><a href="/jobs/123456-engineer">Read more</a></article><a href="/stillinger/designer">Product Designer</a>'
            listed_roles(p,Page(response('https://fjordtest.no/careers',raw)),RECORD,'https://fjordtest.no/')
            claims=[c for c in p.data['claims'] if c['availability']=='available']
            self.assertEqual(len(claims),2)
            self.assertTrue(all(c['field']=='hiring_signal' and 'relationship' in c['value'] and 'date' not in c['value'] for c in claims))
            validate(p.finish(),Path(directory))

    def test_no_openings_foreign_owner_and_group_branch_cannot_lend_roles(self):
        roles='<title>Careers</title><a href="/stillinger/engineer">Senior Engineer</a><a href="/stillinger/designer">Product Designer</a>'
        for extra,home in [('No current vacancies','https://fjordtest.no/'),('Org.nr: 974760673','https://fjordtest.no/'),('','https://fjordtest.no/branches/fjord')]:
            with self.subTest(extra=extra,home=home),tempfile.TemporaryDirectory() as directory:
                p=Profile(ORG,'fixture',Path(directory));listed_roles(p,Page(response('https://fjordtest.no/careers',roles+extra)),RECORD,home)
                self.assertFalse(p.data['claims'])

    def test_live_org_bridge_requires_registered_contact_and_cites_both_sources(self):
        graph=response('https://www.wikidata.org/wiki/Special:EntityData/Q1.json',{'org':ORG,'website':'https://brand.no/'})
        record={**RECORD,'_live_identity_bridges':{'brand.no':{'response':graph,'span':graph.text}}}
        client=Client(Budget(8,5),ORG,transport=lambda u,h:response(u,{},404))
        page=Page(response('https://brand.no/','<title>Unrelated brand</title><p>Testveien 12, 5003 Bergen</p>'))
        self.assertEqual(verify_site(page,record,'public_seed',client,page.response.url)['method'],'live_exact_org_graph_and_registered_contact')
        foreign=Page(response('https://brand.no/','<p>Org.nr: 974760673</p><p>Testveien 12, 5003 Bergen</p>'))
        self.assertIsNone(verify_site(foreign,record,'public_seed',client,foreign.response.url))
