#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ $# == 4 && $1 == --candidate && $3 == --commit ]] || { echo 'Select --candidate and --commit.' >&2; exit 2; }
candidate=$(realpath "$2")
commit=$4
[[ $commit =~ ^[a-f0-9]{40}$ ]] || { echo 'Select an exact candidate SHA.' >&2; exit 2; }
project=vibearr-codex-image-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${RANDOM}
[[ $project =~ ^vibearr-codex-image-[a-z0-9-]+$ && ${#project} -le 63 ]] || exit 2
scratch=$(mktemp -d /tmp/vibearr-codex-image.XXXXXXXX)
output=${VIBEARR_CHECK_OUTPUT:-/tmp/$project-evidence}
[[ ! -e $output ]] || { rm -rf "$scratch"; echo 'Choose a fresh evidence directory.' >&2; exit 2; }
mkdir -p "$output"
export BUILDX_CONFIG=${BUILDX_CONFIG:-$scratch/buildx}
tools=$project-tools:checks
source_image=$project-source:checks
input_image=$project-input:checks
runner=$project-runner:checks
cleanup() {
  local result=$? cleanup_status=0
  trap - EXIT
  for image in "$tools" "$source_image" "$input_image" "$runner"; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm --no-prune "$image" >> "$output/cleanup.log" 2>&1 || cleanup_status=1
    fi
  done
  rm -rf "$scratch" || cleanup_status=1
  if ((result == 0)); then result=$cleanup_status; fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
unset VIBEARR_API_TOKEN VIBEARR_POSTGRES_PASSWORD
unset VIBEARR_CREDENTIAL_KEY DJANGO_SECRET_KEY VIBEARR_RUNNER_API_TOKEN
docker build --target tools --tag "$tools" . > "$output/bootstrap-tools-build.log" 2>&1
git_directory=$candidate/.git
if [[ -f $git_directory ]]; then
  IFS= read -r reference < "$git_directory"
  [[ $reference == 'gitdir: '* ]] || exit 2
  git_directory=${reference#gitdir: }
  [[ $git_directory == /* ]] || git_directory=$candidate/$git_directory
fi
common=$git_directory
if [[ -f $git_directory/commondir ]]; then
  IFS= read -r reference < "$git_directory/commondir"
  common=$(realpath "$git_directory/$reference")
fi
mkdir -p "$scratch/input/git/refs"
cp -R "$common/objects" "$scratch/input/git/objects"
cp -R "$common/refs/." "$scratch/input/git/refs/"
cp "$git_directory/HEAD" "$scratch/input/git/HEAD"
if [[ -f $common/packed-refs ]]; then cp "$common/packed-refs" "$scratch/input/git/packed-refs"; else : > "$scratch/input/git/packed-refs"; fi
docker build --file deploy/docker/GitInput.Dockerfile --tag "$source_image" "$scratch/input" > "$output/source-build.log" 2>&1
docker build --file deploy/docker/Candidate.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "TOOLS_IMAGE=$tools" --tag "$input_image" . > "$output/input-build.log" 2>&1
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 512m --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=128m --entrypoint python "$input_image" -m deploy.docker.export_candidate --commit "$commit" > "$scratch/source.tar" 2> "$output/source.json"
mkdir "$scratch/source"
tar -xf "$scratch/source.tar" -C "$scratch/source"
for target in tools runner; do
  image=$tools
  if [[ $target == runner ]]; then image=$runner; fi
  docker build --file "$scratch/source/Dockerfile" --target "$target" --tag "$image" "$scratch/source" > "$output/$target-build.log" 2>&1
done
docker image inspect "$tools" "$runner" --format '{{json .Id}}' > "$output/image-ids.jsonl"
expected=$(docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$tools" -c 'from pathlib import Path; import re; text=Path("deploy/docker/install_agents.py").read_text(); version=re.search(r"rust-v([0-9.]+)/",text).group(1); print("codex-cli " + version)')
actual=$(docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 256m --pids-limit 32 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=16m --entrypoint codex "$runner" --version 2> "$output/codex-version-stderr.log")
printf '%s\n' "$actual" > "$output/codex-version.log"
[[ $actual == "$expected" ]] || { echo 'Built Codex version differs from the pinned release.' >&2; exit 1; }
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --pids-limit 64 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=512m --entrypoint sh "$tools" -c 'cp -R /app /tmp/codex-source && cd /tmp/codex-source && python -m unittest vibearr_runner.test_native_codex vibearr_runner.test_native_contract vibearr_runner.test_native_conformance vibearr_executor.test_codex_task' > "$output/native-contract-tests.log" 2>&1
