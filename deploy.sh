#!/usr/bin/env bash
# Run from the extracted project folder on a computer with GitHub CLI authenticated.
set -euo pipefail
OWNER="lookyouforever"
NAME="github-ai-radar"
REPO="$OWNER/$NAME"
cd "$(dirname "$0")"
for executable in gh git python3; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    echo "Missing $executable. Install it, then run this script again." >&2
    exit 1
  fi
done
if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated. Run 'gh auth login' first." >&2
  exit 1
fi
LOGIN="$(gh api user --jq .login)"
if [[ "$LOGIN" != "$OWNER" ]]; then
  echo "Authenticated account is $LOGIN, expected $OWNER. Switch GitHub CLI account first." >&2
  exit 1
fi
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "The repository $REPO already exists. Stop to avoid overwriting existing content." >&2
  exit 1
fi
if [[ ! -d .git ]]; then
  git init -b main
fi
git add .
if ! git diff --cached --quiet; then
  git -c user.name="GitHub AI Radar Setup" -c user.email="noreply@users.noreply.github.com" commit -m "feat: initialize AI radar" >/dev/null
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
  git branch -M main
fi
echo "Creating $REPO..."
gh repo create "$REPO" --public --description "AI/Agent/Coding Agent weekly GitHub trending radar" --source . --remote origin --push

echo "Configuring Actions write permissions..."
gh api --method PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false >/dev/null

echo "Configuring GitHub Pages..."
if ! gh api "repos/$REPO/pages" >/dev/null 2>&1; then
  gh api --method POST "repos/$REPO/pages" -f build_type=workflow >/dev/null
else
  gh api --method PUT "repos/$REPO/pages" -f build_type=workflow >/dev/null
fi

trigger() {
  local workflow="$1"
  local attempts
  for attempts in 1 2 3 4 5 6; do
    if gh workflow run "$workflow" --repo "$REPO" --ref main; then
      return 0
    fi
    sleep 5
  done
  echo "Could not trigger $workflow. Use Actions → Run workflow in GitHub." >&2
  return 1
}
watch_latest() {
  local workflow="$1" runid=""
  local attempts
  for attempts in 1 2 3 4 5 6 7 8 9 10; do
    runid="$(gh run list --repo "$REPO" --workflow "$workflow" --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
    if [[ -n "$runid" ]]; then
      gh run watch "$runid" --repo "$REPO" --exit-status
      return $?
    fi
    sleep 5
  done
  echo "Could not identify $workflow run; check the Actions page." >&2
  return 1
}
echo "Collecting initial project data..."
trigger collect.yml
watch_latest collect.yml
echo "Building and deploying first leaderboard..."
trigger report.yml
watch_latest report.yml
printf '\nDeployment workflows finished. Website: https://%s.github.io/%s/\n' "$OWNER" "$NAME"
