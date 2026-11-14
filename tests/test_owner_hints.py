import unittest,tempfile
from pathlib import Path
from signalpost.net import Client,Budget
from signalpost.records import Profile,validate
from signalpost.web import collect_site,words,parse_date
from tests.helpers import ORG,RECORD,response
class OwnerHints(unittest.TestCase):
 def exercise(self,root_body='<title>Brand</title>',proof_body=None,hints=None):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);seen=[]
  def transport(url,headers):
   seen.append(url)
   if url.endswith('/robots.txt'):return response(url,'',404)
   if url=='https://brand.no/':return response(url,root_body)
   if url=='https://brand.no/legal/owner':return response(url,proof_body or '<title>Brand</title>')
   return response(url,'',404)
  p=Profile(ORG,'hints',Path(tmp.name));r=response('https://data.brreg.no/id',RECORD['navn']);p.add('legal_name',RECORD['navn'],r,r.text)
  record={**RECORD,'hjemmeside':'https://brand.no/','_website_proof_pages':hints or ['https://brand.no/legal/owner']}
  collect_site(p,Client(Budget(30,10),ORG,transport=transport,captures=Path(tmp.name)),record,candidate_cap=1,page_cap=0)
  return p,seen
 def test_unlinked_same_site_owner_page_is_fetched_and_cited_for_homepage(self):
  p,seen=self.exercise(proof_body='<footer>Fjord Test AS Org.nr: '+ORG+'</footer>')
  c=next(c for c in p.data['claims'] if c['field']=='official_website' and c['availability']=='available')
  self.assertEqual(c['value'],'https://brand.no/')
  e=next(e for e in p.data['evidence'] if e['id'] in c['evidence_ids']);self.assertEqual(e['source_url'],'https://brand.no/legal/owner');self.assertIn(ORG,e['claim_span']);validate(p.finish(),p.captures)
 def test_cache_pointer_alone_does_not_publish_a_site(self):
  p,_=self.exercise();self.assertFalse(any(c['field']=='official_website' and c['availability']=='available' for c in p.data['claims']))
 def test_foreign_host_and_http_downgrade_are_not_fetched(self):
  p,seen=self.exercise(hints=['https://other.no/legal/owner','http://brand.no/legal/owner'])
  self.assertNotIn('https://other.no/legal/owner',seen);self.assertNotIn('http://brand.no/legal/owner',seen)
 def test_other_entity_homepage_cannot_borrow_cached_subpage_ownership(self):
  p,seen=self.exercise(root_body='<footer>Other AS Org.nr: 974760673</footer>',proof_body='<footer>Fjord Test AS Org.nr: '+ORG+'</footer>')
  self.assertNotIn('https://brand.no/legal/owner',seen)
  self.assertFalse(any(c['field']=='official_website' and c['availability']=='available' for c in p.data['claims']))
 def test_real_norwegian_unicode_uses_expected_ascii_folding(self):
  self.assertEqual(words('SCHRØDER ÆØÅ'),['schroder','aeoa'])
  self.assertEqual(parse_date('1. måned 2026'),None)
  self.assertEqual(parse_date('1. mars 2026').isoformat(),'2026-03-01')
