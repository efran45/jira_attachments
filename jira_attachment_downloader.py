#!/usr/bin/env python3
"""
Download every attachment from every issue matched by a JQL query.

Targets Jira Cloud (*.atlassian.net) using an API token.
Configure everything in the CONFIG block below, then just run:

    python3 jira_attachment_downloader.py

Attachments land in:  <OUTPUT_DIR>/<ISSUE-KEY>/<filename>
"""

import csv
import os
import re
import sys
import time
import unicodedata

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    sys.exit(
        "This script needs the 'requests' library. Install it with:\n"
        "    python -m pip install requests"
    )

# A redirected stdout on Windows defaults to the legacy code page, so printing
# a non-ASCII attachment name would raise UnicodeEncodeError mid-run. Degrade
# to replacement characters rather than dying halfway through a download.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------
# CONFIG -- edit these
# --------------------------------------------------------------------------

# Your Jira Cloud site, no trailing slash.
JIRA_BASE_URL = "https://batterypitchcall.atlassian.net"

# The account the API token belongs to.
JIRA_EMAIL = "ian.evans45@gmail.com"

# Create one at: https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_API_TOKEN = "PASTE_YOUR_API_TOKEN_HERE"

# The query whose issues you want attachments from.
JQL = 'project = COACH AND attachments IS NOT EMPTY ORDER BY created DESC'

# Where files get written. Relative paths resolve against the current directory.
OUTPUT_DIR = "jira_attachments"

# --- options ---------------------------------------------------------------

# True = list what would be downloaded, write nothing. Good for vetting a JQL.
DRY_RUN = False

# Skip a file that already exists on disk with the expected byte size.
SKIP_EXISTING = True

# Stop after this many issues (None = no limit). Safety valve for broad JQL.
MAX_ISSUES = None

# Don't download anything larger than this (None = no limit).
MAX_ATTACHMENT_BYTES = None  # e.g. 50 * 1024 * 1024 for 50 MB

# Write a CSV index of everything downloaded, at <OUTPUT_DIR>/manifest.csv.
WRITE_MANIFEST = True

# Longest attachment filename to keep before truncating. Windows caps a whole
# path at 260 characters unless long paths are enabled, and that budget is
# shared with OUTPUT_DIR plus the issue-key folder -- so keep this modest and
# run from a short directory. On macOS/Linux you can safely raise it to 180.
MAX_FILENAME_CHARS = 120

# Issues fetched per search request (Jira caps this; 100 is the practical max).
PAGE_SIZE = 100

# Network retry behaviour for 429 / 5xx responses.
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT = 60

# --------------------------------------------------------------------------
# End of config
# --------------------------------------------------------------------------

SEARCH_FIELDS = ["key", "attachment"]
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def log(message):
    print(message, flush=True)


def die(message):
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def check_config():
    if not JIRA_BASE_URL.startswith(("http://", "https://")):
        die("JIRA_BASE_URL must start with https://")
    if not JIRA_API_TOKEN or JIRA_API_TOKEN == "PASTE_YOUR_API_TOKEN_HERE":
        die(
            "JIRA_API_TOKEN is not set. Create one at "
            "https://id.atlassian.com/manage-profile/security/api-tokens "
            "and paste it into the CONFIG block."
        )
    if not JIRA_EMAIL or "@" not in JIRA_EMAIL:
        die("JIRA_EMAIL must be the email address the API token belongs to.")
    if not JQL.strip():
        die("JQL is empty.")

    if os.name == "nt":
        # Worst case: <output>\<ISSUE-KEY>\<filename>. Windows rejects paths
        # over 260 chars unless long paths are enabled, and failures show up
        # only once a long-named attachment appears -- so warn up front.
        worst = len(os.path.abspath(OUTPUT_DIR)) + 1 + 24 + 1 + MAX_FILENAME_CHARS
        if worst > 255:
            log(
                f"WARNING: paths could reach ~{worst} characters, over the "
                "260-character Windows limit.\n"
                "         Use a shorter OUTPUT_DIR, lower MAX_FILENAME_CHARS, "
                "or enable long path support.\n"
            )


def make_session():
    session = requests.Session()
    session.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "jira-attachment-downloader/1.0",
    })
    return session


