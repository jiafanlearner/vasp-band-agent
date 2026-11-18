"""Operator preferences changed with /settings, kept across restarts.

Stored in the workspace as .agent/state/agent-settings.json (not committed; see
common/agent_paths.py). An option given
explicitly on the command line wins over the saved value for that run.
"""
import json

from vasp_agent.common import agent_paths

SETTINGS_FILE = agent_paths.AGENT_DIR + '/' + agent_paths.FILES['settings'][0]
RULES = {'timeout': (float, 1, 1800), 'model_retries': (int, 0, 5), 'max_steps': (int, 1, 30)}
COLORS = ('auto', 'always', 'never')
SWITCHES = {'auto_followup': ('on', 'off'), 'ssh_transport': ('pooled', 'subprocess')}
OPTIONS = {'timeout': '--timeout', 'model_retries': '--model-retries', 'max_steps': '--max-steps', 'color': '--color',
           'auto_followup': '--auto-followup', 'ssh_transport': '--ssh-transport'}


def validate(key, value):
    """Normalize one setting or raise ValueError with the allowed range."""
    if key == 'color':
        if value not in COLORS:
            raise ValueError('color 须为 auto / always / never。')
        return value
    if key in SWITCHES:
        if value not in SWITCHES[key]:
            raise ValueError(f'{key} 须为 ' + ' / '.join(SWITCHES[key]) + '。')
        return value
    if key not in RULES:
        raise ValueError('用法：/settings timeout 300 | model_retries 5 | max_steps 20 | color auto | auto_followup on | ssh_transport pooled')
    kind, low, high = RULES[key]
    try:
        if isinstance(value, bool):
            raise ValueError
        number = kind(value)
        if kind is int and isinstance(value, float) and value != number:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f'{key} 须为{"整数" if kind is int else "数值"}，范围 {low}–{high}。') from None
    if not low <= number <= high:
        raise ValueError(f'{key} 范围为 {low}–{high}。')
    return number


def path(workspace):
    return agent_paths.file_path(workspace, 'settings')


def load(workspace):
    """Saved settings that are still valid, plus notes about any that were ignored."""
    try:
        data = json.loads(path(workspace).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError):
        return {}, [f'{SETTINGS_FILE} 无法读取，已忽略并使用默认设置。']
    if not isinstance(data, dict):
        return {}, [f'{SETTINGS_FILE} 格式无效，已忽略。']
    values, notes = {}, []
    for key, value in data.items():
        try:
            values[key] = validate(key, value)
        except ValueError as exc:
            notes.append(f'已忽略保存的设置 {key}：{exc}')
    return values, notes


def save(workspace, values):
    target = path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)


def explicit(argv, key):
    option = OPTIONS[key]
    return any(arg == option or arg.startswith(option + '=') for arg in argv)


def apply_saved(args, argv, workspace):
    """Fill args from saved settings unless the option was given on the command line."""
    values, notes = load(workspace)
    applied = {}
    for key, value in values.items():
        if not explicit(argv, key):
            setattr(args, key, value)
            applied[key] = value
    if applied:
        notes.append('已载入保存的设置：' + '，'.join(f'{k}={v}' for k, v in applied.items()))
    return notes
