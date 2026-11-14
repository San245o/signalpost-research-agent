from __future__ import annotations
import copy
import gzip
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from signalpost.inputs import load_manifest, load_snapshot
from signalpost.net import Budget, Client, clean_url, public_addresses
from signalpost.records import Profile, refresh, validate, atomic_write
from signalpost.registry import accounts, roles, workplaces, json_document, add_identity
from signalpost.runner import run_batch
from signalpost.search import Serper, priority
from signalpost.web import Page, ownership, collect_site, add_article, add_jobs, canonical_social, directory_url
from signalpost.viewer import render
from .helpers import ORG, RECORD, response, official_transport


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = Profile(ORG, "test", self.root / "sources")

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, transport, requests=100):
        return Client(Budget(requests, 10), ORG, transport=transport)


class InputTests(Workspace):
    def test_atomic_output_retries_transient_windows_lock_without_losing_prior_file(self):
        target=self.root/'output.jsonl';target.write_text('prior',encoding='utf-8')
        replace=Path.replace;attempts=[]
        def locked(temp,destination):
            attempts.append(temp)
            self.assertEqual(target.read_text(encoding='utf-8'),'prior')
            if len(attempts)<3:raise PermissionError('Temporary reader lock')
            return replace(temp,destination)
        with patch.object(Path,'replace',locked),patch('signalpost.records.time.sleep'):
            atomic_write(target,'complete replacement')
        self.assertEqual(len(attempts),3)
        self.assertEqual(target.read_text(encoding='utf-8'),'complete replacement')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_atomic_output_permanent_denial_is_reported_and_prior_file_survives(self):
        target=self.root/'output.jsonl';target.write_text('prior',encoding='utf-8')
        with patch.object(Path,'replace',side_effect=PermissionError('Persistent denial')),patch('signalpost.records.time.sleep'):
            with self.assertRaises(PermissionError):atomic_write(target,'replacement')
        self.assertEqual(target.read_text(encoding='utf-8'),'prior')

    def test_manifest_formats(self):
        samples = {
            "plain.txt": ORG + "\n123456789\n",
            "lines.jsonl": json.dumps({"orgnr": ORG}) + '\n"123456789"\n',
            "array.json": json.dumps([ORG, "123456789"]),
            "wrapped.json": json.dumps({"companies": [{"organisation_number": ORG}, "123456789"]}),
            "comma.csv": "organisation_number,name\n" + ORG + ",Fjord\n123456789,Other\n",
            "semi.csv": "organisasjonsnummer;navn\n" + ORG + ";Fjord\n123456789;Other\n"}
        for filename, body in samples.items():
            with self.subTest(filename=filename):
                p = self.root / filename
                p.write_text(body, encoding="utf-8")
                self.assertEqual([r["organisation_number"] for r in load_manifest(p)], [ORG, "123456789"])

    def test_gzip_and_bom(self):
        p = self.root / "batch.gz"
        with gzip.open(p, "wt", encoding="utf-8-sig") as f:
            f.write(ORG + "\n")
        self.assertTrue(load_manifest(p)[0]["input_valid"])

    def test_invalid_and_duplicate_inputs_are_not_silently_dropped(self):
        p = self.root / "batch.json"
        p.write_text(json.dumps([ORG, "bad", ORG]), encoding="utf-8")
        items = load_manifest(p)
        self.assertEqual(len(items), 3)
        self.assertFalse(items[1]["input_valid"])

    def test_optional_missing_snapshot_is_not_fatal(self):
        seeds, warnings = load_snapshot(self.root / "missing.csv.gz", {ORG})
        self.assertEqual(seeds, {})
        self.assertEqual(len(warnings), 1)

    def test_snapshot_can_be_partial(self):
        p = self.root / "universe.jsonl.gz"
        raw = json.dumps({"organisation_number": ORG, "name": "FJORD TEST AS", "employees": 0}) + "\n"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(raw)
        seeds, warnings = load_snapshot(p, {ORG, "123456789"})
        self.assertEqual(set(seeds), {ORG})
        self.assertEqual(seeds[ORG][0]["antallAnsatte"], 0)
        self.assertEqual(seeds[ORG][1].text, gzip.decompress(p.read_bytes()).decode())

    def test_csv_snapshot_preserves_multiline_row(self):
        p = self.root / "snapshot.csv"
        body = 'organisasjonsnummer;navn;organisasjonsform.kode\n' + ORG + ';"FJORD\nTEST AS";AS\n'
        p.write_text(body, encoding="utf-8")
        seeds, warnings = load_snapshot(p, {ORG})
        self.assertEqual(warnings, [])
        self.assertIn('"FJORD\r\nTEST AS"', seeds[ORG][1].text)


