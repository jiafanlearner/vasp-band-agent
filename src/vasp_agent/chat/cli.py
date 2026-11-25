"""Interactive CLI entry point, monitor follow-up routing, and session lifecycle helpers."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.toolbox_core import scrub


def followup_message(events):
    """Turn terminal monitor events into one clearly labelled, non-operator turn."""
    lines = []
    for event in events:
        where = f"{event.get('server') or '本机'} 上的任务 {event.get('job_name') or '?'}（JobID {event.get('job_id') or '?'}"
        where += f"，阶段 {event.get('phase')}）" if event.get('phase') else '）'
        state = f"Slurm {event.get('state') or event.get('scheduler_state') or 'unknown'}"
        if event.get('workflow_status'):
            state += f"，工作流 {event['workflow_status']}"
        if event.get('next_phase'):
            state += f"，下一阶段 {event['next_phase']}"
        lines.append(f'- {where}已结束：{state}。')
    return ('[自动跟进 · 监控事件，非操作者输入]\n' + '\n'.join(lines) + '\n'
            '请只读核验真实结果并汇报结论。成功且到达审阅点时完成本阶段审阅；若下一步是提交下一阶段，'
            '用 request_submission 打开操作者确认菜单，由操作者现场确认，本消息不构成任何授权。'
            '失败或受阻时诊断原因并给出新任务方案，不得自动重交。')


def route_followups(commands, events, deferred, output):
    """Events to follow up in the open conversation now; the rest wait for their own."""
    suffix = '-' + commands.config.identity()
    def owner(event):
        conversation = event.get('conversation')
        if conversation and conversation.endswith(suffix):
            return conversation[:-len(suffix)]
        return None  # older registrations, or another endpoint's conversation
    now = list(deferred.pop(commands.name, []))
    for event in events:
        name = owner(event)
        if name is None or name == commands.name:
            now.append(event)
        else:
            deferred.setdefault(name, []).append(event)
    if not now and deferred and not commands.agent.history:
        name = next(iter(deferred))
        try:
            commands.switch(name)
        except Exception:
            output(f'无法打开提交该作业的对话 {name}；在当前对话中跟进。')
            return deferred.pop(name)
        return deferred.pop(name, [])
    for name, waiting in deferred.items():
        jobs = '、'.join(f"{e.get('job_name')}（JobID {e.get('job_id')}）" for e in waiting)
        output(f'{jobs} 由对话 {name} 提交；输入 /sessions {name} 进入后自动跟进。')
    return now


def answer(ui, agent, message, config=None, operator=True):
    """One interactive turn that never ends the session."""
    try:
        ui.reply(agent.ask(message, operator=operator))
    except Exception as exc:
        with contextlib.suppress(Exception):
            core.save(agent.file, agent.history)
        detail = str(exc)
        if config and getattr(config, 'api_key', None):
            detail = detail.replace(config.api_key, '[REDACTED KEY]')
        ui.reply(f'本次请求未完成：{scrub(detail) or type(exc).__name__}。对话已保存，可用 /retry 继续。')


def session_lock(agent, hold):
    """Hold or release a conversation's open-session lock; stand-in agents have none."""
    method = getattr(agent, 'hold_session' if hold else 'release_session', None)
    if callable(method):
        method()


def _facade_classes():
    """Resolve Agent/Toolbox via the ai_agent facade so monkeypatches on ai_agent apply."""
    from vasp_agent.chat import ai_agent
    return ai_agent.Agent, ai_agent.Toolbox


