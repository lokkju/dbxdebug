#!/usr/bin/env bash
# Two checks on a pull request's commits, which is what yeet reads once they
# land on main (a merge commit lands every one of them):
#
# 1. Every commit subject is a Conventional Commit. Merge commits are skipped,
#    since yeet ignores them too.
# 2. A PR that changes the package's releasable files has at least one commit
#    that releases (feat, fix or perf from bump_types in .yeet.yaml, or a
#    breaking change: type!: or a BREAKING CHANGE footer), unless it carries
#    the no-release label.
#
# Check 2 is per PR, not per commit: it doesn't confirm that the releasing
# commit is the one touching the package. It reads the targets and types from
# .yeet.yaml, so the two agree. yeet's own release PRs (yeet/release-*) are
# skipped.
#
# Needs: GITHUB_EVENT_PATH (a pull_request event), GITHUB_REPOSITORY, GH_TOKEN
# with pull-requests: read, and yq (mikefarah) and jq on PATH.
set -euo pipefail

config=${YEET_CONFIG:-.yeet.yaml}
event=$GITHUB_EVENT_PATH

head_ref=$(jq -r '.pull_request.head.ref' "$event")
if [[ $head_ref == yeet/release-* ]]; then
  echo "yeet release PR ($head_ref); nothing to check."
  exit 0
fi

number=$(jq -r '.pull_request.number' "$event")
types=$(yq -o json '.bump_types' "$config" | jq -r '[.minor[]?, .patch[]?] | .[]')

# Each commit as one JSON string (its full message), so subjects and footers
# both survive.
messages=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$number/commits" \
  --jq '.[] | .commit.message | @json')

# Conventional Commits header: type(scope)!: description
header='^([A-Za-z]+)(\([^\)]*\))?(!)?: .+'
bad=()
releasing=()
while IFS= read -r m; do
  [[ -z $m ]] && continue
  message=$(jq -r . <<<"$m")
  subject=${message%%$'\n'*}
  if [[ $subject == "Merge "* ]]; then continue; fi
  if [[ ! $subject =~ $header ]]; then
    bad+=("$subject")
    continue
  fi
  type=$(tr '[:upper:]' '[:lower:]' <<<"${BASH_REMATCH[1]}")
  if [[ -n ${BASH_REMATCH[3]} ]] || grep -qxF "$type" <<<"$types" ||
    grep -qE '^BREAKING[ -]CHANGE: ' <<<"$message"; then
    releasing+=("$subject")
  fi
done <<<"$messages"

if ((${#bad[@]} > 0)); then
  {
    echo "These commits aren't Conventional Commits, so yeet can't read them:"
    printf '  %s\n' "${bad[@]}"
    echo
    echo "Reword them (git rebase -i, then reword) as type(scope): description."
  } >&2
  echo "::error title=Commit check::Some commit subjects aren't Conventional Commits."
  exit 1
fi

if jq -e '.pull_request.labels | any(.name == "no-release")' "$event" >/dev/null; then
  echo "Commits are conventional; labeled no-release, so no release check."
  exit 0
fi
if ((${#releasing[@]} > 0)); then
  echo "Commits are conventional, and these release:"
  printf '  %s\n' "${releasing[@]}"
  exit 0
fi

# Every path the PR touches, including the old side of a rename.
files=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$number/files" \
  --jq '.[] | .filename, (.previous_filename // empty)')

# under PATH PREFIX: PATH is PREFIX or inside it. A PREFIX of . (the repo
# root, this repo's one target) holds every path.
under() { [[ $2 == . || $1 == "$2" || $1 == "$2"/* ]]; }

hits=()
while IFS=$'\t' read -r target path excludes; do
  while IFS= read -r f; do
    if [[ -z $f ]] || ! under "$f" "$path"; then continue; fi
    excluded=false
    for x in $excludes; do
      if under "$f" "$x"; then excluded=true; break; fi
    done
    $excluded || hits+=("$target: $f")
  done <<<"$files"
done < <(yq -o json '.targets' "$config" |
  jq -r 'to_entries[] | select(.value.path) |
    [.key, .value.path, ((.value.exclude_paths // []) | join(" "))] | @tsv')

if ((${#hits[@]} == 0)); then
  echo "No releasable files changed."
  exit 0
fi

{
  echo "This PR changes files that release the package:"
  printf '  %s\n' "${hits[@]}"
  echo
  echo "but none of its commits releases (${types//$'\n'/, }, or a breaking change)."
  echo "If users should get this change, reword the commit that makes it (fix(<scope>): ... or"
  echo "feat(<scope>): ...). If they shouldn't, add the no-release label."
} >&2
echo "::error title=Release check::Releasable files changed, but no commit releases them; reword a commit as feat/fix/perf or add the no-release label."
exit 1
