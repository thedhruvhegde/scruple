#!/usr/bin/env bash
# Re-download the real (public) data scruple's real-data evals are built from,
# at pinned commits, over anonymous git, and verify every file's sha256.
#
#   scripts/fetch_real_data.sh [DEST]     # default: ${XDG_CACHE_HOME:-~/.cache}/scruple/real-data
#
# Sources, commits and hashes are pinned in scruple/realdata/provenance.py; this
# script must stay in sync with it (tests/test_realdata_provenance.py checks).
# Licences: CFPB and eCFR data are public domain; chargeback codes are CC BY 4.0.
# Then rebuild the frozen splits with: uv run scruple realdata build-splits --src DEST
set -euo pipefail

DEST="${1:-${XDG_CACHE_HOME:-$HOME/.cache}/scruple/real-data}"
PY="${PYTHON:-uv run python}"
if [ -n "${SCRUPLE_FETCH_WORK:-}" ]; then
  WORK="$SCRUPLE_FETCH_WORK"  # caller-owned scratch dir (must not exist yet); kept
  mkdir "$WORK"
else
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
fi
export GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0

# name url commit
REPOS=(
  "cfpb-2012-2015 https://github.com/jiegzhan/multi-class-text-classification-cnn e75a4b1fafb011873055c2d39ce7a48f835f0c86"
  "cfpb-checking-2025-2026 https://github.com/mhoehler22/cfpb-agglomerative-clustering 5ddb49aeb18218b10d11df15cfe60a5dbe8ece09"
  "ecfr-title-12 https://github.com/xerj-org/corpus-ecfr-title-12 4dc0246c43bef051d64cb023596993d787281e3d"
  "chargeback-reason-codes https://github.com/small-business-software-maker/chargeback-reason-codes 8ded36825231948b3d94057d77cdc0875b7a7510"
)

# local_path repo repo_path (raw files only; derived files are rebuilt below)
FILES=(
  "cfpb_2012_2015/consumer_complaints.csv.zip cfpb-2012-2015 data/consumer_complaints.csv.zip"
  "cfpb_checking_2025_2026/sample_metadata.csv cfpb-checking-2025-2026 data/outputs/sample_metadata.csv"
  "regs/sec-1005.11.txt ecfr-title-12 parts/part-1005/sec-1005.11.txt"
  "regs/sec-1005.6.txt ecfr-title-12 parts/part-1005/sec-1005.6.txt"
  "regs/sec-1026.12.txt ecfr-title-12 parts/part-1026/sec-1026.12.txt"
  "regs/sec-1026.13.txt ecfr-title-12 parts/part-1026/sec-1026.13.txt"
  "chargeback_codes/chargeback-reason-codes.csv chargeback-reason-codes chargeback-reason-codes.csv"
)

fetch_repo() {
  local name="$1" url="$2" commit="$3" dir="$WORK/$1"
  echo "fetch $name @ ${commit:0:12}"
  git init -q "$dir"
  git -C "$dir" remote add origin "$url"
  if git -C "$dir" fetch -q --depth 1 origin "$commit" 2>/dev/null; then
    git -C "$dir" checkout -q FETCH_HEAD
  else
    # Some hosts refuse fetching a bare sha; fall back to the default branch tip.
    rm -rf "$dir"
    git clone -q --depth 1 "$url" "$dir"
  fi
  local head
  head="$(git -C "$dir" rev-parse HEAD)"
  if [ "$head" != "$commit" ]; then
    echo "error: $name is at $head, pinned $commit" >&2
    exit 1
  fi
}

for entry in "${REPOS[@]}"; do
  # shellcheck disable=SC2086
  fetch_repo $entry
done

mkdir -p "$DEST"
for entry in "${FILES[@]}"; do
  read -r local repo path <<<"$entry"
  mkdir -p "$DEST/$(dirname "$local")"
  cp "$WORK/$repo/$path" "$DEST/$local"
done

$PY -m scruple.realdata.derive "$DEST"
$PY -c 'from scruple.realdata.provenance import sha256sums_text as s; print(s(), end="")' \
  >"$WORK/SHA256SUMS"
(cd "$DEST" && sha256sum -c "$WORK/SHA256SUMS")
echo "real data ready in $DEST"
