#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 DESTINATION" >&2
  exit 2
fi

destination=$1
manifest_url=https://github.com/D-Robotics/x5-manifest.git
manifest_commit=63edb976220442815b06c29c8a5529a31e68599e
rdk_gen_commit=89f88fec8df211edc64d794df2a5c52150edf43e

mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
config_file="${destination}/.gitconfig-official-https"

git config --file "$config_file" --unset-all \
  url.https://github.com/D-Robotics/.insteadOf 2>/dev/null || true
git config --file "$config_file" --add url.https://github.com/D-Robotics/.insteadOf \
  ssh://git@github.com/D-Robotics/
git config --file "$config_file" --add url.https://github.com/D-Robotics/.insteadOf \
  git@github.com:D-Robotics/
# The disposable build container runs as root while the bind-mounted checkout
# is owned by the invoking host user.  This config is private to that container
# invocation and intentionally does not modify the host's global Git config.
git config --file "$config_file" --replace-all safe.directory '*'

export GIT_CONFIG_GLOBAL=$config_file
# The TUNA git-repo mirror was observed hanging during its bootstrap fetch on
# the build server.  Use the upstream project directly; manifest projects are
# still rewritten from GitHub SSH URLs to anonymous HTTPS below.
export REPO_URL=https://github.com/GerritCodeReview/git-repo

cd "$destination"
repo init -u "$manifest_url" -b main -m default.xml \
  --no-clone-bundle --no-repo-verify

actual_manifest_commit=$(git -C .repo/manifests rev-parse HEAD)
if [[ "$actual_manifest_commit" != "$manifest_commit" ]]; then
  echo "Manifest main moved: expected ${manifest_commit}, got ${actual_manifest_commit}." >&2
  echo "Audit and update the lock before syncing." >&2
  exit 4
fi

repo sync -c -j4 --fail-fast --no-clone-bundle
repo manifest -r -o manifest.lock.xml
repo status >repo-status.txt

actual_rdk_gen_commit=$(git rev-parse HEAD)
if [[ "$actual_rdk_gen_commit" != "$rdk_gen_commit" ]]; then
  echo "x5-rdk-gen moved: expected ${rdk_gen_commit}, got ${actual_rdk_gen_commit}." >&2
  exit 5
fi

git remote get-url upstream >/dev/null 2>&1 || \
  git remote add upstream https://github.com/D-Robotics/x5-rdk-gen.git
git remote get-url user >/dev/null 2>&1 || \
  git remote add user https://github.com/xuguosheng110/x5-rdk-gen.git
user_main=$(git ls-remote user refs/heads/main | awk '{print $1}')
if [[ "$user_main" != "$rdk_gen_commit" ]]; then
  echo "User fork main moved: expected ${rdk_gen_commit}, got ${user_main}." >&2
  exit 6
fi

printf 'manifest_commit=%s\n' "$actual_manifest_commit"
printf 'rdk_gen_commit=%s\n' "$actual_rdk_gen_commit"
printf 'locked_manifest=%s/manifest.lock.xml\n' "$destination"
