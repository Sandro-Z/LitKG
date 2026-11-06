"""Optional real-browser smoke test; run with Playwright against a disposable DB."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    base = os.environ.get('QC_TEST_URL', 'http://quality:8090')
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.goto(base)
        expect(page.locator('#connection')).not_to_have_text('连接中')
        initial = len(page.request.get(base + '/api/graphs').json()['graphs'])
        page.click('[data-tab="import"]')
        examples = Path(__file__).parents[1] / 'examples'
        page.set_input_files('#files', [str(examples / 'graph-a.json'), str(examples / 'graph-b.json')])
        page.click('#import-json')
        expect(page.locator('#graphs .graph-row')).to_have_count(initial + 2)
        assert page.locator('#radar svg').count() == 1
        page.click('[data-tab="audit"]')
        page.select_option('#audit-graph', index=0)
        page.click('#run-auto')
        expect(page.locator('#audit-result')).to_contain_text('completed', timeout=15000)
        expect(page.locator('#findings tbody tr')).to_have_count(1)
        page.click('[data-tab="human"]')
        page.select_option('#human-graph', index=0)
        page.fill('#reviewer', 'browser-fixture-reviewer')
        page.click('#sample')
        page.wait_for_selector('#sample-list button')
        assert page.locator('#sample-list button').count() == 2
        page.locator('#sample-list button').first.click()
        page.wait_for_selector('#claim-detail .source mark')
        page.select_option('#human-verdict', 'supported')
        page.fill('#human-note', 'Synthetic browser test, not a real expert review.')
        page.click('#save-review')
        expect(page.locator('#sample-info')).to_contain_text('已评 1')
        page.screenshot(path='/artifacts/human.png', full_page=True)
        page.click('[data-tab="audit"]')
        with page.expect_download() as download:
            page.click('#export')
        assert download.value.suggested_filename.endswith('.json')
        page.click('[data-tab="compare"]')
        page.screenshot(path='/artifacts/compare.png', full_page=True)
        # Exercise JSON paste import, literal escaping and an empty graph.
        page.click('[data-tab="import"]')
        page.set_input_files('#files', [])
        page.fill('#json', json.dumps({'name': '<img src=x onerror=alert(1)>', 'claims': []}))
        page.click('#import-json')
        expect(page.locator('#graphs .graph-row')).to_have_count(initial + 3)
        assert not page.locator('#graphs img').count()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path='/artifacts/mobile.png', full_page=True)
        browser.close()
    if errors:
        raise AssertionError('BROWSER_CONSOLE_ERRORS: ' + json.dumps(errors))
    print(json.dumps({'browser': 'chromium', 'result': 'passed', 'checks': [
        'multiple-file-import', 'automatic-review', 'radar', 'fixed-sample', 'source-highlight',
        'human-review', 'report-download', 'html-escaping', 'mobile-layout']}))


if __name__ == '__main__':
    main()
