#!/usr/bin/env bash
# Build synthetic git repos with PLANTED architectural decisions so the eval
# harness has a ground-truth answer key (see labels.json). Deterministic:
# fixed author dates + names so analyze.py output is reproducible.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/synthetic"
rm -rf "$OUT"
mkdir -p "$OUT"

export GIT_AUTHOR_NAME="Fixture Bot"
export GIT_AUTHOR_EMAIL="bot@example.com"
export GIT_COMMITTER_NAME="Fixture Bot"
export GIT_COMMITTER_EMAIL="bot@example.com"

commit() {  # commit <repo> <YYYY-MM-DD> <subject>
  local repo="$1" date="$2" subject="$3"
  GIT_AUTHOR_DATE="${date}T12:00:00" GIT_COMMITTER_DATE="${date}T12:00:00" \
    git -C "$repo" add -A
  GIT_AUTHOR_DATE="${date}T12:00:00" GIT_COMMITTER_DATE="${date}T12:00:00" \
    git -C "$repo" commit -q -m "$subject"
}

init() {
  local repo="$1"
  mkdir -p "$repo"
  git -C "$repo" init -q
  git -C "$repo" config user.name "Fixture Bot"
  git -C "$repo" config user.email "bot@example.com"
}

# ---------------------------------------------------------------------------
# Repo 1: squash-merge style. Planted decisions:
#   - Kafka migration across 3 NON-adjacent commits (#12,#18,#25), all touching
#       the shared services/bus.ts -> CLUSTER must bundle into ONE candidate
#   - moment -> date-fns swap (#30): evidenced alternative (delete dep + add dep)
#   - noise: typo fix, dep bump, test-only  -> must be SKIPPED
#   - scaffold: README only -> SKIPPED (does not pre-touch decision files)
# ---------------------------------------------------------------------------
R1="$OUT/repo_squash"
init "$R1"
echo "# App" > "$R1/README.md"
commit "$R1" 2024-01-05 "docs: initial readme (#1)"

mkdir -p "$R1/src"
echo '{"name":"app","dependencies":{"moment":"^2.29.0"}}' > "$R1/package.json"
echo "export const VERSION = '1.0.0';" > "$R1/src/index.ts"
commit "$R1" 2024-01-20 "build: add package.json and entrypoint (#5)"

# kafka decision, part 1 (shared bus.ts + module file)
mkdir -p "$R1/services/payments"
cat > "$R1/services/bus.ts" <<'EOF'
export function publishKafka(topic: string, msg: object) { /* producer */ }
EOF
echo 'import { publishKafka } from "../bus";' > "$R1/services/payments/events.ts"
commit "$R1" 2024-02-10 "feat: add kafka producer to payments (#12)"

# unrelated noise between the kafka parts
echo "// tidy" >> "$R1/src/index.ts"
commit "$R1" 2024-02-15 "fix: tidy whitespace (#13)"

echo '{"name":"app","dependencies":{"moment":"^2.29.0","left-pad":"^1.3.0"}}' > "$R1/package.json"
commit "$R1" 2024-02-20 "chore: bump dependencies (#14)"

# kafka decision, part 2 (non-adjacent, touches shared bus.ts)
mkdir -p "$R1/services/notifications"
cat >> "$R1/services/bus.ts" <<'EOF'
export function consumeKafka(topic: string) { /* consumer */ }
EOF
echo 'import { consumeKafka } from "../bus";' > "$R1/services/notifications/events.ts"
commit "$R1" 2024-03-01 "feat: wire kafka consumer in notifications (#18)"

mkdir -p "$R1/tests"
echo "test('noop', () => {});" > "$R1/tests/events.test.ts"
commit "$R1" 2024-03-05 "test: add events tests (#15)"

# kafka decision, part 3 (non-adjacent, touches shared bus.ts)
cat >> "$R1/services/bus.ts" <<'EOF'
// legacy http dispatch removed in favour of kafka
EOF
commit "$R1" 2024-04-12 "refactor: drop http dispatch from kafka bus (#25)"

