#!/usr/bin/env bash
# Submit one synth_area run as a Slurm batch job (or run locally when sbatch is
# missing). All arguments are passed through to synth_area.py unchanged.
#
#   slurm_synth_area.sh --top fifo fifo.sv --liberty cells.lib -o out/fifo.json
#
# Tunables (environment):
#   SYNTH_AREA_PARTITION   Slurm partition               (default: unset -> cluster default)
#   SYNTH_AREA_CPUS        cpus-per-task                 (default: 1; yosys is single-threaded)
#   SYNTH_AREA_MEM         memory per job                (default: 4G)
#   SYNTH_AREA_TIME        time limit                    (default: 01:00:00)
#   SYNTH_AREA_JOBNAME     job name                      (default: synth_area)
#   SYNTH_AREA_LOGDIR      where slurm-%j.out goes       (default: ./slurm_logs)
#   SYNTH_AREA_SBATCH_ARGS extra sbatch flags (e.g. "--account=chip --qos=fast")
#   SYNTH_AREA_LOCAL=1     force local execution even if sbatch exists
#   YOSYS / SV2V           tool paths forwarded to the job environment
#
# Prints the job id (sbatch) or runs synchronously (local).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$HERE/synth_area.py"
PYTHON="${PYTHON:-python3}"

if [[ $# -eq 0 ]]; then
	sed -n '2,20p' "$0"
	exit 2
fi

if [[ "${SYNTH_AREA_LOCAL:-0}" == "1" ]] || ! command -v sbatch >/dev/null 2>&1; then
	exec "$PYTHON" "$RUNNER" "$@"
fi

LOGDIR="${SYNTH_AREA_LOGDIR:-./slurm_logs}"
mkdir -p "$LOGDIR"

SBATCH_ARGS=(
	--job-name="${SYNTH_AREA_JOBNAME:-synth_area}"
	--cpus-per-task="${SYNTH_AREA_CPUS:-1}"
	--mem="${SYNTH_AREA_MEM:-4G}"
	--time="${SYNTH_AREA_TIME:-01:00:00}"
	--output="$LOGDIR/slurm-%j.out"
	--export=ALL
	--parsable
)
if [[ -n "${SYNTH_AREA_PARTITION:-}" ]]; then
	SBATCH_ARGS+=(--partition="$SYNTH_AREA_PARTITION")
fi
if [[ -n "${SYNTH_AREA_SBATCH_ARGS:-}" ]]; then
	# shellcheck disable=SC2206
	SBATCH_ARGS+=(${SYNTH_AREA_SBATCH_ARGS})
fi

# printf %q so paths with spaces survive the trip through the job script.
ARGS_Q=$(printf '%q ' "$@")
sbatch "${SBATCH_ARGS[@]}" --wrap="cd $(printf '%q' "$PWD") && $(printf '%q' "$PYTHON") $(printf '%q' "$RUNNER") $ARGS_Q"