class NetworkTests(Workspace):
    def test_rejects_credentials_and_non_http_urls(self):
        for url in ("https://user:secret@example.org/", "ftp://example.org/", "file:///tmp/a", "https://example.org:9999/"):
            self.assertEqual(clean_url(url), "")

    def test_private_ipv4_ipv6_and_mapped_addresses_are_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[(None, None, None, None, (ip, 443))]):
                with self.assertRaises(ValueError):
                    public_addresses("example.org", 443)

    def test_redirect_hops_are_counted(self):
        def transport(url, headers):
            return response(url, "", 302, {"location": "https://example.org/target"}) if url.endswith("/start") else response(url, "ok")
        client = self.client(transport)
        result = client.get("https://example.org/start", robots=False)
        self.assertEqual(result.status, 200)
        self.assertEqual(client.budget.used, 2)

    def test_every_redirect_checks_private_address(self):
        client = self.client(lambda u, h: response(u, "", 302, {"location": "http://127.0.0.1/"}))
        self.assertEqual(client.get("https://example.org/start", robots=False).state, "blocked")
        self.assertEqual(client.budget.used, 1)

    def test_redirect_does_not_forward_authorization(self):
        seen = []
        def transport(url, headers):
            seen.append(dict(headers))
            return response(url, "", 302, {"location": "https://second.org/"}) if "example.org" in url else response(url, "ok")
        client = self.client(transport)
        client.get("https://example.org/", robots=False, headers={"Authorization": "Bearer test"})
        self.assertNotIn("Authorization", seen[1])

    def test_robots_denial_prevents_page_fetch(self):
        seen = []
        def transport(url, headers):
            seen.append(url)
            return response(url, "User-agent: *\nDisallow: /\n", headers={"content-type": "text/plain"})
        result = self.client(transport).get("https://example.org/private")
        self.assertEqual(result.state, "blocked")
        self.assertEqual(len(seen), 1)

    def test_missing_robots_allows_but_unreachable_robots_does_not(self):
        client = self.client(lambda u,h: response(u, "", 404) if u.endswith("robots.txt") else response(u, "ok"))
        self.assertEqual(client.get("https://example.org/").status, 200)
        client = self.client(lambda u,h: response(u, "", 503))
        self.assertEqual(client.get("https://example.org/").state, "blocked")

    def test_request_cap_is_atomic_under_concurrency(self):
        budget = Budget(17, 10)
        def work(n):
            client = Client(budget, str(n), transport=lambda u,h: response(u, "ok"))
            return client.get("https://example.org/" + str(n), robots=False)
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(work, range(100)))
        self.assertEqual(budget.used, 17)

    def test_retry_is_counted(self):
        calls = []
        def transport(url, headers):
            calls.append(url)
            return response(url, "", 503, {"retry-after":"0"}) if len(calls) == 1 else response(url, "ok")
        client = self.client(transport)
        self.assertEqual(client.get("https://example.org/", robots=False).status, 200)
        self.assertEqual(client.budget.used, 2)


