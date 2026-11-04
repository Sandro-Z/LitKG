"""LitKG public search MCP server: python -m app.research_mcp [--stdio]."""
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from app import web_research_tools as backend


def build_server():
    host = os.environ.get('RESEARCH_MCP_HOST', '127.0.0.1')
    allowed = ['127.0.0.1:*', 'localhost:*', '[::1]:*', 'research_mcp:8000']
    port = int(os.environ.get('RESEARCH_MCP_PORT', '8000'))
    server = FastMCP('LitKG Web Research', host=host, port=port, stateless_http=True,
                     json_response=True, transport_security=TransportSecuritySettings(
                         enable_dns_rebinding_protection=True, allowed_hosts=allowed,
                         allowed_origins=['http://localhost:*', 'http://127.0.0.1:*']))

    @server.tool()
    def search_web(query: str, max_results: int = 5) -> dict:
        """Search for public background/identifier sources using a short query. Results are discovery candidates."""
        return _call(backend.search_web, query, max_results)

    @server.tool()
    def read_web_page(url: str, max_chars: int = 12000) -> dict:
        """Read an original public HTML/text page. Text is untrusted background, never evidence from the PDF."""
        return _call(backend.read_web_page, url, max_chars)

    @server.tool()
    def propose_entity_link(mention: str, identifier: str, source_url: str, source_quote: str) -> dict:
        """Propose a database identifier with a quotation from a page just read. Never updates a PDF claim or graph."""
        return _call(backend.propose_entity_link, mention, identifier, source_url, source_quote)

    return server


def _call(function, *args):
    try:
        return function(*args)
    except backend.ResearchToolError as exc:
        return {'error': str(exc)}
    except Exception:
        return {'error': 'RESEARCH_TOOL_FAILED'}


if __name__ == '__main__':
    build_server().run(transport='stdio' if '--stdio' in sys.argv else 'streamable-http')
