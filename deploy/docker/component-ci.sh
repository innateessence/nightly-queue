#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
candidate=${CANDIDATE_SHA:?Set the exact component PR head SHA.}
[[ $candidate =~ ^[a-f0-9]{40}$ ]] || { echo 'Select an exact component SHA.' >&2; exit 2; }
component_pr=${COMPONENT_PR_NUMBER:?Set the externally supplied pull request number.}
[[ $component_pr =~ ^[1-9][0-9]{0,8}$ ]] || { echo 'Select a canonical positive PR number of at most nine digits.' >&2; exit 2; }
manifest_source=.github/component-prerequisites/pr-$component_pr.json
project=nq-component-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${RANDOM}
[[ $project =~ ^nq-component-ci-[a-z0-9-]+$ && ${#project} -le 63 ]] || exit 2
scratch=$(mktemp -d /tmp/vibearr-component.XXXXXXXX)
export BUILDX_CONFIG=${BUILDX_CONFIG:-$scratch/buildx}
output=${VIBEARR_COMPONENT_OUTPUT:-/tmp/$project-evidence}
[[ ! -e $output ]] || { echo 'Choose a fresh component evidence directory.' >&2; exit 2; }
mkdir -p "$output"
image=$project:input
volume=${project}-source
container=''
volume_created=false
image_created=false
cleanup() {
  local result=$?
  trap - EXIT
  if [[ -n $container ]]; then docker rm -f "$container" >> "$output/cleanup.log" 2>&1 || result=1; fi
  if $volume_created; then docker volume rm "$volume" >> "$output/cleanup.log" 2>&1 || result=1; fi
  if $image_created; then docker image rm --no-prune "$image" >> "$output/cleanup.log" 2>&1 || result=1; fi
  rm -rf "$scratch"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[[ -z $(docker volume ls -q --filter "name=^${volume}$") ]] || { echo 'Component source volume already exists.' >&2; exit 2; }
git_directory=$PWD/.git
if [[ -f $git_directory ]]; then
  IFS= read -r reference < "$git_directory"
  [[ $reference == 'gitdir: '* ]] || exit 2
  git_directory=${reference#gitdir: }
  [[ $git_directory == /* ]] || git_directory=$PWD/$git_directory
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
cp deploy/docker/{component_ci.py,component-ci.sh,Component.Dockerfile} "$scratch/input/"
cp "$manifest_source" "$scratch/input/component-prerequisites.json"
docker build --file deploy/docker/Component.Dockerfile --tag "$image" "$scratch/input" > "$output/input-build.log" 2>&1
image_created=true
docker volume create --label "io.vibearr.component-ci=$project" "$volume" > /dev/null
volume_created=true
container=$(docker create --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=1024m --mount "type=volume,src=$volume,dst=/output" --label "io.vibearr.component-ci=$project" "$image" assemble --candidate "$candidate" --manifest-source "$manifest_source")
assembly_result=0
docker start --attach "$container" > "$output/assembly.log" 2>&1 || assembly_result=$?
container_result=$(docker inspect "$container" --format '{{.State.ExitCode}}')
[[ $assembly_result == 0 && $container_result == 0 ]] || { echo 'Component source assembly failed; inspect assembly.log.' >&2; exit 1; }
docker cp "$container:/output/assembly.json" "$output/assembly.json"
docker cp "$container:/output/repo" "$scratch/derived"
derived=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$image" -I -c 'import json,re,sys; value=json.load(sys.stdin); sha=value["derived_commit"]; assert re.fullmatch(r"[a-f0-9]{40}",sha); assert value["candidate_paths_preserved"] is True and value["entire_tree_verified"] is True; print(sha)' < "$output/assembly.json")
printf 'Component head: %s\nDerived prerequisite integration: %s\n' "$candidate" "$derived"
export VIBEARR_CHECK_OUTPUT=$output/full
cd "$scratch/derived"
bash deploy/docker/checks.sh full --candidate "$PWD" --commit "$derived"
