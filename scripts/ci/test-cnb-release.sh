#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

source_repo="$tmp/source"
remote_repo="$tmp/cnb.git"
fake_bin="$tmp/bin"
history_file="$tmp/history.json"
request_file="$tmp/request.json"

mkdir -p "$source_repo/scripts/ci" "$source_repo/services/control-plane/src/fomo" "$fake_bin"
for script in cnb-build.sh cnb-release.sh cnb-sync.sh detect-images.sh; do
  cp "$root/scripts/ci/$script" "$source_repo/scripts/ci/"
done

git init -q --initial-branch=main "$source_repo"
git init -q --bare "$remote_repo"
git -C "$source_repo" config user.name 'CNB Release Test'
git -C "$source_repo" config user.email 'cnb-release@example.invalid'
git -C "$source_repo" remote add cnb "$remote_repo"

printf 'APP_ENV = "development"\n' > "$source_repo/services/control-plane/src/fomo/config.py"
git -C "$source_repo" add .
git -C "$source_repo" commit -q -m baseline
baseline=$(git -C "$source_repo" rev-parse HEAD)
git -C "$source_repo" push -q "$remote_repo" HEAD:refs/heads/main
git --git-dir="$remote_repo" symbolic-ref HEAD refs/heads/main

printf '# changed\n' >> "$source_repo/services/control-plane/src/fomo/config.py"
git -C "$source_repo" add services/control-plane/src/fomo/config.py
git -C "$source_repo" commit -q -m control-plane-change
current=$(git -C "$source_repo" rev-parse HEAD)

jq -nc --arg sha "$baseline" \
  '{status:200,data:{data:[{sha:$sha,title:("fomo auto images " + ($sha[0:8]))}]}}' > "$history_file"

cat > "$fake_bin/cnb" <<'EOF'
#!/bin/sh
set -eu
case "$1 $2" in
  'build get-build-logs')
    cat "$FAKE_CNB_HISTORY"
    ;;
  'build start-build')
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --data ]; then
        printf '%s' "$2" > "$FAKE_CNB_REQUEST"
        break
      fi
      shift
    done
    printf '{"status":200,"data":{"success":true,"sn":"cnb-test","buildLogUrl":"https://cnb.cool/test"}}\n'
    ;;
  *)
    echo "unexpected cnb command: $*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$fake_bin/cnb"

output=$(
  cd "$source_repo"
  PATH="$fake_bin:$PATH" \
    FAKE_CNB_HISTORY="$history_file" \
    FAKE_CNB_REQUEST="$request_file" \
    FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh scripts/ci/cnb-release.sh cnb main HEAD
)

printf '%s' "$output" | grep -q "CNB_BUILD_BASELINE=$baseline"
printf '%s' "$output" | grep -q 'CNB_BUILD_IMAGES=control-plane'
printf '%s' "$output" | grep -q 'CNB_BUILD_PROFILE=small-8c'
jq -e --arg baseline "$baseline" --arg current "$current" '
  .sha == $current and
  .event == "api_trigger_fomo_build_small" and
  .env.FOMO_BASE_SHA == $baseline and
  (.env | has("FOMO_WEB_API_URL") | not)
' "$request_file" >/dev/null

# A cold build includes Web and must fail closed until its public API origin is
# explicitly provided; no trigger request may be emitted.
printf '{"status":200,"data":{"data":[]}}\n' > "$history_file"
rm -f "$request_file"
if (
  cd "$source_repo"
  unset FOMO_WEB_API_URL
  PATH="$fake_bin:$PATH" \
    FAKE_CNB_HISTORY="$history_file" \
    FAKE_CNB_REQUEST="$request_file" \
    FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh scripts/ci/cnb-release.sh cnb main HEAD >/dev/null 2>&1
); then
  echo 'CNB release accepted a Web build without its public API origin' >&2
  exit 1
fi
[ ! -e "$request_file" ]

full_output=$(
  cd "$source_repo"
  PATH="$fake_bin:$PATH" \
    FAKE_CNB_HISTORY="$history_file" \
    FAKE_CNB_REQUEST="$request_file" \
    FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    FOMO_WEB_API_URL=https://app.example.test \
    sh scripts/ci/cnb-release.sh cnb main HEAD
)
printf '%s' "$full_output" | grep -q 'CNB_BUILD_BASELINE=none'
printf '%s' "$full_output" | grep -q 'CNB_BUILD_IMAGES=control-plane,sandbox,web'
printf '%s' "$full_output" | grep -q 'CNB_BUILD_PROFILE=full-16c'
jq -e '
  .event == "api_trigger_fomo_build" and
  .env.FOMO_IMAGES == "all" and
  .env.FOMO_WEB_API_URL == "https://app.example.test"
' "$request_file" >/dev/null

# A successfully verified revision is idempotent and consumes no new build.
jq -nc --arg sha "$current" \
  '{status:200,data:{data:[{sha:$sha,title:("fomo auto images " + ($sha[0:8]))}]}}' > "$history_file"
rm -f "$request_file"
already_output=$(
  cd "$source_repo"
  PATH="$fake_bin:$PATH" \
    FAKE_CNB_HISTORY="$history_file" \
    FAKE_CNB_REQUEST="$request_file" \
    FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh scripts/ci/cnb-release.sh cnb main HEAD
)
printf '%s' "$already_output" | grep -q "CNB revision already built and verified successfully: $current"
[ ! -e "$request_file" ]

# A single Sandbox target still uses the 16-core profile.
mkdir -p "$source_repo/infra/opensandbox"
printf 'export {}\n' > "$source_repo/infra/opensandbox/fomo-codex-rpc-bridge.mjs"
git -C "$source_repo" add infra/opensandbox/fomo-codex-rpc-bridge.mjs
git -C "$source_repo" commit -q -m sandbox-change
sandbox_current=$(git -C "$source_repo" rev-parse HEAD)
jq -nc --arg sha "$current" \
  '{status:200,data:{data:[{sha:$sha,title:("fomo auto images " + ($sha[0:8]))}]}}' > "$history_file"

sandbox_output=$(
  cd "$source_repo"
  PATH="$fake_bin:$PATH" \
    FAKE_CNB_HISTORY="$history_file" \
    FAKE_CNB_REQUEST="$request_file" \
    FOMO_CNB_ALLOW_LOCAL_REMOTE=true \
    sh scripts/ci/cnb-release.sh cnb main HEAD
)
printf '%s' "$sandbox_output" | grep -q 'CNB_BUILD_IMAGES=sandbox'
printf '%s' "$sandbox_output" | grep -q 'CNB_BUILD_PROFILE=full-16c'
jq -e --arg sha "$sandbox_current" '.sha == $sha and .event == "api_trigger_fomo_build"' \
  "$request_file" >/dev/null

echo 'CNB release orchestration tests passed'
