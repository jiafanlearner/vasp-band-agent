import gzip
import importlib
import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.api_client import create_client


def test_transport_negotiates_gzip_and_decodes_with_real_sdk(monkeypatch):
    openai = pytest.importorskip('openai')
    # The installed SDK may use httpx or httpx2; use its own public client base.
    package = openai.DefaultHttpxClient.__mro__[1].__module__.split('.')[0]
    http = importlib.import_module(package)
    seen = []
    payload = {'id': 'resp_test', 'object': 'response', 'created_at': 0, 'model': 'test',
               'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_test',
                   'status': 'completed', 'role': 'assistant', 'content': [
                       {'type': 'output_text', 'text': 'summary-ok', 'annotations': []}]}]}
    def serve(request):
        seen.append(request)
        assert request.headers['accept-encoding'] == 'gzip, deflate'
        return http.Response(200, headers={'content-type': 'application/json', 'content-encoding': 'gzip'},
                             content=gzip.compress(json.dumps(payload).encode()))
    transport = openai.DefaultHttpxClient(transport=http.MockTransport(serve))
    real_constructor = openai.OpenAI
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: real_constructor(**kwargs, http_client=transport))
    config = NS(api_key='fake', base_url='https://example.invalid/v1')
    with create_client(config, 15) as client:
        response = client.responses.create(model='test', input='synthetic')
        assert response.output_text == 'summary-ok'
        assert client.max_retries == 0 and client.timeout == 15
    assert str(seen[0].url) == 'https://example.invalid/v1/responses'
