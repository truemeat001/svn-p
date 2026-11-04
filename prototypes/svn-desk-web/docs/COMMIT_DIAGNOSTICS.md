# Preserve completed commits and surface diagnostics

Base: `b22f584bc81903076e3320d2b3a0c0e0925e389c`.
Windows native SVN 1.15.0 testing confirmed the preceding XML-order repair, then
found a distinct outcome-reporting defect on both Python 3.10 and 3.14. A disposable
repository's post-commit hook failed after r3 was created. SVN returned code 0,
empty stderr, and stdout containing the revision followed by a post-commit warning.
The old bridge discarded stdout, hiding the warning. The transaction had succeeded;
retrying would not repair the failed hook.

## Output contract

A bounded interpreter now produces fixed enum codes and a validated revision only.
Raw stdout, stderr, command arguments, commit messages, hook text, credentials and
URLs are never returned as diagnostics or written into the UI operation log.

- `commandCompleted: true` records native exit 0.
- `outcome: committed` and `reportedRevision` require one exact ASCII
  `Committed revision N.` line, with N in 1 through 2^63−1. Known English progress
  may precede it. Unknown text before it or duplicate/conflicting markers makes
  the result `completed-unverified`, with a null revision and visible warning.
- `reportedRevision` is a string and expressly labeled as SVN-reported. It is not
  a separate repository-history verification or a guarantee about hook side effects.
- Any nonempty text after that revision, including localized or unknown diagnostics,
  is warned. Recognized post-commit failure adds `post-commit-hook`; UI says the
  reported commit exists but the post-commit hook failed, and **do not resubmit**.
- Nonempty stderr with exit 0 adds `stderr-output` without destroying completion.
  Empty, localized or otherwise unrecognized successful output is also qualified,
  not presented as plain success or proof that no commit occurred.
- Diagnostic interpretation is capped at 128 KiB stdout / 64 KiB stderr (UTF-8).
  Length is checked before splitting or parsing; oversized/malformed output retains
  completed-unverified state plus a fixed warning. This bounds interpretation,
  not the original backend's subprocess capture, which is unchanged.
- Nonzero exit, subprocess exception, timeout, missing or invalid response remains
  conservative **unknown**, even if a success-looking marker appears. Neither a
  rollback nor a safe retry is inferred.
- `automaticRetryAllowed` is always false; confirmation is consumed before dispatch.
  This field is informational. Existing one-use validation also rejects replay.

Warning codes are a closed set: `post-commit-hook`, `additional-output`,
`stderr-output`, `unrecognized-output`, `output-limit`, `status-refresh-failed`,
`workspace-changed`. Hook-provided text cannot create a new code or HTML message.

## Follow-up status and interrupted dialogs

The HTTP response has `statusRefreshed: true` with a fresh snapshot, or false with
`rows: []`, `snapshotToken: null`, all read/write capability flags disabled, and
one of `status-refresh-failed` / `workspace-changed`. It preserves the completed
commit result in both cases. A catalog merge may not overwrite revoked flags with
configured service availability; independent regression covers both failure paths.

The UI validates the result against the captured plan and displays fixed summaries.
A failed follow-up read must not erase the completed revision or hook warning.
Closing a dialog during dispatch does not cancel SVN. A valid late completion is
logged and shown without reopening the window or accepting its stale state. Explicit
refresh and history inspection are required. Invalid or missing completion remains
unknown, with no automatic retry and no raw server exception in the message.

## Validation

The sanitized Windows report shape is represented in a synthetic regression with
Chinese/@ paths, CRLF output, a reported revision, post-commit hook warning and
private sentinel text. The sentinel never appears in the result. Tests additionally
cover ordinary progress, localized/unknown output, stderr warnings, control-character
injection, Unicode/overflow/duplicate revisions, output limits, exceptions, replay,
post-commit status failure and workspace replacement.

Model/browser-contract fixtures cover warning presentation, reported versus
unverified completion, close during apply, missing refreshed status and malformed
responses. Browser execution remains blocked by this environment's known Chromium
socket restriction. No attempt bypassed it. No user working copy or real repository
was used for this repair; original SVN backend and safety selection rules are unchanged.

Final test counts are in `VALIDATION.md`. The new repair still needs Windows real-SVN
and browser retesting. Update the complete checkpoint and restart the bridge, then
use only self-created disposable repositories to verify:

1. A successful commit with a failed post-commit hook returns HTTP 200, preserves the
   reported revision, shows `post-commit-hook`, and consumes its token exactly once.
2. Repository history confirms one revision, exact paths and bound message. Do not
   rerun the commit to repair a hook; inspect server hook logs separately.
3. Zero-exit localized/unknown output is visible as qualified completion. Nonzero,
   timeout and lost-response cases remain unknown rather than implying rollback.
4. Status-read failure or workspace replacement after completion preserves the
   outcome, clears capabilities and requires explicit refresh.
5. All prior exact-scope, property/content/BASE freshness, XML-order and one-use
   confirmation regressions still pass.
