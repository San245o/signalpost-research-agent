import gzip
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from signalpost.net import Budget,Client
from signalpost.records import Profile,validate
from signalpost.site_crawl import socials
from signalpost.web import Page,collect_site
from signalpost.news import add_news
from scripts.build_public_seeds import build
from .helpers import ORG,RECORD,response


class RelationshipDiscovery(unittest.TestCase):
    def test_original_domain_vocabulary_joins_rare_name_token_without_generic_solutions(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'local-cache';source.mkdir()
            (source/'unique_crawler_domains.json').write_text(json.dumps(['kulturpark.no','solutions.no']),encoding='utf-8')
            db=sqlite3.connect(source/'brreg_companies.sqlite')
            db.execute('CREATE TABLE companies (organisation_number TEXT, website TEXT, name TEXT)')
            db.execute('INSERT INTO companies VALUES (?,?,?)',(ORG,'','HOLMEN KULTURPARK SOLUTIONS AS'))
            db.execute('INSERT INTO companies VALUES (?,?,?)',('974760673','','NORDRE STJERNEHAVN AS'));db.commit();db.close()
            output=root/'leads.gz';build(source,output)
            rows=[json.loads(line) for line in gzip.decompress(output.read_bytes()).decode().splitlines()]
            by_org={r['organisation_number']:r for r in rows}
            self.assertEqual(by_org[ORG]['urls'],['https://kulturpark.no/'])
            self.assertEqual(by_org['974760673']['urls'],['https://stjernehavn.no/'])

    def test_full_legal_owner_domain_uses_live_official_clearance_and_rejects_namesakes(self):
        record={**RECORD,'navn':'E & AL SOLUTIONS AS','hjemmeside':'https://ealsolutions.no/'}
        body='<title>Konsulenttjenester | E &amp; AL SOLUTIONS AS</title><h1>E &amp; AL SOLUTIONS AS</h1><p>'+('Vi leverer rådgivning og utvikling. '*12)+'</p>'
        def exercise(collision):
            tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
            def transport(url,headers):
                if url.endswith('/robots.txt'):return response(url,'',404)
                if 'data.brreg.no' in url:
                    rows=[{'organisasjonsnummer':ORG,'navn':record['navn']}]
                    if collision:rows.append({'organisasjonsnummer':'974760673','navn':record['navn']})
                    return response(url,{'page':{'totalPages':1},'_embedded':{'enheter':rows}})
                return response(url,body)
            p=Profile(ORG,'fixture',root);collect_site(p,Client(Budget(25,10),ORG,transport=transport),record.copy(),candidate_cap=1,page_cap=0)
            return p
        # This is a guessed-site path; a registry-listed URL has its own gate.
        record.pop('hjemmeside');record['_website_seeds']=['https://ealsolutions.no/']
        p=exercise(False);claim=next(c for c in p.data['claims'] if c['field']=='official_website' and c['availability']=='available')
        self.assertTrue(any(e['source_class']=='official_registry' and e['id'] in claim['evidence_ids'] for e in p.data['evidence']))
        validate(p.finish(),p.captures)
        other=exercise(True);self.assertFalse(any(c['availability']=='available' and c['field']=='official_website' for c in other.data['claims']))

    def test_verified_careers_chain_fetches_public_feed_and_labels_client_roles_as_advertised(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);record={**RECORD,'hjemmeside':'https://fjordtest.no/','naeringskode1':{'kode':'78.100'}}
            def item(title,key,expiry='2099-12-31'):
                return {'title':title,'url':'https://fjordtest.teamtailor.com/jobs/'+key,
                        '_jobposting':{'title':title,'datePosted':'2026-11-01','validThrough':expiry,'hiringOrganization':{'name':'Fjord Test'},'description':'Advertisement for a client.'}}
            feed={'version':'https://jsonfeed.org/version/1.1','title':'Fjord Test','home_page_url':'https://fjordtest.teamtailor.com/jobs',
                  'items':[item('Client Alpha - Engineer','123456-engineer'),item('Client Beta - Designer','123457-designer'),
                           item('Expired role','123458-expired','2020-01-01'),item('Open application','123459-pool')]}
            seen=[]
            def transport(url,headers):
                seen.append(url)
                if url.endswith('/robots.txt'):return response(url,'',404)
                if url=='https://fjordtest.no/':return response(url,f'<footer>Fjord Test AS Org.nr: {ORG}</footer><a href="https://fjordtest.teamtailor.com/">Careers</a><a href="/blog">News</a>')
                if url=='https://fjordtest.no/blog':return response(url,'<article><h2><a href="/blog/advice">Useful advice and tips for your business</a></h2><time datetime="2026-11-01"></time></article>')
                if url.endswith('/jobs.json'):return response(url,feed)
                if url=='https://fjordtest.teamtailor.com/':return response(url,'<title>Fjord Test AS Careers</title><meta content="https://static.teamtailor.com/image.png"><a href="/jobs/123456-engineer">Client Alpha - Engineer</a><a href="/jobs/123457-designer">Client Beta - Designer</a>')
                return response(url,'',404)
            p=Profile(ORG,'fixture',root);collect_site(p,Client(Budget(30,10),ORG,transport=transport),record,candidate_cap=1,page_cap=3)
            ads=[c for c in p.data['claims'] if c['field']=='hiring_signal' and c['availability']=='available']
            self.assertEqual(len(ads),2);self.assertIn('https://fjordtest.teamtailor.com/jobs.json',seen)
            self.assertIn('https://fjordtest.no/blog',seen)
            for c in ads:
                self.assertEqual(c['value']['relationship'],'recruiter_advertisement');self.assertFalse(c['value']['direct_employer_verified'])
                self.assertEqual(c['value']['date'],'2026-11-01');self.assertEqual(c['value']['valid_through'],'2099-12-31')
                ev=[e for e in p.data['evidence'] if e['id'] in c['evidence_ids']]
                self.assertTrue(any('"_jobposting"' in e['claim_span'] for e in ev))
                self.assertTrue(any('href="https://fjordtest.teamtailor.com/"' in e['claim_span'] for e in ev))
            final=p.finish();validate(final,root)
            self.assertTrue(any('Recruiter-advertised' in s['text'] for s in final['summary']['sentences']))

    def test_advice_article_is_labelled_publication_and_footer_date_or_index_is_withheld(self):
        with tempfile.TemporaryDirectory() as folder:
            def extract(raw):
                p=Profile(ORG,'fixture',Path(folder));add_news(p,Page(response('https://fjordtest.no/blog',raw)),date(2026,11,14),RECORD);return p
            p=extract('<article><h2><a href="/blog/fuktskader">Fuktskader: Tidlige tegn du bør ta på alvor</a></h2><time datetime="2026-11-01"></time></article>')
            self.assertEqual(p.data['claims'][0]['value']['activity_kind'],'company_published_advice')
            product=extract('<article><div class="archive-item--date">22.06.2026</div><span class="archive-item--title">DJI Matrice 4D now C6 compatible</span><a href="/blog/c6"></a></article>')
            self.assertEqual(product.data['claims'][0]['value']['activity_kind'],'company_published_product_article')
            self.assertFalse(extract('<article><h2><a href="/blog/advice">Useful advice for your business</a></h2><footer><time datetime="2026-11-01"></time></footer></article>').data['claims'])
            self.assertFalse(extract('<article><h2><a href="/blog/index">Liknende artikler</a></h2><time datetime="2026-11-01"></time></article>').data['claims'])

    def test_opaque_social_id_requires_exact_entity_sameas_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            for tax_id,expected in [(ORG,1),('974760673',0),('',0)]:
                with self.subTest(tax_id=tax_id):
                    item={'@type':'Organization','url':'https://fjordtest.no/','taxID':tax_id,'sameAs':['https://facebook.com/123456789012']}
                    raw='<script type="application/ld+json">'+json.dumps(item)+'</script>'
                    p=Profile(ORG,'fixture',Path(folder));socials(p,Page(response('https://fjordtest.no/',raw)),RECORD)
                    self.assertEqual(len(p.data['claims']),expected)

    def test_linked_article_card_with_plain_month_first_date(self):
        with tempfile.TemporaryDirectory() as folder:
            def extract(date_markup):
                raw='<a href="/artikler/fuktskader" class="blog_card"><div>'+date_markup+'</div><div><h3>Fuktskader: Tidlige tegn du bør ta på alvor</h3><p>Råd om vedlikehold.</p></div></a>'
                p=Profile(ORG,'fixture',Path(folder))
                add_news(p,Page(response('https://fjordtest.no/artikler',raw)),date(2026,11,14),RECORD)
                return p
            p=extract('<div class="text-sm">September 9, 2026</div>')
            self.assertEqual(len(p.data['claims']),1)
            self.assertEqual(p.data['claims'][0]['value']['date'],'2026-09-09')
            self.assertEqual(p.data['claims'][0]['value']['activity_kind'],'company_published_advice')
            validate(p.finish(),Path(folder))
            self.assertFalse(extract('<footer>September 9, 2026</footer>').data['claims'])
            self.assertFalse(extract('<span>September 9, 2026</span><span>October 2, 2026</span>').data['claims'])
            self.assertFalse(extract('<p>The rules changed on September 9, 2026.</p>').data['claims'])
