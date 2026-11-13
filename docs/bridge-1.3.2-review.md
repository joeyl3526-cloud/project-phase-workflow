# Bridge 1.3.2: paused review registration

Recorded: 2026-11-12 16:20 Asia/Shanghai. Scope: phase-workflow bridge and its accompanying skill; no live task in another project was changed.

## Change

An already reviewable task can register a PASS, FAIL or BLOCKED review while paused using the exact task ID, context hash and, when available, collected artifact fingerprint. The review is stored as `current.deferred_review`; status remains `PAUSED`, with no stage advance or dispatch. Identical repeats are idempotent; conflicts are rejected.

Explicit resume rechecks the report, context, collected receipt and artifacts before applying the review using the existing A/B semantics. Changed evidence moves the review to `stale_deferred_reviews` and leaves the run paused; a transient read error preserves the registered review. Interrupted tasks remain BLOCKED-only. The normal unpaused interface retains its previous parameters and behavior.

The workflow skill 4.2.4 includes the preceding risk-based scenario, review, batching and pause guidance. It does not change the user's existing model choice, A/B mode or authorization.

## Evidence and limits

| Check | Result | Scope |
|---|---|---|
| Bridge regression | PASS: 113 tests | Synthetic state and fake services; workspace-local temp directory and authorized loopback access; repeated successfully from a copy containing only the intended repository files |
| Installed bridge targeted tests | PASS: 17 tests | Isolated copy of installed bridge 1.3.2; includes CLI registration and resume |
| Installed MCP protocol | PASS | New isolated MCP process reports 1.3.2 and advertises `task_id` and `context_hash`; no live model or task |
| Skill validation | PASS | Official skill structure checker and local reference check; installed files match candidate |
| Current chat MCP | NOT UPDATED | Current host still reports 1.3.1 and the old tool schema; a single-connection refresh was not established |
| Shared coordinator and other chats | NOT RESTARTED | Existing coordinator remains 1.3.1; shared processes were not forcibly restarted |
| Real provider, live callback and end-to-end model run | NOT TESTED | Synthetic tests are not live user acceptance |

The installed bridge is `<user-home>/.codex/tools/phase-workflow/phase_bridge.py`; SHA-256 at validation: `776b2f936f9bef21d492bdfde15a5d6c8dcb2629cee5a904e39510373405d37a`. Until the MCP host actually refreshes, use the installed 1.3.2 CLI for registering or resuming a deferred review. Do not use the old MCP `record_review` or `resume` on a deferred review. Do not restart a shared coordinator or other chats merely to claim the new version is active.

The previous 1.3.1 installation receipt and acceptance records are historical. No claim is made that 1.3.2 has passed real provider, unattended operation or OS reboot validation.

## Current host refresh attempt

Recorded: 2026-11-12 16:26 Asia/Shanghai. The installed 1.3.2 MCP server was started with isolated synthetic configuration and advertised the expected version and review identity fields. A separate, fresh Codex app-server accepted `config/mcpServer/reload`; that is evidence about the isolated host only. The current daemon's `codex app-server proxy` failed to connect to its local control socket with OS error 10050, so no reload request reached it. A subsequent current-chat `workflow_status` still reported 1.3.1. The shared coordinator was not restarted and the current host is not claimed to have reloaded. Reconnect or restart the Codex host when it can be done without affecting other work, then recheck both version and tool schema before using paused-review registration.
