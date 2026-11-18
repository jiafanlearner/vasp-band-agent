"""Every vasp_agent module an engine module imports must ship in the deployed release."""
import ast
from pathlib import Path

from vasp_agent.remote.remote_worker import ENGINE_MODULES, WORKER_UNITS

ROOT = Path(__file__).resolve().parents[1] / 'src'


def imported(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('vasp_agent.'):
            if node.module == 'vasp_agent.workflow':
                names |= {'vasp_agent.workflow.' + alias.name for alias in node.names}
            else:
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names if alias.name.startswith('vasp_agent.')}
    return names


def test_engine_imports_are_all_deployed():
    shipped = set(ENGINE_MODULES.values()) | {'vasp_agent.remote.' + unit for unit in WORKER_UNITS}
    missing = {}
    for module in ENGINE_MODULES.values():
        path = ROOT / (module.replace('.', '/') + '.py')
        # The chat CLI entry point is local-only; nothing on the cluster reaches it.
        wanted = {name for name in imported(path) if not name.startswith('vasp_agent.chat')}
        if wanted - shipped:
            missing[module] = sorted(wanted - shipped)
    assert missing == {}
