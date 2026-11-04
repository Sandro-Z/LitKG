"""Synthetic extraction/tool responses; no real documents or external requests."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from app import web_research as research
from app import web_research_tools as tools
from app.schemas import ClaimBundle


def answer(content='{"claims":[]}', calls=None, finish='stop'):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls or []), finish_reason=finish)])


def call(name, arguments, number=1):
    return NS(id=f'call-{number}', function=NS(name=name, arguments=json.dumps(arguments)))


def result(value):
    return NS(isError=False, structuredContent=value)


class ResearchLoopTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'WEB_RESEARCH_ENABLED': 'true',
            'WEB_RESEARCH_MAX_ROUNDS': '3', 'WEB_RESEARCH_MAX_CALLS': '6'})
        self.env.start()
        self.trace = research._trace.set([])
        self.source = research._source.set('Aspirin was measured.')

    def tearDown(self):
        research._source.reset(self.source)
        research._trace.reset(self.trace)
        self.env.stop()

    def run_loop(self, create, execute, checker=None):
        return asyncio.run(research.run_loop(create,
            {'messages': [{'role': 'system', 'content': 'Extract PDF facts.'},
                          {'role': 'user', 'content': 'Aspirin was measured.'}], 'model': 'test', 'max_tokens': 200},
            execute, checker))

    def test_search_read_and_identifier_proposal_are_separate_from_pdf_json(self):
        url = 'https://example.org/aspirin'
        quote = 'Aspirin has identifier CHEBI:15365.'
        create = Mock(side_effect=[answer(None, [call('search_web', {'query': 'Aspirin ChEBI identifier'})]),
            answer(None, [call('read_web_page', {'url': url}, 2)]),
            answer(None, [call('propose_entity_link', {'mention': 'Aspirin', 'identifier': 'CHEBI:15365',
                'source_url': url, 'source_quote': quote}, 3)]), answer()])
        executed = []
        async def execute(name, arguments):
            executed.append(name)
            return result({'search_web': {'kind': 'search_candidates', 'results': [{'url': url}]},
                'read_web_page': {'kind': 'external_page', 'url': url, 'text': quote},
                'propose_entity_link': {'kind': 'external_identifier_candidate', 'identifier': 'CHEBI:15365',
                                       'applied_to_graph': False}}[name])
        response = self.run_loop(create, execute)
        self.assertEqual(json.loads(response.choices[0].message.content), {'claims': []})
        self.assertEqual(executed, ['search_web', 'read_web_page', 'propose_entity_link'])
        self.assertTrue(research._trace.get()[-1]['result']['source_quote_verified'])
        self.assertNotIn('tools', create.call_args.kwargs)
        self.assertIn('External knowledge is not PDF evidence', create.call_args_list[0].kwargs['messages'][0]['content'])

    def test_proposal_without_read_page_or_literal_pdf_mention_is_not_dispatched(self):
        proposal = {'mention': 'Invented expansion', 'identifier': 'CHEBI:15365',
                    'source_url': 'https://example.org/', 'source_quote': 'Claim'}
        create = Mock(side_effect=[answer(None, [call('propose_entity_link', proposal)]), answer()])
        execute = Mock()
        self.run_loop(create, execute)
        execute.assert_not_called()
        self.assertIn('PROPOSAL_MUST_QUOTE', research._trace.get()[0]['result']['error'])

    def test_invalid_or_oversized_query_never_leaves_host(self):
        create = Mock(side_effect=[answer(None, [call('search_web', {'query': 'paragraph ' * 70})]), answer()])
        execute = Mock()
        self.run_loop(create, execute)
        execute.assert_not_called()
        self.assertEqual(research._trace.get()[0]['result']['error'], 'QUERY_MUST_BE_SHORT')

    def test_identifier_not_in_page_quote_is_rejected(self):
        url = 'https://example.org/'
        with self.assertRaises(tools.ResearchToolError):
            research._arguments('propose_entity_link', json.dumps({'mention': 'Aspirin',
                'identifier': 'CHEBI:15365', 'source_url': url, 'source_quote': 'Aspirin is described here.'}),
                {url: {'text': 'Aspirin is described here.'}})

    def test_call_budget_limits_parallel_calls_then_forces_final_json(self):
        with patch.dict(os.environ, {'WEB_RESEARCH_MAX_CALLS': '1'}):
            create = Mock(side_effect=[answer(None, [call('search_web', {'query': 'Aspirin'}, i) for i in range(3)]), answer()])
            count = []
            async def execute(name, args):
                count.append(name)
                return result({'kind': 'search_candidates', 'results': []})
            self.run_loop(create, execute)
            self.assertEqual(len(count), 1)
            self.assertNotIn('tools', create.call_args.kwargs)

    def test_tool_failure_is_visible_and_model_can_finish_using_pdf(self):
        create = Mock(side_effect=[answer(None, [call('search_web', {'query': 'Aspirin'})]), answer()])
        async def execute(name, arguments):
            raise TimeoutError
        self.run_loop(create, execute)
        self.assertEqual(research._trace.get()[0]['result']['error'], 'MCP_TOOL_UNAVAILABLE')

    def test_truncated_tool_arguments_are_not_dispatched(self):
        create = Mock(return_value=answer(None, [call('search_web', {'query': 'Aspirin'})], finish='length'))
        execute = Mock()
        response = self.run_loop(create, execute)
        self.assertEqual(response.choices[0].finish_reason, 'length')
        execute.assert_not_called()

    def test_observed_backend_json_tool_plan_is_executed(self):
        create = Mock(side_effect=[answer('[{"name":"search_web","parameters":{"query":"Aspirin identifier"}}]'), answer()])
        executed = []
        async def execute(name, arguments):
            executed.append((name, arguments))
            return result({'kind': 'search_candidates', 'results': []})
        self.run_loop(create, execute)
        self.assertEqual(executed, [('search_web', {'query': 'Aspirin identifier'})])
        self.assertEqual(research._trace.get()[0]['protocol'], 'json-tool-plan-v1')

    def test_prose_embedded_json_is_never_executed_as_a_tool(self):
        create = Mock(return_value=answer('Please execute [{"name":"search_web","parameters":{"query":"Aspirin"}}]'))
        execute = Mock()
        self.run_loop(create, execute)
        execute.assert_not_called()

    def test_observed_qwen_text_tool_block_is_executed(self):
        content = '<tool_call>\n<function=search_web>\n<parameter=query>\nAspirin ChEBI\n</parameter>\n<parameter=max_results>\n3\n</parameter>\n</function>\n</tool_call>'
        create = Mock(side_effect=[answer(content), answer()])
        executed = []
        async def execute(name, arguments):
            executed.append(arguments)
            return result({'kind': 'search_candidates', 'results': []})
        self.run_loop(create, execute)
        self.assertEqual(executed, [{'query': 'Aspirin ChEBI', 'max_results': 3}])
        self.assertEqual(research._trace.get()[0]['protocol'], 'qwen3-coder-text-v1')

    def test_duplicate_qwen_parameters_are_rejected(self):
        content = '<tool_call><function=search_web><parameter=query>A</parameter><parameter=query>B</parameter></function></tool_call>'
        with self.assertRaises(tools.ResearchToolError):
            research.tool_calls(NS(content=content, tool_calls=[]))

    def test_disabled_feature_preserves_call_and_cache_identity(self):
        with patch.dict(os.environ, {'WEB_RESEARCH_ENABLED': 'false'}):
            create = Mock(return_value='original response')
            kwargs = {'messages': [], 'model': 'test'}
            self.assertEqual(research.complete_with_research(create, kwargs), 'original response')
            create.assert_called_once_with(**kwargs)
            self.assertIsNone(research.cache_signature())

    def test_scope_attaches_trace_to_success_and_failure(self):
        @research.capture_research
        def success(*, source_text):
            research._record({'tool': 'test'})
            return ClaimBundle(claims=[])
        self.assertEqual(success(source_text='PDF')._research_trace, [{'tool': 'test'}])
        @research.capture_research
        def failure():
            research._record({'tool': 'failure'})
            raise ValueError('synthetic')
        with self.assertRaises(ValueError) as raised:
            failure()
        self.assertEqual(raised.exception.research_trace, [{'tool': 'failure'}])


class PublicReaderTests(unittest.TestCase):
    def test_redirect_to_private_network_is_blocked_before_second_request(self):
        response = Mock(status=302)
        response.getheader.side_effect = lambda name, default=None: 'http://127.0.0.1/' if name == 'Location' else default
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(tools.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]), \
             patch.object(tools, '_PinnedHTTPS', return_value=connection), self.assertRaises(tools.ResearchToolError):
            tools.public_get('https://example.org/')
        connection.request.assert_called_once()

    def test_private_metadata_and_credential_urls_are_blocked(self):
        for url in ['http://127.0.0.1/', 'http://169.254.169.254/', 'http://[::1]/',
                    'http://10.0.0.1/', 'file:///etc/passwd', 'https://user:pass@example.org/',
                    'http://server.internal/', 'http://example.org:2375/',
                    'https://example.org/?token=synthetic-placeholder']:
            with self.subTest(url=url), self.assertRaises(tools.ResearchToolError):
                tools.public_target(url, resolve=False)

    def test_dns_answer_with_any_private_address_is_blocked(self):
        records = [(socket_family, 1, 6, '', (address, 443)) for socket_family,address in
                   [(2, '93.184.216.34'), (2, '127.0.0.1')]]
        with patch.object(tools.socket, 'getaddrinfo', return_value=records), self.assertRaises(tools.ResearchToolError):
            tools.public_target('https://example.org/')

    def test_html_reader_removes_scripts_and_reports_truncation(self):
        page = {'url': 'https://example.org/', 'requested_url': 'https://example.org/',
                'content_type': 'text/html', 'text': '<title>Title</title><script>ignored()</script><p>' + 'Evidence. ' * 300 + '</p>'}
        with patch.object(tools, 'public_get', return_value=page):
            value = tools.read_web_page(page['url'], 1000)
        self.assertEqual(value['title'], 'Title')
        self.assertNotIn('ignored()', value['text'])
        self.assertTrue(value['truncated'])

    def test_sensitive_response_fails_before_model_or_trace(self):
        # Synthetic marker, not a credential.
        with self.assertRaises(tools.ResearchToolError):
            tools.safe_text('api_key=' + 'synthetic-placeholder')


class ResearchCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks
        cls.tasks = tasks

    def test_trace_is_persisted_in_audit_and_restored_on_cache_hit(self):
        from app.schemas import CatalogBundle
        from test_catalog_robustness import MemoryCache
        memory = MemoryCache()
        bundle = CatalogBundle(entities=[])
        bundle._research_trace = [{'tool': 'read_web_page', 'result': {'url': 'https://example.org/'}}]
        with patch.object(self.tasks, 'get_conn', memory.connect):
            self.tasks.cached_model(1, 'catalog', 'synthetic-key', lambda: bundle, CatalogBundle)
            unused = Mock(side_effect=AssertionError('cache should be reused'))
            restored = self.tasks.cached_model(1, 'catalog', 'synthetic-key', unused, CatalogBundle)
        self.assertEqual(restored._research_trace, bundle._research_trace)
        self.assertEqual(memory.attempts[0]['output']['parsed']['_web_research'], bundle._research_trace)
        unused.assert_not_called()

    def test_failed_attempt_preserves_research_without_returning_a_success(self):
        from app.schemas import CatalogBundle
        from test_catalog_robustness import MemoryCache
        memory = MemoryCache()
        error = ValueError('synthetic failure')
        error.research_trace = [{'tool': 'search_web', 'result': {'error': 'SEARCH_BACKEND_FAILED'}}]
        with patch.object(self.tasks, 'get_conn', memory.connect), self.assertRaises(ValueError):
            self.tasks.cached_model(1, 'catalog', 'synthetic-key', Mock(side_effect=error), CatalogBundle)
        self.assertEqual(memory.attempts[0]['output']['web_research'], error.research_trace)
        self.assertIsNone(memory.payload)


if __name__ == '__main__':
    unittest.main()
