import json
import gzip
import hashlib
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from signalpost.net import Budget,Client
from signalpost.records import Profile,validate
from signalpost.site_discovery import name_urls,fetch_candidate
from signalpost.web import collect_site
from signalpost.web import Page
from scripts.build_observed_seeds import owners
from signalpost.seeds import load_seeds
from .helpers import ORG,RECORD,response


class SiteDiscovery(unittest.TestCase):
    def test_domain_plan_prioritises_brand_phrase_and_norwegian_spellings(self):
        urls=name_urls('VAKTMESTER SØR ANLEGG AS')
        self.assertLess(urls.index('https://vaktmestersor.no/'),urls.index('https://vaktmester.no/'))
        self.assertIn('https://vaktmestersoeranlegg.no/',urls)
        self.assertIn('https://nordlys.no/',name_urls('NORDLYS INVEST AS'))

    def test_www_recovery_checks_its_own_robots_and_preserves_tls(self):
        seen=[]
        def transport(url,headers):
            seen.append(url)
            if url.startswith('https://fjordtest.no/'):raise ssl.SSLCertVerificationError('hostname mismatch')
            if url.endswith('/robots.txt'):return response(url,'',404)
            return response(url,f'<footer>Fjord Test AS Org.nr {ORG}</footer>')
        with tempfile.TemporaryDirectory() as folder:
            p=Profile(ORG,'fixture',Path(folder));collect_site(p,Client(Budget(12,10),ORG,transport=transport),{**RECORD,'hjemmeside':'https://fjordtest.no/'},candidate_cap=1,page_cap=0)
            self.assertTrue(any(c['field']=='official_website' and c['value']=='https://www.fjordtest.no/' for c in p.data['claims']))
            self.assertIn('https://www.fjordtest.no/robots.txt',seen)
            self.assertFalse(any(u.startswith('http:') for u in seen))
            validate(p.finish(),p.captures)

    def test_explicit_robots_denial_is_not_bypassed_with_another_hostname(self):
        seen=[]
        def transport(url,headers):seen.append(url);return response(url,'User-agent: *\nDisallow: /')
        client=Client(Budget(12,10),ORG,transport=transport)
        result,attempts=fetch_candidate(client,'https://fjordtest.no/')
        self.assertEqual(result.state,'blocked');self.assertEqual(len(attempts),1)
        self.assertEqual(seen,['https://fjordtest.no/robots.txt'])

    def test_whole_owner_and_registered_leader_with_raw_proof_and_collision_vetoes(self):
        def exercise(body,collision=False):
            tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
            p=Profile(ORG,'fixture',root)
            identity=response('https://data.brreg.no/enhetsregisteret/api/enheter/'+ORG,RECORD)
            p.add('legal_name',RECORD['navn'],identity,json.dumps(RECORD['navn']))
            official=response(identity.url+'/roller',{'role':'DAGL','name':'Ada Fjord'})
            p.add('leader',{'name':'Ada Fjord','role_code':'DAGL','role':'Managing director'},official,official.text)
            def transport(url,headers):
                if url.endswith('/robots.txt'):return response(url,'',404)
                if 'data.brreg.no' in url:
                    rows=[RECORD]
                    if collision:rows.append({**RECORD,'organisasjonsnummer':'974760673'})
                    return response(url,{'page':{'totalPages':1},'_embedded':{'enheter':rows}})
                return response(url,body)
            record={**RECORD,'_website_seeds':['https://different-brand.no/']}
            collect_site(p,Client(Budget(20,10),ORG,transport=transport),record,candidate_cap=1,page_cap=0)
            claims=[c for c in p.data['claims'] if c['field']=='official_website' and c['availability']=='available']
            if claims:validate(p.finish(),root)
            return p,claims
        body='<title>Fjord Test AS - Consulting</title><footer>Fjord Test AS<p>Managing director: Ada Fjord</p></footer>'
        p,claims=exercise(body);self.assertEqual(len(claims),1)
        proofs=[e for e in p.data['evidence'] if e['id'] in claims[0]['evidence_ids']]
        self.assertTrue(any(e['source_url'].endswith('/roller') for e in proofs))
        self.assertTrue(any('<p>Managing director: Ada Fjord</p>'==e['claim_span'] for e in proofs))
        self.assertFalse(exercise(body,True)[1])
        self.assertFalse(exercise('<title>Fjord Test Group AS</title><footer>Ada Fjord</footer>')[1])
        self.assertFalse(exercise('<title>Fjord Test AS</title><footer>© Other Owner AS<p>Ada Fjord</p></footer>')[1])
        self.assertFalse(exercise('<title>Fjord Test AS</title><article>Our customer Ada Fjord</article>')[1])

    def test_registry_listed_placeholder_does_not_become_verified_company_site(self):
        with tempfile.TemporaryDirectory() as folder:
            def transport(url,headers):
                if url.endswith('/robots.txt'):return response(url,'',404)
                return response(url,'<title>Coming Soon</title><h1>Future home of something quite cool.</h1>')
            p=Profile(ORG,'fixture',Path(folder))
            collect_site(p,Client(Budget(12,10),ORG,transport=transport),{**RECORD,'hjemmeside':'https://fjordtest.no/'},candidate_cap=1,page_cap=0)
            self.assertFalse(any(c['field']=='official_website' and c['availability']=='available' for c in p.data['claims']))

    def test_reverse_index_uses_owned_identifiers_and_excludes_agency_customer_and_placeholder(self):
        universe={ORG:RECORD}
        def extract(raw):return owners(Page(response('https://brand.no/',raw)),universe)
        found=extract(f'<footer>Org.nr {ORG}</footer>')
        self.assertEqual(found[0]['organisation_number'],ORG)
        self.assertEqual(found[0]['url'],'https://brand.no/')
        self.assertEqual(len(found[0]['content_sha256']),64)
        self.assertFalse(extract(f'<footer>Website by Fjord Test AS Org.nr {ORG}</footer>'))
        self.assertFalse(extract(f'<footer>Customer: Fjord Test AS Org.nr {ORG}</footer>'))
        self.assertFalse(extract(f'<title>Coming Soon</title><footer>Org.nr {ORG}</footer>'))
        self.assertFalse(extract(f'<footer>Org.nr {ORG} Other owner Org.nr 974760673</footer>'))

    def test_own_observed_index_precedes_old_guesses_and_corruption_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base.gz';observed=root/'observed.gz'
            def write(path,row):
                raw=gzip.compress((json.dumps(row)+'\n').encode());path.write_bytes(raw)
                path.with_suffix('.manifest.json').write_text(json.dumps({'archive_sha256':hashlib.sha256(raw).hexdigest()}))
            write(base,{'organisation_number':ORG,'urls':['https://old-guess.no/']})
            write(observed,{'organisation_number':ORG,'urls':['https://real-brand.no/'],
                            'verification_pages':['https://real-brand.no/legal'],'provenance':{'https://real-brand.no/':[{'source_record':'https://real-brand.no/legal','content_sha256':'a'*64,'rule':'exact_owner_org_number'}]}})
            with patch('signalpost.seeds.DEFAULT',base),patch('signalpost.seeds.OBSERVED',observed):
                candidates,report=load_seeds({ORG},path=base)
                self.assertEqual(candidates[ORG],['https://real-brand.no/','https://old-guess.no/'])
                self.assertIn('https://real-brand.no/legal',report['verification_pages'][ORG])
                self.assertEqual(report['company_hints'][ORG]['provenance']['https://real-brand.no/'][0]['content_sha256'],'a'*64)
                observed.write_bytes(b'corrupt')
                candidates,report=load_seeds({ORG},path=base)
                self.assertEqual(candidates[ORG],['https://old-guess.no/'])
                self.assertIn('error',report['observed_index'])
