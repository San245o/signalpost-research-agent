"""Regression cases from live runs, the starter contract and public entry audits."""
import copy
import gzip
import hashlib
import json
import os
import tempfile
import unittest
from datetime import date,datetime,timezone
from pathlib import Path
from unittest.mock import patch
from signalpost.browser import needs_render
from signalpost.cache import ResponseCache
from signalpost.net import Budget,Client
from signalpost.nav import NavIndex
from signalpost.news import add_news
from signalpost.records import Profile,canonical,public_envelope,refresh,validate
from signalpost.registry import accounts,add_identity,workplaces,registry_events
from signalpost.runner import run_batch,finalize_captures
from signalpost.search import Serper,query_plan
from signalpost.seeds import load_seeds
from signalpost.site_crawl import feed_posts,wordpress_posts
from signalpost.site_crawl import listed_roles,socials
from signalpost.web import Page,verify_site,candidates,namesake,canonical_social,stated_numbers
from .helpers import ORG,RECORD,response,official_transport


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.profile=Profile(ORG,'release',self.root/'sources')
    def tearDown(self):self.tmp.cleanup()
    def client(self,transport,**options):
        return Client(Budget(100,10),ORG,transport=transport,**options)
    def page(self,body,url='https://fjordtest.no/news/factory'):
        return Page(response(url,body,headers={'content-type':'text/html; charset=utf-8'}))
    def available(self,field):
        return [c for c in self.profile.data['claims'] if c['field']==field and c['availability']=='available']
    def seed_map(self):
        path=self.root/'seeds.jsonl.gz'
        raw=gzip.compress((json.dumps({'organisation_number':ORG,'urls':['https://brand-fjord.no/']})+'\n').encode(),mtime=0)
        path.write_bytes(raw)
        path.with_suffix('.manifest.json').write_text(json.dumps({'archive_sha256':hashlib.sha256(raw).hexdigest()}))
        return path

    def test_bundled_map_is_checked_and_connected_to_keyless_runner(self):
        path=self.seed_map()
        def transport(url,headers):
            if 'data.brreg.no' in url:return official_transport(url,headers)
            if url.endswith('/robots.txt'):return response(url,'',404)
            if 'brand-fjord.no' in url:return response(url,f'<footer>Fjord Test AS Org {ORG}</footer>')
            return response(url,'',404)
        rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',transport=transport,nav_pages=0,
                             search_mode='none',candidate_map=path,candidate_cap=1,page_cap=0)
        self.assertEqual(report['candidate_map']['loaded_companies'],1)
        self.assertTrue(any(c['field']=='official_website' and c['value']=='https://brand-fjord.no/' for c in rows[0]['claims']))
        validate(rows[0],self.root/'run/sources')
    def test_corrupt_candidate_map_falls_back_without_a_batch_crash(self):
        path=self.seed_map();path.write_bytes(b'corrupt')
        records,report=load_seeds({ORG},path=path)
        self.assertEqual(records,{})
        self.assertIn('mismatch',report['error'])
    def test_rejected_candidates_are_only_in_separate_attempt_log(self):
        def transport(url,headers):
            if 'data.brreg.no' in url:return official_transport(url,headers)
            if url.endswith('/robots.txt'):return response(url,'',404)
            return response(url,'<footer>Other owner AS Org 974760673</footer>')
        rows,_=run_batch([{'organisation_number':ORG}],self.root/'run',transport=transport,nav_pages=0,
                         search_mode='none',candidate_map=self.seed_map(),candidate_cap=1)
        self.assertNotIn('website_discovery',rows[0])
        self.assertNotIn('brand-fjord.no',canonical(rows[0]))
        self.assertIn('brand-fjord.no',(self.root/'run/discovery-attempts.jsonl').read_text())
    def test_unpublished_error_text_is_scrubbed(self):
        self.profile.error('website','Rejected https://wrong-owner.no/page and wrong-owner.no')
        out=public_envelope(self.profile.finish())
        self.assertNotIn('wrong-owner.no',canonical(out))
    def test_response_bytes_not_python_span_are_hashed(self):
        r=response('https://data.brreg.no/',{'a':'Fjord','b':[0,12]})
        self.profile.add('legal_name','Fjord',r,'"Fjord"')
        out=self.profile.finish();e=out['evidence'][0]
        self.assertEqual(e['content_sha256'],hashlib.sha256(r.body).hexdigest())
        self.assertNotEqual(e['content_sha256'],hashlib.sha256(e['claim_span'].encode()).hexdigest())
        self.assertIn('extraction_method',e);validate(out,self.root/'sources')
        out['evidence'][0]['claim_span']='fabricated quote'
        with self.assertRaises(ValueError):validate(out,self.root/'sources')
    def test_legacy_field_names_and_missing_summary_cannot_ship(self):
        with self.assertRaises(ValueError):self.profile.add('active_job_count',None,state='not_available')
        with self.assertRaises(ValueError):self.profile.add('registered_workplace',None,state='not_available')
        out=self.profile.finish();out.pop('summary')
        with self.assertRaises(ValueError):validate(out)
    def test_registered_full_legal_name_does_not_fail_broad_registry_pagination(self):
        record={**RECORD,'navn':'NORSK TIPPING AS'}
        page=self.page('<footer>Norsk Tipping AS</footer>','https://norsk-tipping.no/')
        client=self.client(lambda u,h:response(u,{'page':{'totalPages':54}}))
        self.assertIsNotNone(verify_site(page,record,'registered',client,page.response.url))
        self.assertEqual(client.budget.used,0)
    def test_registered_legal_form_confusion_is_not_full_legal_owner_proof(self):
        record={**RECORD,'navn':'FJORD TEST AS'}
        page=self.page('<footer>Fjord Test IKS</footer>','https://fjordtest.no/')
        client=self.client(lambda u,h:response(u,{'page':{'totalPages':54}}))
        self.assertIsNone(verify_site(page,record,'registered',client,page.response.url))
    def test_idn_name_candidates_are_punycode_encoded(self):
        urls=[u for u,_ in candidates({**RECORD,'navn':'SCHRØDER EIENDOM AS'})]
        self.assertTrue(any('xn--' in u for u in urls))
    def test_leader_org_and_former_name_recovery_are_planned(self):
        record={**RECORD,'_leaders':['Ada Fjord'],'historiskeNavn':[{'navn':'Old Brand AS','tilDato':'2025-01-01'}]}
        self.assertEqual([tier for tier,_,_ in query_plan(record)],['legal_name','leader','org_number','former_name'])
    def test_unused_paid_allowance_is_available_to_next_company(self):
        other='997770234';r2={**RECORD,'organisasjonsnummer':other}
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake'},clear=True):
            search=Serper({ORG:RECORD,other:r2},{ORG:self.profile.finish(),other:self.profile.finish()},credits=1)
        self.assertTrue(search._reserve(other));self.assertFalse(search._reserve(ORG))
    def test_failed_verification_resumes_next_search_round(self):
        record={**RECORD,'_leaders':['Ada Fjord']};queries=[]
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake'},clear=True):search=Serper({ORG:record},{ORG:self.profile.finish()},credits=3,website_rounds=3)
        def transport(url,headers,method='GET',body=None):
            payload=json.loads(body);queries.append(payload['q'])
            return response(url,{'organic':[{'title':'Fjord Test AS','link':'https://'+('first.no' if len(queries)==1 else 'second.no')}]})
        it=search.website_candidates(record,self.client(transport))
        self.assertEqual(next(it)[0],'https://first.no/')
        self.assertEqual(next(it)[0],'https://second.no/')
        self.assertIn('Ada Fjord',queries[1])
    def test_year_specific_account_capture_is_used_and_group_is_rejected(self):
        seen=[]
        def transport(url,headers):seen.append(url);return official_transport(url,headers)
        accounts(self.profile,self.client(transport))
        revenue=self.available('revenue')[0]
        evidence=next(e for e in self.profile.data['evidence'] if e['id'] in revenue['evidence_ids'])
        self.assertIn('%C3%A5r=2025',evidence['source_url'])
        self.assertIn('regnskapstype=SELSKAP',evidence['source_url'])
    def test_workplace_pagination_keeps_parent_checks(self):
        def transport(url,headers):
            second='page=1' in url
            row={'organisasjonsnummer':'973861883' if second else '974760673','overordnetEnhet':ORG,'navn':'Fjord branch'}
            return response(url,{'_embedded':{'underenheter':[row]},'page':{'number':int(second),'totalPages':2}})
        units=workplaces(self.profile,self.client(transport))
        self.assertEqual(len(units),2);self.assertTrue(self.profile.data['workplace_scan']['complete'])
    def test_registry_events_never_adopt_a_different_company(self):
        events=[{'organisasjonsnummer':ORG,'dato':'2026-10-01','endringstype':'Endring'},
                {'organisasjonsnummer':'974760673','dato':'2026-11-01','endringstype':'Endring'}]
        registry_events(self.profile,self.client(lambda u,h:response(u,{'_embedded':{'oppdaterteEnheter':events}})))
        self.assertEqual(len(self.available('dated_activity')),1)
    def test_html_norwegian_publication_date_stays_with_its_article(self):
        page=self.page('<article><h1>Fjord Test opens a new factory</h1><span class="published">1. november 2026</span></article>')
        add_news(self.profile,page,date(2026,11,13),RECORD)
        self.assertEqual(self.available('dated_activity')[0]['value']['date'],'2026-11-01')
    def test_modified_date_and_footer_date_are_not_publication_dates(self):
        for body in ['<article><h1>Fjord Test new factory</h1><time class="modified" datetime="2026-11-01"></time></article>',
                     '<h1>Fjord Test new factory</h1><footer><time datetime="2026-11-01"></time></footer>']:
            add_news(self.profile,self.page(body),date(2026,11,13),RECORD)
        self.assertEqual(self.available('dated_activity'),[])
    def test_declared_feed_keeps_real_item_span_and_rejects_updated_only(self):
        raw='<feed><entry><title>Fjord Test new factory</title><published>2026-11-01</published><link href="https://fjordtest.no/news/factory"/></entry><entry><title>Fjord Test other factory</title><updated>2026-11-01</updated><link href="https://fjordtest.no/news/other"/></entry></feed>'
        feed_posts(self.profile,response('https://fjordtest.no/feed',raw),'https://fjordtest.no/',date(2026,11,13),None)
        self.assertEqual(len(self.available('dated_activity')),1)
        validate(self.profile.finish(),self.root/'sources')
    def test_wordpress_comment_feed_is_not_company_news(self):
        raw='<rss><channel><item><title>Kommentar til Hei, verden! av En WordPress-kommentator</title><link>https://fjordtest.no/2021/11/16/hei-verden/#comment-2</link><pubDate>Thu, 03 Sep 2026 01:30:37 +0000</pubDate><description>Fjord Test AS</description></item></channel></rss>'
        feed_posts(self.profile,response('https://fjordtest.no/comments/feed/',raw),'https://fjordtest.no/',date(2026,11,13),None,RECORD)
        self.assertEqual(self.available('dated_activity'),[])
        feed_posts(self.profile,response('https://fjordtest.no/feed/',raw),'https://fjordtest.no/',date(2026,11,13),None,RECORD)
        self.assertEqual(self.available('dated_activity'),[])
    def test_norwegian_hello_world_template_is_not_news(self):
        from signalpost.news import about_company
        self.assertFalse(about_company('Hei, verden!','Fjord Test AS',RECORD,'https://fjordtest.no/'))
    def test_successful_disk_cache_retains_acquisition_time_and_rechecks_hash(self):
        cache=ResponseCache(self.root/'cache');r=response('https://fjordtest.no/','body')
        cache.put('test',r);cached=cache.get('test',10**9)
        self.assertEqual(cached.retrieved_at,r.retrieved_at)
        (self.root/'cache'/(r.digest+'.bin')).write_bytes(b'bad')
        self.assertIsNone(cache.get('test',10**9))
    def test_failed_refresh_retains_last_value_with_original_evidence(self):
        r=response('https://fjordtest.no/','Fjord Test')
        self.profile.add('legal_name','Fjord Test',r,r.text)
        first=self.profile.finish();refresh(first,self.root/'history',self.root/'sources')
        failed=Profile(ORG,'next',self.root/'new-sources').finish(status='failed')
        refresh(failed,self.root/'history',self.root/'new-sources')
        old=next(c for c in failed['claims'] if c['field']=='legal_name')
        self.assertEqual(old['value'],'Fjord Test');self.assertTrue(old['stale'])
        self.assertEqual(old['last_observed_at'],r.retrieved_at)
        validate(failed,self.root/'new-sources');self.assertEqual(failed['changes'],[])
    def test_material_refresh_change_cites_both_versions(self):
        r=response('https://data.brreg.no/','Old Name')
        self.profile.add('legal_name','Old Name',r,r.text);refresh(self.profile.finish(),self.root/'history',self.root/'sources')
        p=Profile(ORG,'next',self.root/'new');r2=response('https://data.brreg.no/','New Name');p.add('legal_name','New Name',r2,r2.text)
        out=refresh(p.finish(),self.root/'history',self.root/'new')
        self.assertEqual(out['changes'][0]['previous_value'],'Old Name')
        self.assertTrue(out['changes'][0]['previous_evidence_ids']);self.assertTrue(out['changes'][0]['evidence_ids'])
        validate(out,self.root/'new')
    def test_js_shell_detection_does_not_render_a_complete_static_page(self):
        self.assertTrue(needs_render(self.page('<div id="root"></div><script src="/app.js"></script>')))
        self.assertFalse(needs_render(self.page('<article>'+('A real company page. '*30)+'</article><script src="/app.js"></script>')))
    def test_news_cannot_match_the_company_only_in_a_footer(self):
        page=self.page('<article><h1>How the economy changed this year</h1><time datetime="2026-11-01"></time><footer>Fjord Test AS</footer></article>')
        add_news(self.profile,page,date(2026,11,13),RECORD)
        self.assertEqual(self.available('dated_activity'),[])
    def test_cms_static_about_page_is_not_news(self):
        page=self.page('<html><head><meta property="og:type" content="article"><meta property="article:published_time" content="2026-11-01"></head><body class="page-id-42"><h1>About Fjord Test AS</h1></body></html>','https://fjordtest.no/information/')
        add_news(self.profile,page,date(2026,11,13),RECORD)
        self.assertEqual(self.available('dated_activity'),[])
    def test_role_link_is_not_claimed_as_an_open_job(self):
        page=self.page('<h1>Careers</h1><a href="/jobs/engineer">Software Engineer</a>','https://fjordtest.no/careers')
        listed_roles(self.profile,page,RECORD)
        self.assertEqual(self.available('hiring_signal'),[])
        self.assertEqual(len(self.profile.data['crawl_attempts']),1)
        self.assertEqual(self.available('careers_listing'),[])
    def test_same_post_from_two_extractors_is_one_claim(self):
        r=response('https://fjordtest.no/news/factory','Fjord Test new factory')
        for title in ['Fjord Test new factory','Fjord Test new factory opens']:
            self.profile.add('dated_activity',{'title':title,'date':'2026-11-01','url':r.url,'publisher_type':'company_published'},r,r.text,'company_owned',period='2026-11-01')
        self.assertEqual(len(self.available('dated_activity')),1)
    def test_regional_social_account_does_not_promote_global_account(self):
        page=self.page('<a href="https://facebook.com/fjordtest">Global</a><a href="https://facebook.com/fjordtestnorge">Norway</a>','https://fjordtest.no/')
        socials(self.profile,page,RECORD)
        values=[c['value'] for c in self.available('company_profile')]
        self.assertEqual(values,['https://facebook.com/fjordtestnorge'])
    def test_shared_company_page_does_not_lend_social_accounts(self):
        page=self.page('<footer>Other AS Org 974760673 <a href="https://facebook.com/fjordtest">Facebook</a></footer>','https://fjordtest.no/')
        socials(self.profile,page,RECORD)
        self.assertEqual(self.available('company_profile'),[])
    def test_incomplete_nav_refresh_keeps_prior_complete_index(self):
        state=self.root/'nav.json'
        old={'complete':True,'scan_started_at':'2026-11-01T00:00:00+00:00','active':{'a':{'id':'a','url':'https://pam-stilling-feed.nav.no/api/v1/feedentry/a','name':'fjord test'}}}
        state.write_text(json.dumps(old));before=state.read_bytes()
        def transport(url,headers):
            if url.endswith('publicToken'):return response(url,'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwdWJsaWMifQ.c2lnbmF0dXJl')
            if url.endswith('/p2'):return response(url,'',503)
            return response(url,{'items':[{'id':'a','_feed_entry':{'status':'INACTIVE'}}],'next_url':'/p2'})
        nav=NavIndex(state_path=state);nav.build(self.client(transport),max_pages=2)
        self.assertFalse(nav.complete);self.assertIn('a',nav.active);self.assertEqual(before,state.read_bytes())
    def test_careers_observation_date_does_not_create_false_refresh_change(self):
        def create(run,day):
            p=Profile(ORG,run,self.root/'sources');r=response('https://fjordtest.no/careers','Software Engineer')
            p.add('careers_listing',{'title':'Software Engineer','url':'https://fjordtest.no/jobs/a','observed_at':day},r,r.text,'company_owned')
            return p.finish()
        refresh(create('one','2026-11-01'),self.root/'history')
        second=refresh(create('two','2026-11-02'),self.root/'history')
        self.assertTrue(second['refresh']['unchanged']);self.assertEqual(second['changes'],[])
    def test_optional_browser_renders_script_through_the_same_client(self):
        import importlib.util
        if importlib.util.find_spec('playwright') is None:self.skipTest('optional Playwright not installed')
        from signalpost.browser import BrowserRenderer
        page=self.page('<div id="root"></div><script>document.getElementById("root").innerHTML="<footer>Fjord Test AS</footer>";</script>','https://fjordtest.no/')
        client=self.client(lambda u,h:response(u,'',404),captures=self.root/'sources')
        renderer=BrowserRenderer(max_renders=1)
        try:
            result=renderer.render_if_needed(page,client)
            if renderer.unavailable:self.skipTest(renderer.unavailable)
            self.assertIsNotNone(result)
            self.assertIn('<footer>Fjord Test AS</footer>',result.text)
            self.assertEqual(result.derived_from,page.response.digest)
            self.assertEqual(client.budget.used,0)
            self.assertEqual(result.extraction_method,'browser_rendered_dom')
        finally:renderer.close()
    def test_news_and_places_share_the_same_serper_allowance(self):
        with patch.dict(os.environ,{'SERPER_API_KEY':'fake'},clear=True):
            search=Serper({ORG:RECORD},{ORG:self.profile.finish()},credits=2,per_company=4)
        calls=[]
        def transport(url,headers,method='GET',body=None):
            calls.append(url)
            return response(url,{'news':[]} if url.endswith('/news') else {'places':[{'title':'Fjord Test AS','address':'Testveien 12, 5003 Bergen','rating':4.9}]})
        search.enrich(self.profile,RECORD,self.client(transport))
        self.assertEqual(search.used,2);self.assertEqual(len(calls),2)
        self.assertTrue(self.profile.data['external_discovery'])
        self.assertFalse(any(c['availability']=='available' for c in self.profile.data['claims']))
    def test_independent_news_requires_a_fetched_article_and_exact_subject(self):
        from signalpost.external import independent_article
        item={'@type':'NewsArticle','headline':'Fjord Test AS opens factory','datePublished':'2026-11-01'}
        body='<script type="application/ld+json">'+json.dumps(item)+'</script><article>Fjord Test AS Org '+ORG+'</article>'
        independent_article(self.profile,response('https://publisher.no/news/fjord',body),RECORD)
        self.assertEqual(len(self.available('dated_activity')),1)
        self.assertEqual(self.available('dated_activity')[0]['value']['publisher_type'],'independent_reporting')
    def test_recording_first_and_last_observed_does_not_duplicate_evidence(self):
        r=response('https://fjordtest.no/','Fjord Test AS')
        self.profile.add('legal_name','Fjord Test AS',r,r.text)
        out=self.profile.finish();resumed=Profile.resume(out,self.root/'sources')
        resumed.add('site_description','Fjord Test AS',r,r.text)
        self.assertEqual(len(resumed.data['evidence']),1)
    def test_contact_paragraph_org_number_does_not_require_a_footer_tag(self):
        page=self.page(f'<title>Fjord Test AS</title><div class="module text"><h3>Fjord Test AS</h3><p>Testveien 12<br>Org.nr: {ORG}</p></div>','https://fjordtest.no/')
        result=verify_site(page,RECORD,'name_domain_lead',self.client(lambda u,h:response(u,{},404)),page.response.url)
        self.assertIsNotNone(result);self.assertIn('identifier',result['method'])
    def test_norwegian_vat_identifier_is_legal_ownership_evidence(self):
        self.assertEqual(stated_numbers('Foretaksregisteret: NO'+ORG+'MVA'),{ORG})
        page=self.page(f'<title>Fjord Test AS</title><p>Foretaksregisteret:</p><p>NO{ORG}MVA</p>','https://fjordtest.no/')
        self.assertIsNotNone(verify_site(page,RECORD,'name_domain_lead',self.client(lambda u,h:response(u,{},404)),page.response.url))
    def test_namesake_lookup_uses_distinctive_token_and_exact_name_filter(self):
        from urllib.parse import parse_qs,urlsplit
        seen=[];record={**RECORD,'navn':'STUDIO MORIOKA AS'}
        def transport(url,headers):
            seen.append(parse_qs(urlsplit(url).query)['navn'][0])
            return response(url,{'page':{'totalPages':1},'_embedded':{'enheter':[{'navn':'MORIOKA DESIGN AS','organisasjonsnummer':'974760673'},{'navn':record['navn'],'organisasjonsnummer':ORG}]}})
        self.assertFalse(namesake(record,self.client(transport)));self.assertEqual(seen,['morioka'])
    def test_namesake_on_second_page_cannot_be_missed(self):
        from urllib.parse import parse_qs,urlsplit
        def transport(url,headers):
            page=int(parse_qs(urlsplit(url).query)['page'][0])
            rows=[{'navn':RECORD['navn'],'organisasjonsnummer':'974760673'}] if page else []
            return response(url,{'page':{'totalPages':2},'_embedded':{'enheter':rows}})
        self.assertTrue(namesake(RECORD,self.client(transport)))
    def test_malformed_social_urls_do_not_abort_company_extraction(self):
        self.assertIsNone(canonical_social('https://['))
        page=self.page('<a href="https://[">bad link</a><a href="/contact">Contact</a>')
        self.assertEqual(len(list(page.links())),1)
    def test_facebook_people_profile_keeps_company_handle(self):
        url='https://www.facebook.com/people/Fjord-Test-AS/100090487814108/'
        self.assertEqual(canonical_social(url),('https://facebook.com/people/Fjord-Test-AS/100090487814108','Fjord-Test-AS'))
        socials(self.profile,self.page('<a href="'+url+'">Facebook</a>','https://fjordtest.no/'),RECORD)
        self.assertEqual(len(self.available('company_profile')),1)
        self.assertIsNone(canonical_social('https://facebook.com/people/Fjord-Test-AS/posts'))
    def test_nav_branch_lead_requires_exact_registered_employer_number(self):
        nav=NavIndex();nav.token='public';nav.active={'a':{'id':'a','name':'fjord test barnehage','url':'https://pam-stilling-feed.nav.no/api/v1/feedentry/a'}}
        def ad(org):return {'ad_content':{'employer':{'orgnr':org},'status':'ACTIVE','published':datetime.now(timezone.utc).date().isoformat(),'expires':datetime.now(timezone.utc).date().isoformat(),'title':'Pedagog','uuid':'abc-123'}}
        nav.add_jobs(self.profile,RECORD,self.client(lambda u,h:response(u,ad('974760673'))))
        self.assertEqual(self.available('hiring_signal'),[])
        nav.add_jobs(self.profile,RECORD,self.client(lambda u,h:response(u,ad(ORG))))
        self.assertEqual(len(self.available('hiring_signal')),1)
    def test_live_nav_top_level_status_is_authoritative_and_cited(self):
        nav=NavIndex();nav.token='public';nav.active={'a':{'id':'a','name':'fjord test','url':'https://pam-stilling-feed.nav.no/api/v1/feedentry/a'}}
        data={'uuid':'a','status':'ACTIVE','ad_content':{'employer':{'orgnr':ORG},'published':datetime.now(timezone.utc).date().isoformat(),'expires':datetime.now(timezone.utc).date().isoformat(),'title':'Engineer'}}
        nav.add_jobs(self.profile,RECORD,self.client(lambda u,h:response(u,data)))
        self.assertEqual(len(self.available('hiring_signal')),1)
        self.assertIn('ACTIVE',self.profile.data['evidence'][0]['claim_span'])
        other=Profile(ORG,'inactive',self.root/'other');data['status']='INACTIVE';data['ad_content']['status']='ACTIVE'
        nav.add_jobs(other,RECORD,self.client(lambda u,h:response(u,data)))
        self.assertFalse(any(c['availability']=='available' for c in other.data['claims']))
    def test_failed_capture_readback_is_quarantined_and_never_cited(self):
        client=self.client(lambda u,h:response(u,'body'),captures=self.root/'sources')
        from signalpost.cache import write_bytes
        def bad_write(path,body):write_bytes(path,b'corrupted')
        with patch('signalpost.cache.write_bytes',side_effect=bad_write):
            r=client.get('https://data.brreg.no/test',robots=False,retries=0)
        self.assertNotEqual(r.status,200)
        self.assertEqual(list((self.root/'sources').glob('*.bin')),[])
        self.assertEqual(len(list((self.root/'sources').glob('*.rejected'))),1)
    def test_final_integrity_check_preserves_uncited_corruption_for_diagnosis(self):
        directory=self.root/'sources';directory.mkdir();(directory/('0'*64+'.bin')).write_bytes(b'bad')
        results={0:self.profile.finish()};items=[{'organisation_number':ORG}]
        report=finalize_captures(results,directory,items,'test',Budget())
        self.assertEqual(report['failed_cited_companies'],0)
        self.assertEqual(len(report['rejected']),1)
        self.assertTrue((directory/('0'*64+'.rejected')).exists())
    def test_final_integrity_check_fails_a_company_with_corrupted_cited_evidence(self):
        r=response('https://fjordtest.no/','Fjord Test AS')
        self.profile.add('legal_name','Fjord Test AS',r,r.text)
        (self.root/'sources'/(r.digest+'.bin')).write_bytes(b'bad')
        results={0:self.profile.finish()};items=[{'organisation_number':ORG}]
        report=finalize_captures(results,self.root/'sources',items,'test',Budget())
        self.assertEqual(report['failed_cited_companies'],1)
        self.assertEqual(results[0]['run']['terminal_status'],'failed')
        self.assertFalse(any(c['availability']=='available' for c in results[0]['claims']))
    def test_late_shared_nav_lead_is_revisited_after_website_phase(self):
        original=NavIndex.add_jobs;calls=[]
        def build(nav,client,max_pages):
            nav.token='public';nav.finished.set()
        def check(nav,profile,record,client):
            result=original(nav,profile,record,client);calls.append(1)
            nav.active={'late':{'id':'late','name':'fjord test','url':'https://pam-stilling-feed.nav.no/api/v1/feedentry/late'}}
            return result
        def transport(url,headers):
            if url.endswith('/late'):
                return response(url,{'ad_content':{'employer':{'orgnr':ORG},'status':'ACTIVE','published':datetime.now(timezone.utc).date().isoformat(),'expires':datetime.now(timezone.utc).date().isoformat(),'title':'Engineer','uuid':'late'}})
            return official_transport(url,headers)
        with patch.object(NavIndex,'build',build),patch.object(NavIndex,'add_jobs',check),patch('signalpost.runner.collect_site'):
            rows,_=run_batch([{'organisation_number':ORG}],self.root/'late',transport=transport,search_mode='none',candidate_map=None)
        self.assertEqual(len(calls),2)
        self.assertTrue(any(c['field']=='hiring_signal' and c['availability']=='available' for c in rows[0]['claims']))
    def test_later_crawl_error_does_not_erase_a_verified_website(self):
        def partial(profile,client,record,**kwargs):
            r=response('https://fjordtest.no/',f'Fjord Test AS Org {ORG}')
            profile.add('official_website',r.url,r,r.text,'company_owned');raise ValueError('bad later page')
        with patch('signalpost.runner.collect_site',side_effect=partial):
            rows,_=run_batch([{'organisation_number':ORG}],self.root/'run',transport=official_transport,
                             nav_pages=0,search_mode='none',candidate_map=None)
        self.assertTrue(any(c['field']=='official_website' and c['availability']=='available' for c in rows[0]['claims']))
        self.assertTrue(rows[0]['errors'])


if __name__=='__main__':unittest.main()