def main():
    parser = argparse.ArgumentParser(description='Natural-language supervisor using the OpenAI Responses tool-calling API.')
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--model')
    parser.add_argument('--profile')
    parser.add_argument('--config', help='models.toml 路径；默认工作区根目录')
    parser.add_argument('--env-file', help='.env 路径；默认工作区根目录')
    parser.add_argument('--base-url')
    parser.add_argument('--protocol', choices=['responses', 'chat_completions'])
    parser.add_argument('--api-key-env', help='保存密钥的环境变量名称，不是密钥正文')
    parser.add_argument('--network-mode', choices=['auto', 'direct'],
                        help='auto 使用原网络设置；direct 仅关闭模型 HTTP 客户端的应用代理，仍验证证书')
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check-config', action='store_true', help='离线检查，不发送网络请求')
    checks.add_argument('--test-connection', action='store_true', help='发送两个小请求测试工具调用及回传')
    checks.add_argument('--list-models', action='store_true', help='从所选接口读取模型列表，不需要提前填写模型')
    parser.add_argument('--select-model', action='store_true', help='获取模型列表并交互选择本次使用的模型')
    parser.add_argument('--session', default='default')
    parser.add_argument('--potcar', help='操作者配置的拼接 PBE POTCAR；不向模型发送正文')
    parser.add_argument('--potcar-library', help='本地 PBE 库目录，默认自动发现工作区 potentials/PBE')
    parser.add_argument('--submit-script', help='操作者审核的 Slurm 脚本绝对路径')
    parser.add_argument('--agent-dir', help='集群已安装程序及 .venv 的目录')
    parser.add_argument('--allow-submit', action='store_true', help='旧版参数；交互模式请使用 /submit once')
    parser.add_argument('--max-submissions', type=int, default=1)
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--context-window', type=int, help='手动配置上下文总token预算，非自动检测容量')
    parser.add_argument('--output-reserve', type=int, help='输出token预留及请求输出上限')
    parser.add_argument('--context-safety', type=int, help='上下文安全预留tokens')
    parser.add_argument('--compact-trigger', type=float, help='自动压缩触发比例，例如0.8')
    parser.add_argument('--compact-target', type=float, help='压缩目标比例，例如0.55')
    parser.add_argument('--compaction-mode', choices=['summary', 'native'], help='默认summary结构化摘要；native须显式选择且接口支持')
    parser.add_argument('--recent-context-tokens', type=int, help='近期完整轮次保留预算，默认12000；还受压缩目标限制')
    parser.add_argument('--context-chars', type=int, default=None,
                        help='显式启用旧字符阈值模式；0关闭自动压缩；省略则按token预算管理')
    parser.add_argument('--timeout', type=float, default=180, help='模型接口请求超时秒数，默认180')
    parser.add_argument('--model-retries', type=int, default=5, help='模型临时故障自动重试次数0–5，默认5；0关闭')
    parser.add_argument('--color', choices=['auto', 'always', 'never'], default='auto', help='终端颜色模式')
    parser.add_argument('--auto-followup', choices=['on', 'off'], default='on',
                        help='监控到作业结束时自动核验并说明下一步（不自动提交），默认 on')
    parser.add_argument('--ssh-transport', choices=['pooled', 'subprocess'], default='pooled',
                        help='pooled 复用一条 SSH 连接（需 paramiko，不可用时自动退回）；subprocess 每次新建 ssh 进程')
    parser.add_argument('--prompt', help='单次请求；省略则进入交互对话')
    parser.add_argument('--migrate-workspace', nargs='?', const='plan', choices=['plan', 'apply', 'revert-plan', 'revert'],
                        help='把工作区根目录的旧运行文件移入 .agent/：plan 预览（默认）、apply 执行、revert-plan/revert 撤销；不启动对话')
    args = parser.parse_args()
    if args.migrate_workspace:
        from vasp_agent.common import workspace_migration
        parser.exit(workspace_migration.run(args.workspace, args.migrate_workspace))
    from vasp_agent.common import agent_paths
    if agent_paths.migration_running(args.workspace):
        parser.exit(1, '工作区迁移正在进行；完成后再启动 Agent。\n')
    from vasp_agent.chat import agent_settings
    # /settings values persist in the workspace; explicit command-line options win.
    settings_notes = agent_settings.apply_saved(args, sys.argv[1:], args.workspace)
    from vasp_agent.remote import ssh_pool
    ssh_pool.configure(args.ssh_transport)
    if args.context_chars is not None and args.context_chars != 0 and args.context_chars < 16000:
        parser.error('--context-chars 必须为0或至少16000')
    from vasp_agent.ui.terminal_ui import TerminalUI
    ui = TerminalUI(args.color, workspace=args.workspace)
    for note in settings_notes:
        print(note, file=sys.stderr)
    if args.select_model and (args.check_config or args.list_models):
        parser.error('--select-model 不能与 --check-config 或 --list-models 同时使用')
    if not 0 <= args.max_submissions <= 10:
        parser.error('--max-submissions 必须在 0 到 10 之间')
    if not 1 <= args.timeout <= 1800:
        parser.error('--timeout 必须在 1 到 1800 秒之间')
    if not 0 <= args.model_retries <= 5:
        parser.error('--model-retries 必须为 0–5')
    config = None
    try:
        from vasp_agent.models.model_config import resolve_config
        from vasp_agent.models.model_adapters import adapter_for, test_connection
        config = resolve_config(args, allow_missing_model=not args.check_config)
        if args.check_config:
            print(json.dumps(config.public(), ensure_ascii=False, indent=2))
            if not config.api_key:
                parser.exit(2, f'未配置 {config.api_key_env}。\n')
            return
        if not config.api_key:
            raise ValueError(f'请在 .env 或环境变量中配置 {config.api_key_env}。')
        import importlib
        importlib.import_module('openai')
        importlib.import_module('jsonschema')  # Validate installation before the first paid request.
        from vasp_agent.models.api_client import create_client
        client = create_client(config, args.timeout)
        from vasp_agent.models.model_catalog import list_models, display_models, select_model
        if args.list_models:
            display_models(list_models(client))
            return
        if args.select_model or not config.model:
            if not sys.stdin.isatty():
                raise ValueError('交互选择需要终端。请先 --list-models，再用 --model 指定模型。')
            chosen = select_model(list_models(client))
            if chosen is None:
                print('已取消选择，没有发送生成请求。')
                return
            config.model = chosen
        adapter = adapter_for(client, config.protocol)
        if args.test_connection:
            print(json.dumps(test_connection(adapter, config.model), ensure_ascii=False))
            return
        agent_cls, toolbox_cls = _facade_classes()
        toolbox = toolbox_cls(args.workspace, args.potcar, args.submit_script, args.agent_dir,
                              False, args.max_submissions, args.potcar_library)
        if args.allow_submit:
            print('提交权限默认关闭；远程提交会打开操作者确认菜单，/submit 可设置默认资源。')
        toolbox.require_scan_plans = True
        toolbox.unified_plan_confirm = True
        toolbox.lazy_tool_groups = True
        if not args.prompt:
            toolbox.plan_reader = ui.read_menu
        session = f'{args.session}-{config.identity()}'
        agent = agent_cls(client, config.model, toolbox, session, args.max_steps, adapter=adapter, ui=ui, context_chars=args.context_chars, context_config=config, model_retries=args.model_retries)
        session_lock(agent, hold=True)
        print(json.dumps(config.public(), ensure_ascii=False), file=sys.stderr)
        if args.prompt:
            ui.reply(agent.ask(None if args.prompt == '/retry' else args.prompt))
            return
        from vasp_agent.chat.chat_commands import Commands
        usage = agent.usage
        def agent_factory(name):
            cls, _ = _facade_classes()
            result = cls(client, config.model, toolbox, f'{name}-{config.identity()}',
                         args.max_steps, adapter=adapter, ui=ui, context_chars=args.context_chars, context_config=config, model_retries=args.model_retries)
            result.usage = usage
            session_lock(result, hold=True)
            return result
        def runtime(c, command, argument):
            nonlocal config, client, adapter
            from copy import copy
            from vasp_agent.chat.extra_commands import choose
            if command == '/settings':
                if argument:
                    key, _, value = argument.partition(' ')
                    number = agent_settings.validate(key, value.strip())
                    setattr(args, key, number)
                    if key == 'timeout':
                        client.timeout = number
                    elif key == 'model_retries':
                        c.agent.model_retries = number
                    elif key == 'max_steps':
                        c.agent.max_steps = number
                    elif key == 'color':
                        ui.color = number
                    elif key == 'ssh_transport':
                        ssh_pool.configure(number)
                    try:
                        agent_settings.save(args.workspace, {name: getattr(args, name) for name in agent_settings.OPTIONS})
                        c.output(f'已保存到 {agent_settings.SETTINGS_FILE}，重启后仍然生效（命令行显式参数优先）。')
                    except OSError:
                        c.output('设置已在本次生效，但写入设置文件失败，重启后会恢复。')
                c.output(json.dumps({'timeout': args.timeout, 'model_retries': args.model_retries, 'max_steps': args.max_steps,
                                     'auto_followup': args.auto_followup, 'ssh_transport': args.ssh_transport,
                                     'color': ui.color, 'colors': ui.palette}))
                return
            if command == '/connection':
                c.output(json.dumps(test_connection(adapter, config.model), ensure_ascii=False))
                return
            options = copy(args)
            if command == '/model':
                ids = list_models(client)
                selected = argument or choose(c, ids, '选择模型')
                if not selected:
                    return
                if selected not in ids:
                    raise ValueError('模型不在接口返回列表中。')
                candidate = copy(config)
                candidate.model = selected
            else:
                if not argument:
                    import tomllib
                    path = Path(args.config).resolve() if args.config else Path(args.workspace).resolve() / 'models.toml'
                    data = tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'profiles': {'official': {}}}
                    argument = choose(c, sorted(data.get('profiles', {})), '选择接口配置')
                if not argument:
                    return
                options.profile = argument
                options.model = options.base_url = options.api_key_env = options.protocol = None
                options.network_mode = None
                candidate = resolve_config(options, allow_missing_model=True)
            if not candidate.api_key:
                raise ValueError(f'未配置 {candidate.api_key_env}。')
            new_client = create_client(candidate, args.timeout)
            try:
                if not candidate.model:
                    selected = choose(c, list_models(new_client), '选择模型')
                    if not selected:
                        new_client.close()
                        return
                    candidate.model = selected
                new_adapter = adapter_for(new_client, candidate.protocol)
                next_name = c.name
                if candidate.identity() != config.identity() and c.agent.history:
                    next_name = c.prepare_transfer(c.agent.history, c.name, candidate)
                    if next_name is None:
                        new_client.close()
                        return
                cls, _ = _facade_classes()
                replacement = cls(new_client, candidate.model, toolbox, f'{next_name}-{candidate.identity()}',
                                  args.max_steps, adapter=new_adapter, ui=ui, context_chars=args.context_chars, context_config=candidate, model_retries=args.model_retries)
            except Exception:
                new_client.close()
                raise ValueError('切换未完成，请检查配置和接口；原对话保持不变。') from None
            replacement.usage = usage
            session_lock(c.agent, hold=False)
            session_lock(replacement, hold=True)
            if candidate.identity() != config.identity() and toolbox.revoke_submission('model_switch'):
                c.output('已撤销本会话的提交授权（切换模型后需重新确认）。')
            old_client = client
            config, client, adapter = candidate, new_client, new_adapter
            c.config, c.agent, c.name = candidate, replacement, next_name
            old_client.close()
            c.output(json.dumps(candidate.public(), ensure_ascii=False))
            c.output('已切换；旧对话上下文已保留（若原对话为空则保持空白）。未重放工具；可用 /sessions 查看所有接口的对话。')
        commands = Commands(agent, agent_factory, config, args.session, ui,
                            reader=ui.read_menu, runtime=runtime)
        print('VASP AI 助理。输入 / 查看菜单；/sessions 切换对话；/new 新建；/exit 退出。')
        from prompt_toolkit.patch_stdout import patch_stdout
        from vasp_agent.monitoring.polling import poll_job
        with patch_stdout(raw=True) if sys.stdin.isatty() else contextlib.nullcontext():
            monitor = toolbox.monitor
            from vasp_agent.monitoring.job_monitor import status_line
            ui.status_provider = lambda: status_line(monitor)
            import queue
            import threading
            followups, idle = queue.Queue(), threading.Event()
            def notify(event):
                if not event.get('manual'):  # /watch check already printed its result
                    ui.job_notification(event)
                if event.get('kind') == 'terminal' and args.auto_followup == 'on':
                    followups.put(event)
                    if idle.is_set():
                        ui.interrupt_prompt()
            try:
                monitor.start(lambda target: poll_job(toolbox.workspace, target), notify)
                deferred = {}
                while True:
                    events = []
                    while not followups.empty():
                        events.append(followups.get_nowait())
                    if (events or deferred and commands.name in deferred) and args.auto_followup == 'on':
                        events = route_followups(commands, events, deferred, print)
                        if events:
                            answer(ui, commands.agent, followup_message(events), config, operator=False)
                            continue
                    idle.set()
                    try:
                        message = ui.prompt()
                    finally:
                        idle.clear()
                    if message == ui.AUTO_PROMPT:
                        continue
                    message = message.strip()
                    action = commands.handle(message)
                    if action == 'exit':
                        break
                    if message and action == 'pass':
                        answer(ui, commands.agent, message, config)
            finally:
                monitor.stop()
                ssh_pool.close_all()
    except ImportError:
        parser.exit(2, '请先安装 AI 依赖：python -m pip install -e ".[ai]"\n')
    except (KeyboardInterrupt, EOFError):
        print('\n已退出；已提交的集群作业继续运行。')
    except Exception as exc:
        message = str(exc)
        if config and config.api_key:
            message = message.replace(config.api_key, '[REDACTED KEY]')
        parser.exit(1, f'AI 助理错误：{scrub(message)}\n')


def workflow_main():
    """Console entry point for `vasp-band-agent`, injecting the local chat handler."""
    return core.main(chat_handler=main)
