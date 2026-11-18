"""Run synthetic native-compaction tests from the project without reinstalling."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from vasp_agent.models.compaction_probe import main

if __name__ == '__main__':
    raise SystemExit(main())