class EvidenceAndRegistryTests(Workspace):
    def test_unspecified_industry_is_unknown(self):
        record=copy.deepcopy(RECORD)
        record['naeringskode1']={'kode':'00.000','beskrivelse':'Uoppgitt'}
        add_identity(self.profile,record,response('https://data.brreg.no/',record))
        claim=next(c for c in self.profile.data['claims'] if c['field']=='industry')
        self.assertEqual(claim['availability'],'not_available')

    def test_tampered_existing_capture_is_refused(self):
        r=response('https://example.org','Fjord')
        self.profile.captures.mkdir(parents=True)
        (self.profile.captures/(r.digest+'.bin')).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.profile.add('legal_name','Fjord',r,'Fjord')

    def test_json_path_spans_are_verbatim(self):
        raw = ' {"name": "Fj\\u00f8rd", "values": [0, -12.5, null, true]} '
        r = response("https://example.org/", raw)
        data, spans = json_document(r)
        self.assertEqual(data["name"], "Fjørd")
        self.assertEqual(spans[("name",)], '"Fj\\u00f8rd"')
        self.assertEqual(spans[("values",1)], "-12.5")

    def test_fabricated_span_is_rejected(self):
        with self.assertRaises(ValueError):
            self.profile.add("legal_name", "Fjord", response("https://example.org", "Real name"), "Fjord")

    def test_hash_and_captured_bytes_match(self):
        r = response("https://example.org", "Fjord")
        self.profile.add("legal_name", "Fjord", r, "Fjord")
        env = self.profile.finish()
        validate(env, self.root / "sources")
        self.assertEqual((self.root/'sources'/f'{r.digest}.bin').read_bytes(), b"Fjord")

    def test_accounts_preserve_zero_and_omit_absent_metrics(self):
        accounts(self.profile, self.client(official_transport))
        rows = {c["field"]:c for c in self.profile.data["claims"]}
        self.assertEqual(rows["annual_result"]["value"]["amount"], 0)
        self.assertNotIn("equity", rows)
        self.assertEqual(rows["revenue"]["reporting_period"], "2025-01-01/2025-12-31")

    def test_group_and_wrong_company_accounts_are_not_published(self):
        data = [{"regnskapstype":"KONSERN","virksomhet":{"organisasjonsnummer":ORG}},
                {"regnskapstype":"SELSKAP","virksomhet":{"organisasjonsnummer":"123456789"}}]
        accounts(self.profile, self.client(lambda u,h:response(u,data)))
        self.assertFalse(any(c["availability"]=="available" for c in self.profile.data["claims"]))

    def test_resigned_and_deceased_people_are_not_published(self):
        data = {"rollegrupper":[{"roller":[{"fratraadt":True,"person":{"navn":{"fornavn":"Old"}}},
                 {"person":{"erDoed":True,"navn":{"fornavn":"Past"}}}]}]}
        roles(self.profile,self.client(lambda u,h:response(u,data)))
        self.assertEqual(self.profile.data["claims"][0]["availability"],"not_available")

    def test_subunit_parent_must_match(self):
        data={"_embedded":{"underenheter":[{"organisasjonsnummer":"123456789","navn":"Wrong","overordnetEnhet":"111111111"}]}}
        self.assertEqual(workplaces(self.profile,self.client(lambda u,h:response(u,data))),[])

    def test_refresh_is_idempotent_and_preserves_history(self):
        r = response("https://example.org", "Fjord")
        self.profile.add("legal_name","Fjord",r,"Fjord")
        first=self.profile.finish()
        refresh(first,self.root/'history')
        second=copy.deepcopy(first);second['run']['run_id']='new';second['evidence'][0]['retrieved_at']='2026-11-14T12:00:00Z'
        refresh(second,self.root/'history')
        self.assertEqual(second['changes'],[])
        self.assertTrue(second['refresh']['unchanged'])
        self.assertEqual(len((self.root/'history'/f'{ORG}.jsonl').read_text().splitlines()),1)

    def test_failed_refresh_does_not_claim_company_closure(self):
        self.profile.add("legal_name","Fjord",response("https://example.org","Fjord"),"Fjord")
        refresh(self.profile.finish(),self.root/'history')
        failed=Profile(ORG,'failed',self.root/'sources').finish(status='failed')
        refresh(failed,self.root/'history')
        self.assertEqual(failed['changes'],[])
        self.assertIn('legal_name',[x['field'] for x in failed['refresh_unknowns']])


