# Managed Muse conversation identity

OpenCode Go requires an `x-opencode-session` value stable within each conversation:
https://opencode.ai/docs/go/#where-can-i-use-it

Both managed launch paths now persist a generated UUID header in
`<state directory>/muse-conversations/` before launching. These records contain
routing names and ownership, never credentials. Atomic replacement, fsync, and
per-record locks make concurrent creation and restart recovery deterministic.
A dotted CLI override changes only the selected provider's `http_headers`
`x-opencode-session` leaf; model, effort, credential selection, other headers,
and global profiles remain unchanged.

`run_attempt` treats the same item in the same state directory as one logical
conversation across attempt numbers. An explicit `conversation_id` starts an
independent conversation. Its item and effective worktree are immutable. Exact
resume also requires the session binding created from an explicitly observed CLI
session ID. Missing historical bindings refuse with a recovery message: restore
original managed identity evidence or explicitly start a new conversation. The
current profile-wide static header does not prove an older session's original
header, so this change does not silently migrate old sessions.

The daemon retains its existing new-process continuation behavior. Logical item
retries keep their identity. If existing account-selection policy deliberately
re-places an exhausted account, the daemon persists a fresh conversation identity
on that selected profile; old exact-session bindings retain the original identity.
Restart recovery restores stored affinity unless the queue explicitly names a
profile. At reap, only an explicitly emitted CLI session ID creates a binding;
time/worktree-based session guesses do not. No daemon restart is part of this fix.

Validation uses hermetic adapter/daemon tests and small local subprocesses. This
is a protocol correction, not proof of a throttling cure or sustained provider
capacity. The parent reported three simultaneous uniquely identified Go calls
still returned 429; no provider requests were made to validate this patch.
