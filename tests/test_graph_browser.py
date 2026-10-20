"""Optional real-browser checks using synthetic data and intercepted HTTP only.

Install Playwright and its Chromium browser, then run with LITKG_TEST_BROWSER=1.
No running API, database, model, or frontend service is used.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('LITKG_TEST_BROWSER') == '1', 'Opt in with LITKG_TEST_BROWSER=1')
class GraphBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        spec = importlib.util.spec_from_file_location('litkg_browser_ui',
            Path(__file__).resolve().parents[1] / 'frontend/app.py')
        ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ui)
        cls.graph = {'knowledge_base_id': 1, 'nodes': [
            {'id': 1, 'label': 'AKT1', 'type': 'Protein'},
            {'id': 2, 'label': 'mTOR', 'type': 'Protein'},
            {'id': 3, 'label': '细胞内蛋白质修饰位点', 'type': 'ModificationSite'},
            {'id': 4, 'label': 'Rapamycin', 'type': 'Drug'},
            {'id': 5, 'label': 'Autophagy', 'type': 'BiologicalProcess'},
            {'id': 6, 'label': 'Long-protein-identifier-Q123456789012345678901234567890', 'type': 'Proteoform'},
            {'id': 7, 'label': 'Independent protein', 'type': 'Protein'},
            {'id': 8, 'label': 'Independent peptide', 'type': 'Peptide'},
        ], 'edges': [
            {'id': 1, 'source': 1, 'target': 2, 'label': 'ACTIVATES'},
            {'id': 2, 'source': 1, 'target': 2, 'label': 'ACTIVATES',
             'qualifiers': {'direction': 'increased', 'metric': 'abundance'}, 'speculative': True},
            {'id': 3, 'source': 2, 'target': 1, 'label': 'REGULATES'},
            {'id': 4, 'source': 4, 'target': 2, 'label': 'INHIBITS'},
            {'id': 5, 'source': 2, 'target': 3, 'label': 'MODIFIES'},
            {'id': 6, 'source': 3, 'target': 5, 'label': 'ASSOCIATED_WITH'},
            {'id': 7, 'source': 6, 'target': 5, 'label': 'AFFECTS', 'negated': True},
            {'id': 8, 'source': 1, 'target': 1, 'label': 'INTERACTS_WITH'},
            {'id': 9, 'source': 7, 'target': 8, 'label': 'HAS_PEPTIDE'},
        ]}
        with patch.object(ui, 'st'), patch.object(ui, 'components') as components:
            ui.render_graph(cls.graph)
        cls.html = components.html.call_args.args[0].replace(
            'const network = new vis.Network', 'const network = window.testNetwork = new vis.Network')
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.artifacts = Path(os.environ.get('LITKG_TEST_BROWSER_ARTIFACTS') or tempfile.mkdtemp(prefix='litkg-graph-browser-'))
        cls.artifacts.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1400, 'height': 1000}, accept_downloads=True,
            device_scale_factor=float(os.environ.get('LITKG_TEST_BROWSER_DPR', '1')))
        self.page = self.context.new_page()
        self.errors = []
        self.external = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        def route(request):
            if request.request.url == 'http://litkg.test/':
                request.fulfill(status=200, content_type='text/html', body=self.html)
            else:
                self.external.append(request.request.url)
                request.abort()
        self.page.route('**/*', route)
        self.page.goto('http://litkg.test/')
        self.page.wait_for_function("document.getElementById('status').textContent.includes('布局已固定')")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])

    def download(self, name):
        from PIL import Image
        with self.page.expect_download() as pending:
            self.page.get_by_role('button', name='下载白底 PNG').click()
        download = pending.value
        self.assertEqual(download.suggested_filename, 'litkg-kb-1.png')
        path = self.artifacts / name
        download.save_as(path)
        with Image.open(path) as image:
            image.load()
            self.assertEqual(image.format, 'PNG')
            self.assertGreater(image.width, 500)
            self.assertGreater(image.height, 500)
            if image.mode == 'RGBA':
                self.assertEqual(image.getchannel('A').getextrema(), (255, 255))
            self.assertEqual(image.convert('RGB').getpixel((0, 0)), (255, 255, 255))
            self.assertLess(min(channel[0] for channel in image.convert('RGB').getextrema()), 100)
        self.assertIn('全部 9 条', self.page.locator('#status').inner_text())
        return path

    def test_layouts_download_complete_images_and_restore_manual_positions(self):
        for mode in ['hierarchy', 'circle', 'force']:
            self.page.locator('#layout').select_option(mode)
            self.page.get_by_role('button', name='整理节点').click()
            self.page.wait_for_function("!document.getElementById('arrange').disabled")
            positions = self.page.evaluate('window.testNetwork.getPositions()')
            self.assertEqual(len(positions), 8)
            self.download(f'{mode}.png')
            self.assertEqual(positions, self.page.evaluate('window.testNetwork.getPositions()'))
        # Move a real canvas node with the pointer, then restore the saved layout.
        self.page.get_by_role('button', name='显示全图').click()
        point = self.page.evaluate('window.testNetwork.canvasToDOM(window.testNetwork.getPositions([4])[4])')
        canvas = self.page.locator('#mynetwork canvas').bounding_box()
        self.page.mouse.move(canvas['x'] + point['x'], canvas['y'] + point['y'])
        self.page.mouse.down()
        self.page.mouse.move(canvas['x'] + point['x'] + 80, canvas['y'] + point['y'] + 60, steps=8)
        self.page.mouse.up()
        moved = self.page.evaluate('window.testNetwork.getPositions()')
        self.assertNotEqual(positions['4'], moved['4'])
        self.page.reload()
        self.assertIn('已恢复', self.page.locator('#status').inner_text())
        self.assertEqual(moved, self.page.evaluate('window.testNetwork.getPositions()'))
        self.page.screenshot(path=str(self.artifacts / 'workspace.png'), full_page=True)

    def test_export_is_independent_of_viewport_and_handles_size_limit(self):
        from PIL import Image, ImageChops
        first = self.download('full-view.png')
        self.page.evaluate('window.testNetwork.moveTo({position:{x:99999,y:99999},scale:.01})')
        second = self.download('panned-view.png')
        with Image.open(first) as a, Image.open(second) as b:
            self.assertEqual(a.size, b.size)
            self.assertIsNone(ImageChops.difference(a.convert('RGB'), b.convert('RGB')).getbbox())
        self.page.evaluate('window.testNetwork.moveNode(1, 1000000, 1000000)')
        self.page.get_by_role('button', name='下载白底 PNG').click()
        self.page.wait_for_function("document.getElementById('status').classList.contains('error')")
        self.assertIn('降低清晰度', self.page.locator('#status').inner_text())
        self.assertTrue(self.page.get_by_role('button', name='下载白底 PNG').is_enabled())

    def test_png_download_works_in_streamlit_style_sandboxed_iframe(self):
        from html import escape
        self.page.set_content('<iframe width="1400" height="920" '
            'sandbox="allow-scripts allow-same-origin allow-downloads" srcdoc="' + escape(self.html, quote=True) + '"></iframe>')
        frame = self.page.frame_locator('iframe')
        frame.get_by_role('button', name='固定布局').click()
        with self.page.expect_download() as pending:
            frame.get_by_role('button', name='下载白底 PNG').click()
        self.assertEqual(pending.value.suggested_filename, 'litkg-kb-1.png')
        pending.value.save_as(self.artifacts / 'iframe.png')


if __name__ == '__main__':
    unittest.main()
