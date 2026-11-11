#!/usr/bin/env bash
# Read-only probe: can the Claude / Codex CLI run inside a bubblewrap sandbox
# on this Runtime node? Run as the user the runtime daemon runs as. No root.
# Creates only a temp dir (removed on exit). Never prints credential contents.
#
# Usage: bash sandbox-probe.sh [WORKSPACE_ROOT]
#   WORKSPACE_ROOT: parent dir of all employee workspaces (hidden inside the
#   sandbox to prove isolation). Optional.
set -u
ROOT="${1:-}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/work" "$W/claude" "$W/codex" "$W/tmp"
say() { printf '%s\n' "$*"; }

say "== host"
say "kernel=$(uname -r)"
say "userns_clone=$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo n/a)"
say "max_user_namespaces=$(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo n/a)"
say "cgroup_fs=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo n/a)"
say "nft=$(command -v nft >/dev/null && echo yes || echo no)"
say "bwrap=$(command -v bwrap || echo MISSING)"
say "claude=$(command -v claude || echo MISSING)"
say "codex=$(command -v codex || echo MISSING)"
command -v bwrap >/dev/null || { say "RESULT bwrap missing: install bubblewrap, or use the per-user fallback"; exit 0; }

CLAUDE_CRED="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
CODEX_AUTH="${CODEX_HOME:-$HOME/.codex}/auth.json"
say "claude_cred_file=$([ -f "$CLAUDE_CRED" ] && echo present || echo absent)"
say "codex_auth_file=$([ -f "$CODEX_AUTH" ] && echo present || echo absent)"

# Sandbox: whole filesystem read-only, sensitive parents masked with tmpfs,
# only this probe's dirs writable, credentials bound read-only.
ARGS=(--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp
      --bind "$W/work" "$W/work" --bind "$W/claude" "$W/claude"
      --bind "$W/codex" "$W/codex" --chdir "$W/work"
      --unshare-pid --die-with-parent
      --setenv CLAUDE_CONFIG_DIR "$W/claude" --setenv CODEX_HOME "$W/codex")
for d in "$HOME/.claude/projects" "$HOME/.codex/sessions" "$ROOT"; do
  [ -n "$d" ] && [ -d "$d" ] && ARGS+=(--tmpfs "$d")
done
[ -f "$CLAUDE_CRED" ] && { : > "$W/claude/.credentials.json"; ARGS+=(--ro-bind "$CLAUDE_CRED" "$W/claude/.credentials.json"); }
[ -f "$CODEX_AUTH" ] && { : > "$W/codex/auth.json"; ARGS+=(--ro-bind "$CODEX_AUTH" "$W/codex/auth.json"); }

say "== T1 sandbox starts"
if bwrap "${ARGS[@]}" true 2>"$W/tmp/t1.err"; then say "T1 ok"; else say "T1 FAIL: $(head -c 300 "$W/tmp/t1.err")"; exit 0; fi

say "== T2 isolation"
say "other_transcripts_visible=$(bwrap "${ARGS[@]}" sh -c 'ls "$HOME/.claude/projects" "$HOME/.codex/sessions" 2>/dev/null | wc -l')"
[ -n "$ROOT" ] && say "workspace_root_entries_visible=$(bwrap "${ARGS[@]}" sh -c "ls '$ROOT' 2>/dev/null | wc -l")"
say "write_outside=$(bwrap "${ARGS[@]}" sh -c 'touch "$HOME/.probe-should-fail" 2>/dev/null && echo ALLOWED || echo denied')"

if command -v claude >/dev/null; then
  say "== T3 claude in sandbox (creds read-only)"
  bwrap "${ARGS[@]}" claude -p "Reply with exactly: ok" --output-format json --max-turns 1 \
    </dev/null >"$W/tmp/c1.json" 2>"$W/tmp/c1.err"
  say "exit=$? is_error=$(grep -o '"is_error":[a-z]*' "$W/tmp/c1.json" | head -1)"
  say "detail=$(grep -o '"result":"[^"]\{0,120\}' "$W/tmp/c1.json" | head -1)"
  say "stderr=$(head -c 200 "$W/tmp/c1.err" | tr '\n' ' ')"
  say "transcripts_in_private_dir=$(find "$W/claude" -name '*.jsonl' | wc -l)"
fi

if command -v codex >/dev/null; then
  say "== T4 codex in sandbox (auth read-only)"
  bwrap "${ARGS[@]}" codex exec --skip-git-repo-check --json "Reply with exactly: ok" \
    </dev/null >"$W/tmp/x1.jsonl" 2>"$W/tmp/x1.err"
  say "exit=$? lines=$(wc -l <"$W/tmp/x1.jsonl")"
  say "stderr=$(tail -c 200 "$W/tmp/x1.err" | tr '\n' ' ')"
  say "rollouts_in_private_dir=$(find "$W/codex" -name 'rollout-*.jsonl' | wc -l)"
fi

say "== T5 no network namespace (expect failure to connect)"
bwrap "${ARGS[@]}" --unshare-net sh -c 'command -v curl >/dev/null && curl -s -m 5 -o /dev/null -w "http=%{http_code}\n" https://example.com || echo "no curl"' 2>&1 | tail -1
say "== done"