class WebsiteTests(Workspace):
    def page(self, body, url="https://fjordtest.no/"):
        return Page(response(url,body,headers={'content-type':'text/html; charset=utf-8'}))

    def test_owner_footer_needs_both_legal_name_and_exact_org(self):
        self.assertIsNotNone(ownership(self.page(f'<footer>Fjord Test AS. Org. {ORG}</footer>'),RECORD))
        self.assertIsNone(ownership(self.page('<footer>Other AS. Org. 123456789</footer>'),RECORD))
        self.assertIsNone(ownership(self.page(f'<article>Fjord Test AS org {ORG}</article>'),RECORD))

    def test_name_and_town_alone_do_not_prove_ownership(self):
        self.assertIsNone(ownership(self.page('<footer>Fjord Test AS in Bergen</footer>'),RECORD))

    def test_parked_domain_is_rejected(self):
        self.assertIsNone(ownership(self.page(f'<footer>Fjord Test AS {ORG}. Buy this domain</footer>'),RECORD))

    def test_exact_street_postcode_and_whole_domain_can_verify(self):
        self.assertIsNotNone(ownership(self.page('<footer>Fjord Test AS, Testveien 12, 5003 Bergen</footer>'),RECORD))
        self.assertIsNone(ownership(self.page('<footer>Fjord Test AS, Testveien 12, 5003 Bergen</footer>','https://unrelated.org/'),RECORD))

    def test_directories_and_subdomains_are_rejected(self):
        for url in ('https://northdata.de/a','https://www.norgelei.no/','https://a.largestcompanies.com/x','https://example.org/company-directory/a'):
            self.assertTrue(directory_url(url))

    def test_social_posts_people_and_share_links_are_rejected(self):
        for url in ('https://linkedin.com/in/ada','https://instagram.com/p/123','https://facebook.com/sharer.php','https://youtube.com/watch?v=abc'):
            self.assertIsNone(canonical_social(url))
        self.assertEqual(canonical_social('https://www.linkedin.com/company/fjordtest/?trk=foo')[0], 'https://www.linkedin.com/company/fjordtest')

    def test_news_needs_own_url_and_published_date(self):
        from datetime import date
        item={'@type':'NewsArticle','headline':'Fjord Test new factory opens','datePublished':'2026-11-01','url':'https://fjordtest.no/news/factory'}
        body='<script type="application/ld+json">'+json.dumps(item)+'</script>'
        self.assertFalse(add_article(self.profile,self.page(body),date(2026,11,13)))
        self.assertTrue(add_article(self.profile,self.page(body,item['url']),date(2026,11,13)))

    def test_future_news_and_modified_only_dates_are_not_published(self):
        from datetime import date
        for datefield, value in (('datePublished','2027-01-01'),('dateModified','2026-11-01')):
            item={'@type':'NewsArticle','headline':'News','url':'https://fjordtest.no/news/a',datefield:value}
            self.assertFalse(add_article(self.profile,self.page('<script type="application/ld+json">'+json.dumps(item)+'</script>',item['url']),date(2026,11,13)))

    def test_jobs_need_exact_employer_number_and_current_expiry(self):
        from datetime import date
        job={'@type':'JobPosting','title':'Engineer','datePosted':'2026-11-01','validThrough':'2026-12-01','hiringOrganization':{'identifier':ORG}}
        page=lambda:self.page('<script type="application/ld+json">'+json.dumps(job)+'</script>','https://fjordtest.no/jobs/engineer')
        self.assertEqual(add_jobs(self.profile,page(),RECORD,date(2026,11,13)),1)
        job['hiringOrganization']['identifier']='123456789'
        self.assertEqual(add_jobs(self.profile,page(),RECORD,date(2026,11,13)),0)
        job['hiringOrganization']['identifier']=ORG;job['validThrough']='2026-10-01'
        self.assertEqual(add_jobs(self.profile,page(),RECORD,date(2026,11,13)),0)

    def test_script_in_company_name_cannot_escape_viewer_data(self):
        r=response('https://example.org','</script><script>alert(1)</script>')
        self.profile.add('legal_name',r.text,r,r.text)
        doc=render([self.profile.finish()],{'requests_used':0})
        self.assertNotIn('</script><script>alert(1)',doc)
        self.assertIn('\\u003c/script>',doc)


