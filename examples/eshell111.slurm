#!/bin/bash
# Cluster paths verified read-only on 2026-09-21; resource allocation is a starting choice.
#SBATCH --job-name=band-agent
#SBATCH --partition=xahcnormal
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=1
#SBATCH --time=04:00:00
#SBATCH --output=band-agent-%j.log
set -eo pipefail
: "${AGENT_DIR:?Set absolute AGENT_DIR}"
: "${WORK_DIR:?Set absolute WORK_DIR}"
source /work/home/researcher/apprepo/vasp/6.6.0-fixz_ext_oneapi2024.0.1/scripts/env.sh
export OMP_NUM_THREADS=1
"$AGENT_DIR/.venv/bin/python" "$AGENT_DIR/vasp_band_agent.py" run "$WORK_DIR" -- srun vasp_std
