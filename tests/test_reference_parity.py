import unittest
from unittest.mock import patch
from signalpost.web import candidates,verify_site,Page,canonical_social,namesake
from signalpost.net import Budget,Client
from tests.helpers import RECORD,ORG,response
class ReferenceParity(unittest.TestCase):
 def test_brand_contact_is_proof_but_shared_switchboard_alone_is_not(self):
  record={**RECORD,'navn':'FJORD HOTEL DRIFT AS','telefon':'12345678'}
  client=Client(Budget(),ORG,transport=lambda u,h:response(u,{},404))
  with patch('signalpost.web.namesake',return_value=False),patch('signalpost.web.held_by_other',return_value=False):
   page=Page(response('https://fjordhotel.no/','<title>Fjord Hotel</title><p>12345678</p>'))
   self.assertIsNotNone(verify_site(page,record,'name_domain_lead',client,page.response.url))
   other=Page(response('https://unrelated.no/','<title>Other company</title><p>12345678</p>'))
   self.assertIsNone(verify_site(other,record,'name_domain_lead',client,other.response.url))
  with patch('signalpost.web.namesake',return_value=False),patch('signalpost.web.held_by_other',return_value=True):
   self.assertIsNone(verify_site(page,record,'name_domain_lead',client,page.response.url))
 def test_email_precedes_cache_and_short_names_are_tried(self):
  rows=list(candidates({**RECORD,'navn':'ABCD AS','epostadresse':'post@brand.no','_website_seeds':['https://oldbrand.no/']}))
  self.assertLess(next(i for i,x in enumerate(rows) if x[1]=='email_domain_lead'),next(i for i,x in enumerate(rows) if x[1]=='public_seed'))
  self.assertIn(('https://abcd.com/','name_domain_lead'),rows)
 def test_operating_name_is_a_candidate_and_a_named_owner(self):
  record={**RECORD,'_operating_names':['Fjord Clinic']}
  self.assertIn(('https://fjordclinic.no/','operating_name_domain_lead'),list(candidates(record)))
  page=Page(response('https://fjordclinic.no/','<title>Fjord Clinic</title><p>Org.nr: '+ORG+'</p>'))
  client=Client(Budget(),ORG,transport=lambda u,h:response(u,{},404))
  self.assertIsNotNone(verify_site(page,record,'operating_name_domain_lead',client,page.response.url))
 def test_mobile_and_legacy_social_routes(self):
  self.assertEqual(canonical_social('https://m.facebook.com/pages/Fjord-Test/123456789')[1],'Fjord-Test')
  self.assertEqual(canonical_social('https://facebook.com/pg/fjordtest/about')[1],'fjordtest')
  self.assertEqual(canonical_social('https://www.tiktok.com/@fjordtest')[1],'@fjordtest')
  self.assertIsNone(canonical_social('https://www.tiktok.com/@fjordtest/video/123'))

 def test_complete_phrase_search_resolves_broad_tokens_without_ignoring_collision(self):
  from urllib.parse import urlsplit,parse_qs
  record={**RECORD,'navn':'STORD HOTEL DRIFT AS'}
  def transport(url,headers):
   q=parse_qs(urlsplit(url).query)
   if 'navnMetodeForSoek' not in q:return response(url,{'page':{'totalPages':99},'_embedded':{'enheter':[]}})
   return response(url,{'page':{'totalPages':1},'_embedded':{'enheter':[{'navn':record['navn'],'organisasjonsnummer':ORG}]}})
  self.assertFalse(namesake(record,Client(Budget(),ORG,transport=transport)))
  def collision(url,headers):
   r=transport(url,headers)
   if 'navnMetodeForSoek' in url:return response(url,{'page':{'totalPages':1},'_embedded':{'enheter':[{'navn':record['navn'],'organisasjonsnummer':'974760673'}]}})
   return r
  self.assertTrue(namesake(record,Client(Budget(),ORG,transport=collision)))

 def test_operating_company_cannot_lend_its_site_to_its_holding(self):
  record={**RECORD,'navn':'FJORD HOLDING AS','telefon':'12345678'}
  client=Client(Budget(),ORG,transport=lambda u,h:response(u,{},404))
  with patch('signalpost.web.namesake',return_value=False),patch('signalpost.web.held_by_other',return_value=False):
   page=Page(response('https://fjord.no/','<title>Fjord AS</title><p>12345678</p>'))
   self.assertIsNone(verify_site(page,record,'name_domain_lead',client,page.response.url))
   exact=Page(response('https://fjord.no/','<title>Fjord AS</title><footer>Fjord Holding AS Org.nr: '+ORG+'</footer>'))
   self.assertIsNotNone(verify_site(exact,record,'name_domain_lead',client,exact.response.url))