def request_with_retry(session, method, url, **kwargs):
    """Issue a request, retrying on rate limits and transient server errors."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            )
        except requests.RequestException as exc:
            last_error = exc
            delay = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            log(f"    network error ({exc}); retrying in {delay:.0f}s")
            time.sleep(delay)
            continue

        if response.status_code in (429, 500, 502, 503, 504):
            last_error = f"HTTP {response.status_code}"
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            response.close()
            log(f"    {last_error}; retrying in {delay:.0f}s")
            time.sleep(delay)
            continue

        return response

    raise RuntimeError(f"giving up after {MAX_RETRIES} attempts: {last_error}")


def explain_failure(response):
    """Turn a Jira error response into something readable."""
    detail = ""
    try:
        body = response.json()
        messages = body.get("errorMessages") or []
        if not messages and isinstance(body.get("errors"), dict):
            messages = [f"{k}: {v}" for k, v in body["errors"].items()]
        detail = "; ".join(str(m) for m in messages)
    except ValueError:
        detail = response.text[:400].strip()

    hint = ""
    if response.status_code == 401:
        hint = " (check JIRA_EMAIL and JIRA_API_TOKEN)"
    elif response.status_code == 403:
        hint = " (the account lacks permission, or the token is scoped too narrowly)"
    elif response.status_code == 400:
        hint = " (usually a malformed JQL query)"
    return f"HTTP {response.status_code}{hint}: {detail or '<no detail>'}"


def search_issues(session):
    """Yield issues matching JQL, using the current search API with a
    fallback to the legacy endpoint on older/edge deployments."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    next_page_token = None
    yielded = 0

    while True:
        payload = {
            "jql": JQL,
            "fields": SEARCH_FIELDS,
            "maxResults": PAGE_SIZE,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        response = request_with_retry(session, "POST", url, json=payload)

        if response.status_code in (404, 410) and next_page_token is None:
            log("  /search/jql unavailable, falling back to the legacy search API")
            yield from search_issues_legacy(session, yielded)
            return
        if not response.ok:
            die(f"search failed -- {explain_failure(response)}")

        body = response.json()
        issues = body.get("issues") or []
        for issue in issues:
            yield issue
            yielded += 1
            if MAX_ISSUES is not None and yielded >= MAX_ISSUES:
                log(f"  reached MAX_ISSUES ({MAX_ISSUES}); stopping search")
                return

        next_page_token = body.get("nextPageToken")
        if body.get("isLast") or not next_page_token or not issues:
            return


def search_issues_legacy(session, already_yielded=0):
    """Pagination against the older /rest/api/3/search endpoint."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    start_at = 0
    yielded = already_yielded

    while True:
        payload = {
            "jql": JQL,
            "fields": SEARCH_FIELDS,
            "maxResults": PAGE_SIZE,
            "startAt": start_at,
        }
        response = request_with_retry(session, "POST", url, json=payload)
        if not response.ok:
            die(f"search failed -- {explain_failure(response)}")

        body = response.json()
        issues = body.get("issues") or []
        if not issues:
            return

        for issue in issues:
            yield issue
            yielded += 1
            if MAX_ISSUES is not None and yielded >= MAX_ISSUES:
                log(f"  reached MAX_ISSUES ({MAX_ISSUES}); stopping search")
                return

        start_at += len(issues)
        total = body.get("total")
        if total is not None and start_at >= total:
            return


def safe_name(name, fallback):
    """Make a filesystem-safe name out of whatever Jira hands back.

    Attachment filenames are user-supplied, so treat them as hostile: strip
    path separators and control characters so nothing escapes OUTPUT_DIR.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace(os.sep, "_")
    if os.altsep:
        name = name.replace(os.altsep, "_")
    # The regex covers < > : " / \ | ? * and control chars -- i.e. every
    # character Windows rejects. Backslash is stripped on POSIX too, so a
    # name is never a separator on the *other* platform either.
    # strip(". ") rather than strip(".") -- removing dots can expose spaces
    # underneath (and vice versa), and Windows rejects both at either end.
    name = _UNSAFE_CHARS.sub("_", name).strip().strip(". ")

    if not name or name in (".", ".."):
        return fallback
    stem, dot, ext = name.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"

    limit = max(MAX_FILENAME_CHARS, 16)
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = stem[:limit - len(ext) - 1] + "." + ext
        else:
            name = name[:limit]
        # Truncation can land on a trailing dot or space; Windows refuses to
        # create such a file, so re-strip and re-check for an empty result.
        name = name.rstrip(". ")
        if not name:
            return fallback
    return name


def disambiguate(filename, attachment_id):
    """Fold the attachment id into a filename.

    Two different attachments on one issue can share a filename, so the id --
    unique per attachment -- is what keeps them apart. This is deliberately a
    pure function of (name, id) rather than a "does it exist yet" check, so a
    given attachment resolves to the same path on every run.
    """
    stem, dot, ext = filename.rpartition(".")
    if dot:
        return f"{stem}_{attachment_id}.{ext}"
    return f"{filename}_{attachment_id}"


def sort_key(attachment):
    """Stable ordering for an issue's attachments, so which one of a pair of
    same-named attachments keeps the plain filename doesn't drift run to run."""
    raw = str(attachment.get("id") or "")
    return (0, int(raw)) if raw.isdigit() else (1, 0)


def human_size(num_bytes):
    if num_bytes is None:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def download_attachment(session, url, destination):
    """Stream one attachment to disk via a temp file, so an interrupted run
    never leaves a truncated file that SKIP_EXISTING would later trust."""
    temp_path = destination + ".part"
    response = request_with_retry(session, "GET", url, stream=True)
    try:
        if not response.ok:
            raise RuntimeError(explain_failure(response))
        written = 0
        with open(temp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
    finally:
        response.close()

    os.replace(temp_path, destination)
    return written


def main():
    check_config()
    output_root = os.path.abspath(OUTPUT_DIR)

    log(f"Jira site : {JIRA_BASE_URL}")
    log(f"User      : {JIRA_EMAIL}")
    log(f"JQL       : {JQL}")
    log(f"Output    : {output_root}")
    if DRY_RUN:
        log("Mode      : DRY RUN (nothing will be written)")
    log("")

    session = make_session()

    if not DRY_RUN:
        os.makedirs(output_root, exist_ok=True)

    issues_seen = 0
    issues_with_attachments = 0
    downloaded = 0
    skipped = 0
    too_large = 0
    failed = 0
    total_bytes = 0
    manifest_rows = []

    log("Searching...")
    for issue in search_issues(session):
        issues_seen += 1
        key = issue.get("key") or issue.get("id") or "UNKNOWN"
        attachments = (issue.get("fields") or {}).get("attachment") or []
        if not attachments:
            continue

        issues_with_attachments += 1
        log(f"\n{key} -- {len(attachments)} attachment(s)")

        issue_dir = os.path.join(output_root, safe_name(key, "UNKNOWN"))
        if not DRY_RUN:
            os.makedirs(issue_dir, exist_ok=True)

        claimed_names = set()
        for attachment in sorted(attachments, key=sort_key):
            attachment_id = str(attachment.get("id") or "0")
            size = attachment.get("size")
            filename = safe_name(
                attachment.get("filename"), f"attachment_{attachment_id}"
            )
            # Compare case-insensitively: on macOS/Windows two names differing
            # only in case are the same file.
            if filename.lower() in claimed_names:
                filename = disambiguate(filename, attachment_id)
            claimed_names.add(filename.lower())
            content_url = attachment.get("content")

            if not content_url:
                log(f"  ! {filename}: no download URL, skipping")
                failed += 1
                continue

            if MAX_ATTACHMENT_BYTES is not None and (size or 0) > MAX_ATTACHMENT_BYTES:
                log(f"  - {filename} ({human_size(size)}) exceeds size limit, skipping")
                too_large += 1
                continue

            if DRY_RUN:
                log(f"  would download {filename} ({human_size(size)})")
                continue

            destination = os.path.join(issue_dir, filename)
            if (
                SKIP_EXISTING
                and os.path.exists(destination)
                and size is not None
                and os.path.getsize(destination) == size
            ):
                log(f"  = {filename} already present, skipping")
                skipped += 1
                continue

            try:
                written = download_attachment(session, content_url, destination)
            except Exception as exc:  # keep going; report at the end
                log(f"  ! {filename}: download failed -- {exc}")
                failed += 1
                continue

            downloaded += 1
            total_bytes += written
            log(f"  + {os.path.basename(destination)} ({human_size(written)})")
            manifest_rows.append({
                "issue_key": key,
                "attachment_id": attachment_id,
                "filename": attachment.get("filename") or "",
                "saved_as": os.path.relpath(destination, output_root),
                "bytes": written,
                "created": attachment.get("created") or "",
                "author": ((attachment.get("author") or {}).get("displayName") or ""),
                "mime_type": attachment.get("mimeType") or "",
            })

    if WRITE_MANIFEST and manifest_rows and not DRY_RUN:
        manifest_path = os.path.join(output_root, "manifest.csv")
        write_header = not os.path.exists(manifest_path)
        with open(manifest_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0].keys()))
            if write_header:
                writer.writeheader()
            writer.writerows(manifest_rows)
        log(f"\nManifest: {manifest_path}")

    log("\n" + "-" * 52)
    log(f"Issues matched          : {issues_seen}")
    log(f"Issues with attachments : {issues_with_attachments}")
    log(f"Downloaded              : {downloaded} ({human_size(total_bytes)})")
    if skipped:
        log(f"Already present         : {skipped}")
    if too_large:
        log(f"Over size limit         : {too_large}")
    if failed:
        log(f"Failed                  : {failed}")
    log("-" * 52)

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\nInterrupted.")
        sys.exit(130)
