#!/usr/bin/env bash
# Fan a directory of blocks out as one Slurm job per block.
#
#   slurm_sweep.sh <blocks.txt> <outdir> [extra synth_area args...]
#
# blocks.txt has one block per line:  <top> <source1> [<source2> ...]
# Fields are whitespace-separated, so source paths must not contain spaces.
# Lines starting with # are ignored. Each job writes <outdir>/<top>.json.
# Without sbatch on PATH, blocks run sequentially in this shell.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIST="${1:?blocks list}"
OUT="${2:?output dir}"
shift 2
mkdir -p "$OUT"

while read -r top rest || [[ -n "$top" ]]; do
	[[ -z "$top" || "$top" == \#* ]] && continue
	# words after <top> are source files only; `--` keeps them from being parsed as options
	# shellcheck disable=SC2086
	"$HERE/slurm_synth_area.sh" --top "$top" -o "$OUT/$top.json" "$@" -- $rest
done < "$LIST"
