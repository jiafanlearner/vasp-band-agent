import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat import agent_settings as settings


def settings_file(workspace):
    path = settings.path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_saved_settings_apply_unless_given_on_command_line(tmp_path):
    settings.save(tmp_path, {'timeout': 300.0, 'model_retries': 3, 'max_steps': 20, 'color': 'never'})
    # argparse has already put the explicit --max-steps 12 into args.
    args = NS(timeout=180.0, model_retries=5, max_steps=12, color='auto')
    notes = settings.apply_saved(args, ['--max-steps', '12'], tmp_path)
    assert (args.timeout, args.model_retries, args.max_steps, args.color) == (300.0, 3, 12, 'never')
    assert 'max_steps' not in notes[-1] and 'timeout=300.0' in notes[-1]
    args = NS(timeout=180.0, model_retries=5, max_steps=15, color='auto')
    settings.apply_saved(args, ['--max-steps=15'], tmp_path)
    assert args.max_steps == 15


def test_invalid_or_corrupt_settings_are_ignored_with_a_note(tmp_path):
    settings_file(tmp_path).write_text(json.dumps({'max_steps': 99, 'timeout': 200, 'unknown': 1}))
    args = NS(timeout=180.0, model_retries=5, max_steps=10, color='auto')
    notes = settings.apply_saved(args, [], tmp_path)
    assert args.max_steps == 10 and args.timeout == 200.0
    assert any('max_steps' in n for n in notes) and any('unknown' in n for n in notes)
    settings_file(tmp_path).write_text('{broken')
    args = NS(timeout=180.0, model_retries=5, max_steps=10, color='auto')
    assert '无法读取' in settings.apply_saved(args, [], tmp_path)[0] and args.max_steps == 10


@pytest.mark.parametrize('key,value', [('max_steps', '0'), ('max_steps', '2.5'), ('timeout', 'x'), ('color', 'blue'), ('foo', '1')])
def test_validate_rejects_out_of_range(key, value):
    with pytest.raises(ValueError):
        settings.validate(key, value)


def test_validate_normalizes():
    assert settings.validate('max_steps', '20') == 20 and settings.validate('timeout', '300') == 300.0


def test_auto_followup_switch():
    assert settings.validate('auto_followup', 'off') == 'off'
    with pytest.raises(ValueError):
        settings.validate('auto_followup', 'maybe')
