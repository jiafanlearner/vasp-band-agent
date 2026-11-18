"""Explicit endpoint configuration. Never infer a provider or fall back."""
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high')


@dataclass
class ModelConfig:
    profile: str
    base_url: str
    model: str
    protocol: str
    api_key_env: str
    api_key: str = field(repr=False)

    context_window: int = 32768
    output_reserve: int = 4000
    context_safety: int = 2048
    compact_trigger: float = .8
    compact_target: float = .55
    compaction_mode: str = 'summary'
    recent_context_tokens: int = 12000
    network_mode: str = 'auto'
    # Reasoning effort for context-summary requests only; None sends no setting.
    summary_reasoning_effort: str = None

    def public(self):
        return {name: getattr(self, name) for name in
                ('profile', 'base_url', 'model', 'protocol', 'api_key_env', 'context_window', 'output_reserve', 'context_safety', 'compact_trigger', 'compact_target', 'compaction_mode', 'recent_context_tokens', 'network_mode', 'summary_reasoning_effort')} | {
                    'api_key': '[configured]' if self.api_key else '[missing]'}

    def identity(self):
        return hashlib.sha256(f'{self.base_url}|{self.protocol}|{self.model}'.encode()).hexdigest()[:12]


def resolve_config(args, environ=None, allow_missing_model=False):
    from dotenv import dotenv_values
    env = dict(os.environ if environ is None else environ)
    root = Path(args.workspace).resolve()
    # Explicit files only: no ancestor search and no interpolation of secrets.
    dotenv = Path(args.env_file).resolve() if args.env_file else root / '.env'
    if args.env_file and not dotenv.is_file():
        raise ValueError('指定的 .env 文件不存在。')
    values = dotenv_values(dotenv, encoding='utf-8-sig', interpolate=False) if dotenv.is_file() else {}
    env = {**{k: v for k, v in values.items() if v is not None}, **env}
    path = Path(args.config).resolve() if args.config else root / 'models.toml'
    if args.config and not path.is_file():
        raise ValueError('指定的 models.toml 文件不存在。')
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    except tomllib.TOMLDecodeError:
        raise ValueError('models.toml 语法错误，请检查 TOML 格式。') from None
    profile = args.profile or env.get('VASP_AGENT_PROFILE') or data.get('default_profile', 'official')
    profiles = data.get('profiles', {})
    if profile not in profiles:
        if data or profile != 'official':
            raise ValueError('选择的 profile 不存在。')
        selected = dict(base_url='https://api.openai.com/v1', api_key_env='OPENAI_API_KEY', protocol='responses')
    else:
        selected = profiles[profile]
    if not isinstance(selected, dict) or set(selected) - {'base_url', 'api_key_env', 'model', 'protocol', 'context_window', 'output_reserve', 'context_safety', 'compact_trigger', 'compact_target', 'compaction_mode', 'recent_context_tokens', 'network_mode', 'summary_reasoning_effort'}:
        raise ValueError('profile 含未知字段；密钥只能存入 .env 或环境变量。')
    def pick(name, env_name):
        return getattr(args, name, None) or env.get(env_name) or selected.get(name, '')
    base = pick('base_url', 'VASP_AGENT_BASE_URL')
    model = pick('model', 'VASP_AGENT_MODEL')
    protocol = pick('protocol', 'VASP_AGENT_PROTOCOL')
    key_env = pick('api_key_env', 'VASP_AGENT_API_KEY_ENV')
    network_mode = getattr(args, 'network_mode', None)
    if network_mode is None:
        network_mode = env.get('VASP_AGENT_NETWORK_MODE', selected.get('network_mode', 'auto'))
    if network_mode not in ('auto', 'direct'):
        raise ValueError('network_mode 必须为 auto 或 direct。')
    if not all(isinstance(v, str) for v in (base, model, protocol, key_env, profile)):
        raise ValueError('模型配置字段必须是字符串。')
    url = urlsplit(base)
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('base_url 必须是无内嵌凭据、查询或 fragment 的 HTTP(S) 接口地址。')
    if protocol not in ('responses', 'chat_completions'):
        raise ValueError('protocol 必须显式指定 responses 或 chat_completions。')
    mode = pick('compaction_mode', 'VASP_AGENT_COMPACTION_MODE') or 'summary'
    if mode not in ('summary', 'native'):
        raise ValueError('compaction_mode 必须为 summary 或 native。')
    if mode == 'native' and protocol != 'responses':
        raise ValueError('native 压缩仅支持 responses 协议；请显式选择 summary 或更改协议。')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_env):
        raise ValueError('api_key_env 必须是有效环境变量名。')
    if not model.strip() or '填写' in model or model == 'YOUR_MODEL_ID':
        if not allow_missing_model:
            raise ValueError('请填写模型 ID，或使用 --list-models / --select-model 从接口获取模型。')
        model = ''
    settings = {}
    for name, default in [('context_window', 32768), ('output_reserve', 4000), ('context_safety', 2048), ('compact_trigger', .8), ('compact_target', .55), ('recent_context_tokens', 12000)]:
        value = getattr(args, name, None)
        if value is None:
            value = env.get('VASP_AGENT_' + name.upper(), selected.get(name, default))
        try:
            if isinstance(value, bool):
                raise ValueError()
            settings[name] = int(value) if isinstance(default, int) else float(value)
            if isinstance(default, int) and isinstance(value, float) and value != settings[name]:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError('上下文配置字段无效：' + name) from None
    from vasp_agent.models.context_budget import ContextBudget
    ContextBudget(settings['context_window'], settings['output_reserve'], settings['context_safety'], settings['compact_trigger'], settings['compact_target'])
    if settings['recent_context_tokens'] < 1024:
        raise ValueError('recent_context_tokens 须至少为1024；实际保留预算还受压缩目标限制。')
    effort = env.get('VASP_AGENT_SUMMARY_REASONING_EFFORT', selected.get('summary_reasoning_effort')) or None
    if effort not in (None, *REASONING_EFFORTS):
        raise ValueError('summary_reasoning_effort 须为 ' + ' / '.join(REASONING_EFFORTS) + '。')
    return ModelConfig(profile, base.rstrip('/'), model, protocol, key_env, env.get(key_env, ''), compaction_mode=mode,
                       network_mode=network_mode, summary_reasoning_effort=effort, **settings)
