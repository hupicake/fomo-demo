#!/bin/sh
set -eu

remote=${1:-cnb}
branch=${2:-main}
source_ref=${3:-HEAD}
expected_repository=${FOMO_CNB_REPOSITORY:-hupicake/fomo-demo}
allow_local_remote=${FOMO_CNB_ALLOW_LOCAL_REMOTE:-false}

git check-ref-format "refs/heads/$branch" >/dev/null || {
  echo "Invalid CNB branch: $branch" >&2
  exit 1
}

tracking_ref="refs/remotes/$remote/$branch"
git check-ref-format "$tracking_ref" >/dev/null || {
  echo "Invalid CNB remote or branch: $remote/$branch" >&2
  exit 1
}

source_commit=$(git rev-parse "$source_ref^{commit}")
fetch_urls=$(git remote get-url --all "$remote")
push_urls=$(git remote get-url --push --all "$remote")

[ "$(printf '%s\n' "$fetch_urls" | sed '/^$/d' | wc -l | tr -d ' ')" = 1 ] || {
  echo "CNB remote must have exactly one fetch URL: $remote" >&2
  exit 1
}
[ "$(printf '%s\n' "$push_urls" | sed '/^$/d' | wc -l | tr -d ' ')" = 1 ] || {
  echo "CNB remote must have exactly one push URL: $remote" >&2
  exit 1
}

fetch_url=$fetch_urls
push_url=$push_urls

case "$fetch_url" in
  "https://cnb.cool/$expected_repository"|"https://cnb.cool/$expected_repository.git")
    is_cnb_remote=true
    command -v cnb >/dev/null 2>&1 || {
      echo 'CNB CLI is required for repository authentication' >&2
      exit 1
    }
    ;;
  *)
    [ "$allow_local_remote" = true ] || {
      echo "Refusing CNB fetch from unexpected remote: $fetch_url" >&2
      echo "Expected https://cnb.cool/$expected_repository" >&2
      exit 1
    }
    case "$fetch_url" in
      /*|file:///*) ;;
      *)
        echo "Local CNB sync override only accepts an absolute path or file:// URL: $fetch_url" >&2
        exit 1
        ;;
    esac
    is_cnb_remote=false
    ;;
esac

if [ "$is_cnb_remote" = true ]; then
  case "$push_url" in
    "https://cnb.cool/$expected_repository"|"https://cnb.cool/$expected_repository.git") ;;
    *)
      echo "Refusing CNB push to unexpected URL: $push_url" >&2
      echo "Expected https://cnb.cool/$expected_repository" >&2
      exit 1
      ;;
  esac
elif [ "$fetch_url" != "$push_url" ]; then
  echo 'Local CNB sync test requires identical fetch and push URLs' >&2
  exit 1
fi

remote_git() {
  if [ "$is_cnb_remote" = true ]; then
    git -c credential.helper= -c credential.helper='!cnb git-credential' "$@"
  else
    git "$@"
  fi
}

remote_main=$(remote_git ls-remote --heads "$fetch_url" refs/heads/main | awk 'NR == 1 { print $1 }')
[ -n "$remote_main" ] || {
  echo 'CNB main is empty; run the full-history migration before incremental sync' >&2
  exit 1
}

remote_tip=$(remote_git ls-remote --heads "$fetch_url" "refs/heads/$branch" | awk 'NR == 1 { print $1 }')
if [ -n "$remote_tip" ]; then
  remote_git fetch --no-tags "$fetch_url" "+refs/heads/$branch:$tracking_ref"
  fetched_tip=$(git rev-parse "$tracking_ref^{commit}")
  [ "$fetched_tip" = "$remote_tip" ] || {
    echo 'Fetched CNB branch does not match the advertised remote tip' >&2
    exit 1
  }

  if [ "$remote_tip" = "$source_commit" ]; then
    echo "Source commit already synced: $source_commit"
    printf 'CNB_SOURCE_COMMIT=%s\nCNB_REMOTE_COMMIT=%s\n' \
      "$source_commit" "$remote_tip"
    exit 0
  fi

  git merge-base --is-ancestor "$remote_tip" "$source_commit" || {
    echo "CNB branch has diverged; refusing non-fast-forward update: $branch" >&2
    exit 1
  }
  ahead_count=$(git rev-list --count "$remote_tip..$source_commit")
  echo "Fast-forwarding CNB $branch by $ahead_count commit(s)"
else
  echo "Creating CNB branch: $branch"
fi

if [ "$is_cnb_remote" = true ]; then
  git -c credential.helper= -c credential.helper='!cnb git-credential' \
    push "$push_url" "$source_commit:refs/heads/$branch"
else
  git push "$push_url" "$source_commit:refs/heads/$branch"
fi

updated_tip=$(remote_git ls-remote --heads "$fetch_url" "refs/heads/$branch" | awk 'NR == 1 { print $1 }')
[ "$updated_tip" = "$source_commit" ] || {
  echo 'CNB branch verification failed after push' >&2
  exit 1
}

printf 'CNB_SOURCE_COMMIT=%s\nCNB_REMOTE_COMMIT=%s\n' \
  "$source_commit" "$updated_tip"
