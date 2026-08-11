#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

source_repo="$tmp/source"
remote_repo="$tmp/cnb.git"
unexpected_repo="$tmp/unexpected.git"
empty_repo="$tmp/empty.git"
writer_repo="$tmp/writer"

git init -q --initial-branch=main "$source_repo"
git init -q --bare "$remote_repo"
git init -q --bare "$unexpected_repo"
git init -q --bare "$empty_repo"
git -C "$source_repo" config user.name 'CNB Sync Test'
git -C "$source_repo" config user.email 'cnb-sync@example.invalid'
git -C "$source_repo" remote add cnb "$remote_repo"

printf 'first\n' > "$source_repo/example.txt"
git -C "$source_repo" add example.txt
git -C "$source_repo" commit -q -m first
source_one=$(git -C "$source_repo" rev-parse HEAD)
git -C "$source_repo" push -q "$remote_repo" HEAD:refs/heads/main
git --git-dir="$remote_repo" symbolic-ref HEAD refs/heads/main

if (
  cd "$source_repo"
  sh "$root/scripts/ci/cnb-sync.sh" cnb main HEAD >/dev/null 2>&1
); then
  echo 'CNB sync accepted an unexpected remote without a test override' >&2
  exit 1
fi

git -C "$source_repo" remote set-url --push cnb "$unexpected_repo"
if (
  cd "$source_repo"
  FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh "$root/scripts/ci/cnb-sync.sh" cnb main HEAD >/dev/null 2>&1
); then
  echo 'CNB sync accepted mismatched fetch and push URLs' >&2
  exit 1
fi
git -C "$source_repo" config --unset-all remote.cnb.pushurl

git -C "$source_repo" remote add empty "$empty_repo"
if (
  cd "$source_repo"
  FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh "$root/scripts/ci/cnb-sync.sh" empty main HEAD >/dev/null 2>&1
); then
  echo 'CNB incremental sync accepted an empty repository' >&2
  exit 1
fi

(
  cd "$source_repo"
  FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh "$root/scripts/ci/cnb-sync.sh" cnb main HEAD >/dev/null
)
[ "$(git --git-dir="$remote_repo" rev-parse refs/heads/main)" = "$source_one" ]

printf 'second\n' >> "$source_repo/example.txt"
git -C "$source_repo" add example.txt
git -C "$source_repo" commit -q -m second
source_two=$(git -C "$source_repo" rev-parse HEAD)

(
  cd "$source_repo"
  FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh "$root/scripts/ci/cnb-sync.sh" cnb main HEAD >/dev/null
)
[ "$(git --git-dir="$remote_repo" rev-parse refs/heads/main)" = "$source_two" ]
[ "$(git --git-dir="$remote_repo" rev-list --count refs/heads/main)" = 2 ]

git clone -q "$remote_repo" "$writer_repo"
git -C "$writer_repo" config user.name 'CNB Remote Writer'
git -C "$writer_repo" config user.email 'remote-writer@example.invalid'
printf 'remote\n' >> "$writer_repo/example.txt"
git -C "$writer_repo" add example.txt
git -C "$writer_repo" commit -q -m remote-only
git -C "$writer_repo" push -q origin main
remote_diverged=$(git --git-dir="$remote_repo" rev-parse refs/heads/main)

printf 'local\n' >> "$source_repo/example.txt"
git -C "$source_repo" add example.txt
git -C "$source_repo" commit -q -m local-only
if (
  cd "$source_repo"
  FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh "$root/scripts/ci/cnb-sync.sh" cnb main HEAD >/dev/null 2>&1
); then
  echo 'CNB sync accepted a non-fast-forward update' >&2
  exit 1
fi
[ "$(git --git-dir="$remote_repo" rev-parse refs/heads/main)" = "$remote_diverged" ]

echo 'CNB sync tests passed'
