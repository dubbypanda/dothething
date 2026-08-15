#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

bump="patch"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --minor) bump="minor"; shift ;;
        --major) bump="major"; shift ;;
        *) echo "Usage: $0 [--minor | --major]" >&2; exit 1 ;;
    esac
done

current=$(cat VERSION | tr -d '[:space:]')
IFS=. read -r major minor patch <<< "$current"

case "$bump" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
esac

new="${major}.${minor}.${patch}"

echo "$new" > VERSION
sed -i '' "s/^DTT_VERSION=\".*\"/DTT_VERSION=\"${new}\"/" dtt.sh

echo "${current} → ${new}"

# Point the release at whatever the notte fork's main is now, so a fix there
# ships with this bump. Costs nothing when the fork hasn't moved: same sha,
# no diff, and nobody reinstalls. Anything unfinished must stay off notte's
# main, because this will pick it up.
notte_msg=""
notte_sha=$(git ls-remote https://github.com/fluffypony/notte.git main 2>/dev/null | cut -f1 || true)
current_pin=$(grep -m1 '^NOTTE_PIN=' dtt.sh | cut -d'"' -f2 || true)
if [ -z "$notte_sha" ]; then
    echo "⚠ Could not reach the notte fork — keeping pin ${current_pin:0:8}" >&2
elif [ "$notte_sha" = "$current_pin" ]; then
    echo "notte: unchanged (${notte_sha:0:8})"
else
    sed -i '' "s/^NOTTE_PIN=\".*\"/NOTTE_PIN=\"${notte_sha}\"/" dtt.sh
    echo "notte: ${current_pin:0:8} → ${notte_sha:0:8}"
    notte_msg=" and Notte pin"
fi

git add VERSION dtt.sh
git commit -m "Bump build version${notte_msg}"
git push
