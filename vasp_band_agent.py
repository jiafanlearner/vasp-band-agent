"""Compatibility launcher for existing deterministic workflow CLI commands."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
application = import_module('vasp_agent.workflow.vasp_band_agent')
if __name__ == '__main__':
    application.main(chat_handler=lambda: import_module('vasp_agent.chat.ai_agent').main())
else:
    sys.modules[__name__] = application