class SearchTests(Workspace):
    def env(self):
        return self.profile.finish()

    def test_no_keys_means_zero_paid_attempts(self):
        with patch.dict(os.environ,{},clear=True):
            s=Serper({ORG:RECORD},{ORG:self.env()},credits=10)
            self.assertEqual(s.cap,0)
            self.assertEqual(list(s.website_candidates(RECORD,self.client(official_transport))),[])

    def test_spend_priority_favors_operating_company(self):
        shell=copy.deepcopy(RECORD);shell.update(navn='Fjord Holding AS',antallAnsatte=0)
        self.assertGreater(priority(RECORD,self.env())[1],priority(shell,self.env())[1])

    def test_incomplete_registry_does_not_receive_paid_allowance(self):
        bad={'organisasjonsnummer':ORG,'navn':'Fjord'}
        with patch.dict(os.environ,{'SERPER_API_KEY':'test'}):
            s=Serper({ORG:bad},{ORG:self.env()},credits=20)
        self.assertEqual(s.allocations.get(ORG,0),0)

    def test_shared_credit_cap_includes_failed_requests(self):
        with patch.dict(os.environ,{'SERPER_API_KEY':'test'}):
            s=Serper({ORG:RECORD},{ORG:self.env()},credits=1)
        def transport(url,headers,method,body):
            return response(url,{'message':'error'},503)
        client=self.client(transport)
        self.assertEqual(list(s.website_candidates(RECORD,client)),[])
        self.assertEqual(s.used,1)
        self.assertEqual(client.budget.used,1)

    def test_auth_or_rate_limit_disables_provider(self):
        for status in (401,402,403,429):
            with patch.dict(os.environ,{'SERPER_API_KEY':'test'}):
                s=Serper({ORG:RECORD},{ORG:self.env()},credits=2)
            client=self.client(lambda u,h,m,b:response(u,{},status))
            list(s.website_candidates(RECORD,client))
            self.assertTrue(s.disabled)
            self.assertEqual(s.used,1)

    def test_serper_leads_are_filtered_and_never_become_claims(self):
        with patch.dict(os.environ,{'SERPER_API_KEY':'test'}):
            s=Serper({ORG:RECORD},{ORG:self.env()},credits=1)
        body={'organic':[{'link':'https://northdata.de/x','title':'Fjord Test AS'}, {'link':'https://real-fjord.no/about','title':'Fjord Test AS'}]}
        client=self.client(lambda u,h,m,b:response(u,body))
        leads=list(s.website_candidates(RECORD,client))
        self.assertEqual(leads,[('https://real-fjord.no/','serper_lead_requires_owner_proof')])
        self.assertEqual(self.profile.data['evidence'],[])


class RunnerTests(Workspace):
    def test_one_result_per_input_on_worker_crash(self):
        def crash(*args,**kwargs):raise RuntimeError('Injected worker failure')
        items=[{'organisation_number':str(900000000+i),'input_valid':True} for i in range(100)]
        rows,report=run_batch(items,self.root/'run',processor=crash,websites=False,workers=8,max_seconds=5)
        self.assertEqual(len(rows),100)
        self.assertEqual(report['terminal_states'],{'failed':100})
        self.assertEqual(report['requests_used'],0)
        self.assertEqual(len((self.root/'run'/'submission.jsonl').read_text().splitlines()),100)

    def test_hung_worker_does_not_block_terminal_output(self):
        release=threading.Event()
        entered=threading.Event()
        def hang(*args,**kwargs):
            entered.set()
            release.wait(1)
            return Profile(ORG,'late',self.root/'sources').finish(status='failed')
        before=time.monotonic()
        try:
            # Measure worker shutdown, not parsing the optional 210k-row map.
            rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',processor=hang,websites=False,max_seconds=.08,candidate_map=None)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic()-before,.8)
            self.assertEqual(len(rows),1)
            self.assertTrue(report['deadline_reached'])
        finally:release.set()

    def test_snapshot_gap_does_not_abort_other_companies(self):
        def transport(url,headers):
            if '123456789' in url:return response(url,{},404)
            return official_transport(url,headers)
        rows,report=run_batch([{'organisation_number':ORG},{'organisation_number':'123456789'}],self.root/'run',transport=transport,websites=False)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['run']['terminal_status'],'completed')
        self.assertEqual(rows[1]['run']['terminal_status'],'failed')

    def test_successful_pipeline_emits_verifiable_contract(self):
        rows,report=run_batch([{'organisation_number':ORG}],self.root/'run',transport=official_transport,websites=False)
        self.assertTrue(report['one_result_per_input'])
        validate(rows[0],self.root/'run'/'sources')
        self.assertTrue(any(c['field']=='revenue' and c['availability']=='available' for c in rows[0]['claims']))
        self.assertTrue(rows[0]['summary']['sentences'])

    def test_500_inputs_scale_without_a_100_record_limit(self):
        def minimal(item,runid,sources,*args,**kwargs):
            return Profile(item['organisation_number'],runid,sources).finish(status='failed')
        rows,report=run_batch([{'organisation_number':str(900000000+i)} for i in range(500)],self.root/'run',processor=minimal,websites=False,max_seconds=15)
        self.assertEqual(len(rows),500)
        self.assertTrue(report['one_result_per_input'])


if __name__=='__main__':unittest.main()
