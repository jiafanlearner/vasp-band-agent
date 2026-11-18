"""Run a synthetic cache test without reinstalling the package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from vasp_agent.models.cache_probe import main

if __name__ == '__main__':
    raise SystemExit(main())
