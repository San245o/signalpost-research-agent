import unittest,tempfile
from pathlib import Path
from signalpost.social_identity import own_handle
from signalpost.site_crawl import socials
from signalpost.web import Page
from signalpost.records import Profile,refresh
from tests.helpers import ORG,RECORD,response
class SocialOwnership(unittest.TestCase):
 def test_descriptive_handle_from_own_page_is_cited(self):
  record={**RECORD,'navn':'PEMASOL AS'}
  with tempfile.TemporaryDirectory() as d:
   p=Profile(ORG,'test',Path(d));r=response('https://pemasol.no/','<footer><a href="https://facebook.com/pemasolskjerming">Facebook</a></footer>')
   socials(p,Page(r),record)
   self.assertEqual([c['value'] for c in p.data['claims']],['https://facebook.com/pemasolskjerming'])
   self.assertIn('pemasolskjerming',p.data['evidence'][0]['claim_span'])
 def test_group_and_foreign_accounts_are_withheld(self):
  r={**RECORD,'navn':'FJORD TEST CLINIC AS'}
  for h in ['fjordtestgroup','fjordtestholding','fjordtest-ltd','fjordtestgmbh','unrelatedfjordtest']:
   self.assertFalse(own_handle(h,r,'https://fjordtest.no/'),h)
 def test_group_itself_can_have_its_named_account(self):
  self.assertTrue(own_handle('fjordtestgroup',{**RECORD,'navn':'FJORD TEST AS'},'https://fjordtest.no/'))
 def test_other_branch_town_is_rejected(self):
  r={**RECORD,'navn':'FJORD TEST AS','forretningsadresse':{'poststed':'OSLO','kommune':'OSLO'}}
  self.assertFalse(own_handle('fjordtestbergen',r,'https://fjordtest.no/'))
  self.assertTrue(own_handle('fjordtestoslo',r,'https://fjordtest.no/'))

 def test_norway_account_on_one_platform_blocks_unmarked_global_on_another(self):
  record={**RECORD,'navn':'JYSK AS'}
  with tempfile.TemporaryDirectory() as d:
   p=Profile(ORG,'test',Path(d))
   raw='<a href="https://instagram.com/jysknorge">Instagram Norway</a><a href="https://tiktok.com/@jysk">TikTok</a>'
   socials(p,Page(response('https://jysk.no/',raw)),record)
   self.assertEqual([c['value'] for c in p.data['claims']],['https://instagram.com/jysknorge'])

 def test_invalid_holding_site_is_withdrawn_on_refresh_instead_of_kept_stale(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);history=root/'history';captures=root/'sources'
   legal=response('https://data.brreg.no/identity','Fjord Holding AS')
   wrong=response('https://fjord.no/','<title>Fjord AS</title><p>12345678</p>')
   old=Profile(ORG,'old',captures);old.add('legal_name','Fjord Holding AS',legal,legal.text)
   old.add('official_website',wrong.url,wrong,wrong.text,'company_owned')
   old.add('company_profile','https://facebook.com/fjord',wrong,wrong.text,'company_owned')
   refresh(old.finish(),history,captures)
   new=Profile(ORG,'new',captures);new.add('legal_name','Fjord Holding AS',legal,legal.text)
   env=new.finish();refresh(env,history,captures)
   self.assertFalse(any(c['availability']=='available' and c['field'] in {'official_website','company_profile'} for c in env['claims']))
   self.assertEqual(sum(c['type']=='invalidated_claim' for c in env['changes']),2)
   import json
   self.assertNotIn('https://fjord.no',json.dumps(env))
