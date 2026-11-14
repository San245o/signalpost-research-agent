import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from signalpost.seeds import load_seeds
from signalpost.runner import run_batch
from signalpost.records import validate
from scripts.build_public_seeds import build
from tests.helpers import ORG, RECORD, response, official_transport


class PublicDiscovery(unittest.TestCase):
    def test_export_connects_public_sql_directory_and_domain_leads_with_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'local-cache';source.mkdir()
            (source/'fagfolkguiden_harvested.json').write_text(json.dumps({ORG:{'sameAs':['brand.no/contact'],'aggregateRating':{'ratingValue':5}}}),encoding='utf-8')
            (source/'unique_crawler_domains.json').write_text(json.dumps(['fjordtest.no','unrelated.no']),encoding='utf-8')
            with sqlite3.connect(source/'brreg_companies.sqlite') as db:
                db.execute('CREATE TABLE companies (organisation_number TEXT, website TEXT, name TEXT)')
                db.execute('INSERT INTO companies VALUES (?,?,?)',(ORG,'brand.no',RECORD['navn']))
            db.close()
            path=root/'public.gz';manifest=build(source,path)
            rows,report=load_seeds({ORG},path=path)
            self.assertEqual(rows[ORG],['https://brand.no/','https://fjordtest.no/'])
            self.assertEqual(report['verification_pages'][ORG],['https://brand.no/contact'])
            self.assertEqual(manifest['website_urls'],2)
            self.assertNotIn('ratingValue',gzip.decompress(path.read_bytes()).decode())
            self.assertEqual(report['company_hints'][ORG]['website_origins']['https://brand.no/'],[0,1])

    def graph_run(self,root,ids,*,rank='normal',ended=False):
        row={'organisation_number':ORG,'urls':[],'wikidata':['Q123'],
             'social_urls':['https://facebook.com/fjordtest']}
        path=root/'map.gz';raw=gzip.compress((json.dumps(row)+'\n').encode(),mtime=0);path.write_bytes(raw)
        path.with_suffix('.manifest.json').write_text(json.dumps({'archive_sha256':hashlib.sha256(raw).hexdigest()}),encoding='utf-8')
        def statement(value):return {'rank':'normal','mainsnak':{'snaktype':'value','datavalue':{'value':value}}}
        social=statement('fjordtest');social['rank']=rank
        if ended:social['qualifiers']={'P582':[{'snaktype':'value','datavalue':{'value':{'time':'+2020-01-01T00:00:00Z'}}}]}
        graph={'entities':{'Q123':{'id':'Q123','claims':{'P2333':[statement(v) for v in ids],
               'P856':[statement('https://freshbrand.no/')],'P2013':[social]}}}}
        def transport(url,headers):
            if 'data.brreg.no' in url:return official_transport(url,headers)
            if url.endswith('/robots.txt'):return response(url,'',404)
            if url.endswith('/Q123.json'):return response(url,graph)
            if url=='https://freshbrand.no/':return response(url,f'<footer>{RECORD["navn"]} Org.nr: {ORG}</footer>')
            return response(url,'',404)
        rows,report=run_batch([{'organisation_number':ORG}],root/'run',candidate_map=path,
                             transport=transport,search_mode='none',nav_pages=0,candidate_cap=1,page_cap=0)
        validate(rows[0],root/'run/sources')
        return rows[0]

    def test_live_graph_resolves_brand_and_social_but_never_substitutes_for_site_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            row=self.graph_run(Path(directory),[ORG])
            values={(c['field'],str(c['value'])) for c in row['claims'] if c['availability']=='available'}
            self.assertIn(('official_website','https://freshbrand.no/'),values)
            self.assertIn(('company_profile','https://facebook.com/fjordtest'),values)
            social=next(c for c in row['claims'] if c['field']=='company_profile' and c['availability']=='available')
            ev=next(e for e in row['evidence'] if e['id'] in social['evidence_ids'])
            self.assertIn(ORG,ev['claim_span']);self.assertIn('fjordtest',ev['claim_span'])
            self.assertEqual(ev['source_class'],'public_knowledge_graph')
            self.assertNotIn('external_discovery',row)

    def test_wrong_shared_deprecated_and_ended_graph_statements_cannot_publish_cached_socials(self):
        for ids,rank,ended in [(['974760673'],'normal',False),([ORG,'974760673'],'normal',False),
                               ([ORG],'deprecated',False),([ORG],'normal',True)]:
            with self.subTest(ids=ids,rank=rank,ended=ended),tempfile.TemporaryDirectory() as directory:
                row=self.graph_run(Path(directory),ids,rank=rank,ended=ended)
                self.assertFalse(any(c['field']=='company_profile' and c['availability']=='available' for c in row['claims']))

    def test_custom_map_hash_failure_does_not_fall_back_to_other_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.gz';path.write_bytes(b'corrupt')
            path.with_suffix('.manifest.json').write_text(json.dumps({'archive_sha256':'0'*64}),encoding='utf-8')
            rows,report=load_seeds({ORG},path=path)
            self.assertEqual(rows,{});self.assertIn('mismatched',report['error'])
