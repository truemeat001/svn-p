"""Bounded commit outcome summaries; raw SVN/hook diagnostics never leave here.

Zero exit records command completion. A unique recognized success marker is an
SVN-reported revision, not an independent repository-history verification. Unknown
or localized output stays visibly qualified, and never invites a retry.
"""
import re

from changelist_ops import MutationUncertain

MAX_STDOUT_BYTES = 128 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_REVISION = 2**63 - 1
REVISION_LINE = re.compile(r'Committed revision ([1-9][0-9]{0,18})\.')
POST_HOOK_LINE = re.compile(r'Warning: post-commit hook failed(?:\s.*)?', re.IGNORECASE)
PROGRESS_LINE = re.compile(
    r'(?:(?:Sending|Adding|Deleting|Replacing)\s+\S.*'
    r'|Transmitting file data [.]*(?:done)?'
    r'|Committing transaction[.]{3}(?:done)?)')


def classify_commit_output(result):
    """Return fixed codes/validated revision only; exceptions remain uncertain."""
    if type(result.returncode) is not int or result.returncode != 0:
        raise MutationUncertain('Commit did not complete with a successful exit.')
    summary = {'commandCompleted': True, 'outcome': 'completed-unverified',
               'reportedRevision': None, 'warnings': [], 'automaticRetryAllowed': False}
    stdout, stderr = result.stdout, result.stderr
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        summary['warnings'] = ['unrecognized-output']
        return summary
    warnings = summary['warnings']
    if stderr:
        warnings.append('stderr-output')
    # Check length before encoding or splitting; bound all diagnostic processing.
    if (len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES
            or len(stdout.encode('utf-8', errors='replace')) > MAX_STDOUT_BYTES
            or len(stderr.encode('utf-8', errors='replace')) > MAX_STDERR_BYTES):
        warnings.append('output-limit')
        return summary
    if any((ord(c) < 32 and c not in '\t\r\n') or 127 <= ord(c) <= 159
           or 0xd800 <= ord(c) <= 0xdfff or c in '\u2028\u2029' for c in stdout):
        warnings.append('unrecognized-output')
        return summary
    # Native Windows captures may contain CRLF or doubled CR. Do not split on
    # Unicode separators or trim arbitrary prefixes into a trusted marker.
    lines = [line for line in stdout.replace('\r\n', '\n').replace('\r', '\n').split('\n')
             if line.strip(' \t')]
    markers = [(index, match.group(1)) for index, line in enumerate(lines)
               if (match := REVISION_LINE.fullmatch(line))
               and int(match.group(1)) <= MAX_REVISION]
    hook_warning = any(POST_HOOK_LINE.fullmatch(line) for line in lines)
    if hook_warning:
        warnings.append('post-commit-hook')
    if len(markers) == 1:
        index, revision = markers[0]
        # Unknown text before the marker could be hook-controlled or localized;
        # preserve completion but do not promote it to a reported revision.
        if all(PROGRESS_LINE.fullmatch(line) for line in lines[:index]):
            summary.update(outcome='committed', reportedRevision=revision)
            if lines[index + 1:]:
                warnings.append('additional-output')
            return summary
    warnings.append('unrecognized-output')
    return summary
