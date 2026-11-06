"""Real browser: five scores, two reviewers, two rounds, live score fusion."""
import json
import os
from playwright.sync_api import sync_playwright, expect
from quality_control.tests.test_quality import fixture


def main():
    base = os.environ.get('QC_TEST_URL', 'http://quality:8090')
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.goto(base)
        expect(page.locator('#connection')).to_have_text('模型已配置')
        initial = len(page.request.get(base + '/api/graphs').json()['graphs'])
        payload = fixture()
        payload['policy']['scope'] = None
        payload['documents'][0]['parsing_status'] = 'partial'
        page.click('[data-tab="import"]')
        page.fill('#json', json.dumps(payload))
        page.click('#import-json')
        expect(page.locator('#graphs .graph-row')).to_have_count(initial + 1)
        page.click('[data-tab="audit"]')
        page.select_option('#audit-graph', index=0)
        page.click('#run-llm')
        expect(page.locator('#audit-result')).to_contain_text('completed', timeout=20000)
        expect(page.locator('#dimension-scores .dimension-card')).to_have_count(6)
        assert all(s != '—' for s in page.locator('#dimension-scores .score').all_text_contents())
        gid = page.request.get(base + '/api/graphs').json()['graphs'][0]['id']
        def report():
            return page.request.get(base + f'/api/graphs/{gid}').json()['latest_run']['report']
        original = report()
        assert original['dimensions']['specialization'] == 90
        assert original['dimensions']['granularity'] == 95
        assert original['dimension_details']['completeness']['basis'] == 'provided_source_text_only'
        assert original['scenarios']['field_review']['score'] is not None
        assert original['scenarios']['field_review']['overall'] is None
        page.click('[data-tab="human"]')
        page.select_option('#human-graph', index=0)
        page.fill('#reviewer', 'Alice')
        page.click('#sample')
        page.locator('#sample-list button').first.click()
        expect(page.locator('#claim-detail .source mark')).to_have_count(2)
        page.select_option('#human-verdict', 'incorrect')
        page.fill('#human-note', 'Synthetic reviewer A opinion')
        page.click('#save-review')
        expect(page.locator('#sample-info')).to_contain_text('历史提交 1 次')
        assert report()['dimensions']['correctness'] == 70
        page.fill('#reviewer', 'Bob');page.locator('#reviewer').blur()
        expect(page.locator('#save-review')).to_be_visible()
        page.select_option('#human-verdict', 'supported')
        page.click('#save-review')
        expect(page.locator('#sample-info')).to_contain_text('历史提交 2 次')
        assert report()['dimensions']['correctness'] == 85
        page.fill('#round-name', 'Second expert round')
        page.click('#new-round')
        expect(page.locator('#review-round option')).to_have_count(2)
        page.fill('#reviewer', 'Alice');page.locator('#reviewer').blur()
        page.locator('#sample-list button').first.click()
        page.select_option('#human-verdict', 'partial')
        page.click('#save-review')
        expect(page.locator('#sample-info')).to_contain_text('历史提交 3 次')
        final = report()
        assert final['dimensions']['correctness'] == 92.5
        assert final['human']['metrics']['active_votes'] == 2
        assert final['human']['metrics']['history_count'] == 3
        assert final['human']['rounds'] == 2
        assert final['machine_dimensions']['correctness'] == 100
        assert final['score_revision'] != original['score_revision']
        page.screenshot(path='/artifacts/multi-review.png', full_page=True)
        page.click('[data-tab="audit"]')
        expect(page.locator('#audit-result')).to_contain_text('92.5')
        page.screenshot(path='/artifacts/dimension-scores.png', full_page=True)
        with page.expect_download() as download:
            page.click('#export')
        exported = json.loads(open(download.value.path()).read())
        assert len(exported['human']['history']) == 3
        assert len(exported['human']['rounds']) == 2
        assert exported['run']['report']['dimensions']['correctness'] == 92.5
        page.click('[data-tab="compare"]')
        page.locator(f'input[data-graph="{gid}"]').check()
        expect(page.locator('#scoretable')).to_contain_text('92.5')
        page.screenshot(path='/artifacts/score-comparison.png', full_page=True)
        browser.close()
    assert not errors, 'Browser console errors'
    print(json.dumps({'result': 'passed', 'checks': ['five-evidence-dimensions', 'weighted-rubric-fallback',
        'provisional-scope-score', 'two-reviewers', 'two-rounds', 'revision-not-double-counted',
        'live-correctness-100-to-70-to-85-to-92.5', 'export-history-and-final-score']}))


if __name__ == '__main__': main()
