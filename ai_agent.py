"""Compatibility launcher: python ai_agent.py remains supported."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
application = import_module('vasp_agent.chat.ai_agent')
if __name__ == '__main__':
    application.main()
else:
    sys.modules[__name__] = application
