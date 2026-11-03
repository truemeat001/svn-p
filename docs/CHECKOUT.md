# Safe checkout boundaries

The built-in checkout creates a new local SVN working copy. It does not create a repository, save a password, or change the repository server.

## Supported inputs

- Repository URLs using `https://`, `http://`, `svn://`, or `file://`.
- An existing empty destination directory, or a new directory whose parent already exists.
- Unicode and spaces in local directory names. A literal `@` in the destination is preserved exactly.

SVN revision syntax in the repository URL is preserved: a URL ending in `@17` checks out that specified revision, while an unspecified revision defaults to HEAD. The helper does not rewrite the repository URL or force HEAD.

The URL and destination are shown for confirmation before checkout. URL passwords, query strings, fragments, control characters, nonempty destinations, symbolic links, Windows junctions, and destinations inside an existing working copy are rejected. To include a literal `?` or `#` in a repository path, use its URL-encoded form.

`svn+ssh://` checkout is deliberately unsupported in this dialog. An external SSH helper can prompt independently of SVN and can survive the SVN process timeout. Use a trusted SVN/SSH client to perform that checkout, then add the resulting existing working copy to SVN Desk. This restriction applies to built-in checkout, not to the advanced command interface.

## Authentication and execution

Install a trusted native SVN executable and configure any required authentication or certificates with a trusted SVN client first. The helper reuses existing SVN authentication, passes `--non-interactive` and `--no-auth-cache`, and never accepts a password argument or URL password. It does not bypass certificate errors.

Checkout excludes SVN externals and never uses `--force`. After the checkout command returns successfully, the helper checks that SVN identifies the selected destination as the working-copy root. A successful command without successful verification is reported as a verification failure.

## Failures are not rollback

A failure, interruption, or timeout can leave a partial or complete working copy. SVN Desk never deletes the destination to clean up a failed checkout and never automatically retries it. Inspect the destination before retrying; if a valid working copy already exists, add that working copy instead. Do not edit the destination concurrently while checkout is running.

## Backend API

- `validate_checkout_url(url) -> str`: read-only syntax and credential checks; no network access.
- `validate_checkout_destination(destination) -> Path`: read-only path checks; creates nothing.
- `checkout_working_copy(url, destination, *, executable=None, timeout=300) -> CommandResult`: mutating operation. The caller must confirm the URL and destination first. The helper validates again immediately before running SVN.

Unit tests use mocked processes and temporary directories. The real integration test uses a temporary local `file://` repository and skips when `svn` or `svnadmin` is unavailable; it does not contact a public remote repository.
