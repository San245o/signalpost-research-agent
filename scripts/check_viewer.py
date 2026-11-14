"""Optional developer browser check. Playwright is not a runtime dependency."""
from pathlib import Path
import json
import sys
from playwright.sync_api import sync_playwright

root=Path(sys.argv[1] if len(sys.argv)>1 else 'runs/smoke-100').resolve()
expected=len((root/'submission.jsonl').read_text(encoding='utf-8').splitlines())
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1050},device_scale_factor=1)
    page.set_default_timeout(15000)
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto((root/'index.html').as_uri())
    page.wait_for_selector('#list .company')
    assert page.locator('#list .company').count()==expected
    page.locator('#list .company input').nth(0).check()
    page.locator('#list .company input').nth(1).check()
    page.locator('#compare').click()
    assert page.locator('.compare-table').is_visible()
    assert page.locator('#compare-period').is_visible()
    page.locator('#list .company button').nth(0).click()
    assert page.locator('[role="tab"]').count()==6
    page.locator('#tab-financials').click()
    assert page.locator('#financial-period').is_visible()
    page.locator('#tab-people').click()
    assert 'Registered leadership and locations' in page.locator('#topic').inner_text()
    page.locator('#tab-hiring').click()
    assert 'Observed job advertisements' in page.locator('#topic').inner_text()
    page.locator('#tab-activity').click()
    assert 'Dated activity' in page.locator('#topic').inner_text()
    page.locator('#tab-sources').click()
    assert 'Facts and provenance' in page.locator('#topic').inner_text()
    page.locator('#tab-overview').click()
    page.keyboard.press('ArrowRight')
    assert page.locator('#tab-financials').get_attribute('aria-selected')=='true'
    page.locator('#tab-overview').click()
    page.locator('#sort').select_option('revenue')
    assert page.locator('#list .company').count()==expected
    page.get_by_role('button',name='Evidence',exact=True).first.click()
    assert page.locator('#evidence').is_visible()
    assert page.locator('#evbody pre').count()>=1
    page.keyboard.press('Escape')
    assert not page.locator('#evidence').is_visible()
    rows=[json.loads(x) for x in (root/'submission.jsonl').read_text(encoding='utf-8').splitlines()]
    org=rows[0]['organisation_number']
    page.locator('#search').fill(org)
    assert page.locator('#list .company').count()==1
    page.locator('#search').fill('')
    with page.expect_download() as downloaded:
        page.locator('#export').click()
    download=downloaded.value
    download.save_as(str(root/'viewer-export.jsonl'))
    assert len((root/'viewer-export.jsonl').read_text(encoding='utf-8').splitlines())==2
    page.locator('#question').fill('Who runs this company?')
    page.locator('#ask').click()
    assert 'Answer from captured evidence' in page.locator('#detail').inner_text()
    assert 'company=' in page.url
    page.locator('#list .company button').nth(0).click()
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=str(root/'viewer-desktop.png'),full_page=False)
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#detail').scroll_into_view_if_needed()
    page.screenshot(path=str(root/'viewer-mobile.png'),full_page=False)
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    for tab in ['financials','people','hiring','activity','sources']:
        page.locator('#tab-'+tab).click()
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),tab
    page.emulate_media(color_scheme='dark')
    page.locator('#tab-overview').click()
    page.screenshot(path=str(root/'viewer-dark.png'),full_page=False)
    browser.close()
assert not errors,errors
report={'passed':True,'company_count':expected,'checks':['load all supplied companies','select and compare two','financial period selector','six research topic tabs','keyboard tab navigation','sort companies by revenue','open evidence and exact span','keyboard close dialog','search by org','export selected JSONL','ask from cited claims','company deep link','desktop 1440px','all tabs mobile 390px no horizontal overflow','dark mode'],'browser_errors':errors}
(root/'browser-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
