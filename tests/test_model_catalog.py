from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.model_catalog import list_models, select_model
from vasp_agent.models.model_config import resolve_config


def test_catalog_sorted_unique_without_generation():
    client = NS(models=NS(list=lambda: iter([NS(id='b'), NS(id='a'), NS(id='b')])))
    assert list_models(client) == ['a', 'b']


def test_selection_invalid_then_valid():
    answers = iter(['wrong', '99', '2'])
    assert select_model(['a', 'b'], lambda _: next(answers), lambda _: None) == 'b'


def test_exact_model_name():
    assert select_model(['vendor/model'], lambda _: 'vendor/model', lambda _: None) == 'vendor/model'


def test_cancel():
    assert select_model(['a'], lambda _: 'q', lambda _: None) is None


@pytest.mark.parametrize('status,fragment', [(401, '鉴权'), (403, '权限'), (404, '/models'), (429, '限流')])
def test_error_classification(status, fragment):
    def fail():
        error = RuntimeError('do-not-expose-provider-secret')
        error.status_code = status
        raise error
    with pytest.raises(ValueError, match=fragment) as caught:
        list_models(NS(models=NS(list=fail)))
    assert 'do-not-expose' not in str(caught.value)


def test_empty_catalog():
    with pytest.raises(ValueError, match='空模型列表'):
        list_models(NS(models=NS(list=lambda: [])))


def test_discovery_without_model(tmp_path):
    args = NS(workspace=str(tmp_path), config=None, env_file=None, profile=None)
    cfg = resolve_config(args, {'OPENAI_API_KEY': 'fake'}, allow_missing_model=True)
    assert cfg.model == '' and cfg.api_key == 'fake'
    with pytest.raises(ValueError, match='模型'):
        resolve_config(args, {'OPENAI_API_KEY': 'fake'})