# moment -> date-fns swap (evidenced alternative)
echo '{"name":"app","dependencies":{"date-fns":"^2.30.0"}}' > "$R1/package.json"
cat > "$R1/src/dates.ts" <<'EOF'
import { format } from "date-fns";
export const fmt = (d: Date) => format(d, "yyyy-MM-dd");
EOF
commit "$R1" 2024-05-01 "feat: replace moment with date-fns to cut bundle size (#30)"

# ---------------------------------------------------------------------------
# Repo 2: linear / rebase style, NO PR refs, NO merges.
#   - redis cache adoption across 2 commits sharing lib/cache/ dir + redis token
#   - a docs-only commit -> SKIP
#   - scaffold README only -> SKIP
# ---------------------------------------------------------------------------
R2="$OUT/repo_linear"
init "$R2"
echo "# svc" > "$R2/README.md"
commit "$R2" 2024-01-01 "readme"

mkdir -p "$R2/lib/cache" "$R2/docs"
cat > "$R2/lib/cache/redis.go" <<'EOF'
package cache
type RedisStore struct{}
EOF
echo "require github.com/redis/go-redis/v9 v9.0.0" > "$R2/go.mod"
commit "$R2" 2024-02-01 "adopt redis for session cache"

cat > "$R2/lib/cache/backend.go" <<'EOF'
package cache
// RedisStore is the cache backend
EOF
commit "$R2" 2024-02-02 "use redis store as cache backend"

echo "# Notes" > "$R2/docs/notes.md"
commit "$R2" 2024-02-10 "docs: add notes"

# ---------------------------------------------------------------------------
# Repo 3: two modules, for scope-aware output path testing.
# ---------------------------------------------------------------------------
R3="$OUT/repo_modules"
init "$R3"
mkdir -p "$R3/src/payments" "$R3/src/auth"
echo "export const pay = () => {};" > "$R3/src/payments/index.ts"
echo '{"name":"pay","dependencies":{"stripe":"^12.0.0"}}' > "$R3/src/payments/package.json"
commit "$R3" 2024-01-01 "feat: adopt stripe for payments (#1)"
echo "export const auth = () => {};" > "$R3/src/auth/index.ts"
echo '{"name":"auth","dependencies":{"jose":"^5.0.0"}}' > "$R3/src/auth/package.json"
commit "$R3" 2024-01-10 "feat: adopt jose for jwt auth (#2)"

# ---------------------------------------------------------------------------
# Repo 4: shallow clone, for preflight halt testing.
# ---------------------------------------------------------------------------
R4SRC="$OUT/_repo_full_src"
init "$R4SRC"
echo "a" > "$R4SRC/a.txt"; commit "$R4SRC" 2024-01-01 "one (#1)"
echo "b" > "$R4SRC/b.txt"; commit "$R4SRC" 2024-01-02 "two (#2)"
echo "c" > "$R4SRC/c.txt"; commit "$R4SRC" 2024-01-03 "three (#3)"
git clone -q --depth 1 "file://$R4SRC" "$OUT/repo_shallow"

# ---------------------------------------------------------------------------
# Repo 5: cross-module decision with NO shared file (only shared concept).
# Documents the KNOWN LIMITATION of deterministic clustering (the v2 embeddings
# case): these two commits SHOULD be one decision but won't merge on files.
# ---------------------------------------------------------------------------
R5="$OUT/repo_crossmod"
init "$R5"
echo "# x" > "$R5/README.md"; commit "$R5" 2024-01-01 "readme"
mkdir -p "$R5/services/payments" "$R5/services/notifications"
echo "export const p = () => {};" > "$R5/services/payments/grpc_client.ts"
commit "$R5" 2024-02-01 "feat: adopt grpc transport in payments (#10)"
echo "export const n = () => {};" > "$R5/services/notifications/grpc_client.ts"
commit "$R5" 2024-03-01 "feat: adopt grpc transport in notifications (#20)"

echo "fixtures built in $OUT"
