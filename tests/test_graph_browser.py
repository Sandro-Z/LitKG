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
            {'id': 3, 'source': 2, 'target': 1, 'label': 'AFFECTS'},
            {'id': 4, 'source': 4, 'target': 2, 'label': 'INHIBITS'},
            {'id': 5, 'source': 2, 'target': 3, 'label': 'HAS_SITE'},
            {'id': 6, 'source': 3, 'target': 5, 'label': 'ASSOCIATED_WITH'},
            {'id': 7, 'source': 6, 'target': 5, 'label': 'AFFECTS', 'negated': True},
            {'id': 8, 'source': 1, 'target': 1, 'label': 'INTERACTS_WITH'},
            {'id': 9, 'source': 7, 'target': 8, 'label': 'ASSOCIATED_WITH'},
        ]}
        from app.entity_types import TYPE_DEFINITIONS
        from app.predicate_registry import PREDICATES
        assert {node['type'] for node in cls.graph['nodes']} <= set(TYPE_DEFINITIONS)
        assert {edge['label'] for edge in cls.graph['edges']} <= set(PREDICATES)
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

    def test_export_is_independent_of_viewport(self):
        from PIL import Image, ImageChops
        first = self.download('full-view.png')
        self.page.evaluate('window.testNetwork.moveTo({position:{x:99999,y:99999},scale:.01})')
        second = self.download('panned-view.png')
        with Image.open(first) as a, Image.open(second) as b:
            self.assertEqual(a.size, b.size)
            self.assertIsNone(ImageChops.difference(a.convert('RGB'), b.convert('RGB')).getbbox())

    def test_large_png_is_downloaded_at_original_size_beyond_previous_application_limit(self):
        from PIL import Image
        # A wide, short graph verifies the old 16000-pixel rejection without
        # allocating an enormous square canvas in the test browser.
        self.page.locator('#resolution').select_option('1')
        self.page.evaluate('''() => {
            for (let id = 1; id <= 6; id++) window.testNetwork.moveNode(id, id * 300, 0);
            window.testNetwork.moveNode(7, 18000, 0);
            window.testNetwork.moveNode(8, 18500, 0);
        }''')
        expected = self.page.evaluate('''() => {
            const positions = window.testNetwork.getPositions();
            const context = document.createElement('canvas').getContext('2d');
            context.font = '14px Arial, sans-serif';
            const bounds = GraphView.exportBounds(graphData.nodes.map(node => window.testNetwork.getBoundingBox(node.id)),
                graphData.edges, positions, label => ({
                    width: Math.max(...label.split('\\n').map(line => context.measureText(line).width)),
                    height: label.split('\\n').length * 18,
                }));
            return Math.ceil(bounds.right - bounds.left + 80);
        }''')
        positions = self.page.evaluate('window.testNetwork.getPositions()')
        path = self.download('large-original.png')
        with Image.open(path) as image:
            self.assertGreater(image.width, 16000)
            self.assertEqual(image.width, expected)
        self.assertEqual(positions, self.page.evaluate('window.testNetwork.getPositions()'))

    def test_native_browser_encoding_failure_has_visible_error_and_allows_retry(self):
        self.page.evaluate('''() => {
            window.originalToBlob = HTMLCanvasElement.prototype.toBlob;
            HTMLCanvasElement.prototype.toBlob = function(callback) { callback(null); };
        }''')
        self.page.get_by_role('button', name='下载白底 PNG').click()
        self.page.wait_for_function("document.getElementById('status').classList.contains('error')")
        self.assertIn('浏览器', self.page.locator('#status').inner_text())
        self.assertIn('原尺寸 PNG', self.page.locator('#status').inner_text())
        self.assertTrue(self.page.get_by_role('button', name='下载白底 PNG').is_enabled())
        status = self.page.locator('#status').bounding_box()
        canvas = self.page.locator('#mynetwork').bounding_box()
        self.assertLess(status['y'], canvas['y'])
        self.page.evaluate('() => { HTMLCanvasElement.prototype.toBlob = window.originalToBlob; }')
        self.download('retried.png')

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
