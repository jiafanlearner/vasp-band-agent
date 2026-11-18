#!/bin/bash
# TEMPLATE: replace partition/account/resource settings before submission.
#SBATCH --job-name=vasp-bands
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --time=01:00:00
#SBATCH --output=agent-%j.log
set -euo pipefail

# Load your site's VASP and Python modules here.
# module load ...
: "${AGENT_DIR:?Set AGENT_DIR to the installed agent directory}"
: "${WORK_DIR:?Set WORK_DIR to the prepared calculation directory}"
source "$AGENT_DIR/.venv/bin/activate"
python "$AGENT_DIR/vasp_band_agent.py" run "$WORK_DIR" -- srun vasp_std
