"""Customer-metadata leak check (mission Section 10) - reusable, cross-platform.

Fails if a customer object-naming pattern appears outside ``tests/fixtures/`` in anything that could
be COMMITTED (tracked files plus untracked-but-not-ignored ones) or ANYWHERE IN GIT HISTORY (a clean
tip commit is meaningless if an earlier commit leaked a name). Git-ignored files are out of scope:
they cannot reach a commit, and reporting them trains people to ignore the check.
Three patterns:

1. generated tables:  ``/BIC/<name>`` or ``/BI0/<name>`` (3+ name chars);
2. customer namespace: whole tokens starting ``Z`` or ``Y`` with 4+ upper/digit/underscore chars;
3. **internal host names**: any dotted host name that is not an RFC-reserved documentation form or a
   known public domain. Mission Rule 5 keeps hosts out of code, tests, logs and error messages, and
   connection details arrive by the least careful route there is - pasted into a conversation and
   then typed into config. Patterns 1 and 2 cannot see a host name, so nothing guarded that until a
   real landscape's FQDNs were handed over. **The rule is allow-list by suffix, not deny-list by
   customer**, because writing the customer's actual domain into this file to detect it would *be*
   the leak: ``.invalid``, ``.example`` and ``.example.com`` are reserved for documentation and are
   free, every other host has to be named here on purpose.

Documentation and source use the bare ``/BIC/`` or ``/BIC/<...>`` placeholder form, which pattern 1
does not match. The allow-list holds generic non-customer tokens that happen to match pattern 2.

This is the single source of truth for the check: CI runs it, and it is the same code used locally
(no bash/Python drift). Runs on Linux and Windows (pure Python + ``git``; uses ``git grep -P``).

Usage (from the repo root):

    python scripts/customer_metadata_scan.py

Exit code 0 = clean, 1 = potential leak (details printed), 2 = usage error.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Pattern 1 (/BIC//BI0/ generated tables) | pattern 2 (Z*/Y* customer namespace tokens).
PATTERN = r"(/(?:BIC|BI0)/[A-Za-z0-9_]{3,})|(\b[ZY][A-Z0-9_]{4,}\b)"
_COMBINED = re.compile(PATTERN)

# Generic non-customer tokens that match pattern 2 but are not object names:
#   ZZ_TEST                    - throwaway/copy-artifact marker the description service detects
#   YYYYMMDD / YYYYMMDDHHMMSS   - SAP date/timestamp FORMAT placeholders (process-log parser)
#   ZY_FIELDS                  - a former internal SQL column alias (since renamed) mentioned once
#                                in an earlier commit's PROGRESS.md prose
#   ZXRSAU01..04               - SAP-DEFINED customer includes of enhancement RSAP0001 (the four BW
#                                extractor exits). Named by SAP's own ZXnnnU01 convention, identical
#                                on every ABAP system, and carrying no customer information. They
#                                sit in the customer namespace only because SAP put them there.
ALLOW = {
    "ZZ_TEST",
    "YYYYMMDD",
    "YYYYMMDDHHMMSS",
    "ZY_FIELDS",
    "ZXRSAU01",
    "ZXRSAU02",
    "ZXRSAU03",
    "ZXRSAU04",
}

# Pattern 4: **IPv4 addresses.** Patterns 1-3 cannot see one: an address is not a BW object name
# and not a dotted *host name* (no alphabetic TLD to match). That gap is not theoretical - two
# internal addresses from a live landscape, quoted verbatim out of an hdbcli transport error into a
# source comment, a test and the CHANGELOG, passed every check here and reached a public
# repository. An address identifies a server and a subnet, which is Rule 5's concern whether or
# not it is globally routable.
#
# Allow-listed by range rather than by value, for the same reason the host rule is: writing the
# customer's actual addresses in here to detect them would *be* the leak. The documentation and
# testing ranges reserved by RFC 5737 and RFC 3927 are free; everything else has to be justified.
# Loopback and the unspecified address are free because they name no host.
IPV4_PATTERN = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
_IPV4 = re.compile(IPV4_PATTERN)
_MAX_OCTET = 255

#: Reserved for documentation (RFC 5737), link-local (RFC 3927), loopback, and placeholders.
_IP_ALLOW_PREFIXES = (
    "192.0.2.",  # RFC 5737 TEST-NET-1
    "198.51.100.",  # RFC 5737 TEST-NET-2
    "203.0.113.",  # RFC 5737 TEST-NET-3
    "169.254.",  # RFC 3927 link-local
    "127.",  # loopback
    "0.0.0.0",  # the unspecified address
    "255.255.255.",  # broadcast / netmasks
)


def _ipv4_tokens(text: str) -> set[str]:
    """IPv4 addresses that are not from a reserved documentation or placeholder range.

    Three-component version strings never match, because four octets are required. A
    four-component version is indistinguishable from an address by shape alone, so it is reported:
    a false positive costs one allow-list entry, a missed address costs a disclosure. (No example
    is written out here - this file is scanned too, and the first draft of this docstring tripped
    the check on its own illustration.)
    """
    hits: set[str] = set()
    for match in _IPV4.finditer(text):
        token = match.group(0)
        if any(token.startswith(prefix) for prefix in _IP_ALLOW_PREFIXES):
            continue
        if any(int(octet) > _MAX_OCTET for octet in token.split(".")):
            continue  # not a valid address: a version string or an identifier
        hits.add(token)
    return hits


# Pattern 3: dotted host names. Deliberately broad; narrowed by allow-list rather than by guessing
# which TLDs a customer might use.
#
# The suffix list is deliberately *not* every TLD. Two-letter country codes and short generic ones
# (two-letter country codes and the shortest generic ones) match ordinary prose and identifiers: the
# first version flagged a storage-key collision example twelve times, because that example lists
# five spellings of one alias and one of them reads as a host. A scanner whose output has to be
# triaged is a scanner people stop reading, so the list covers the suffixes an internal or corporate
# host actually uses.
#
# The trailing lookahead rejects *file names* rather than listing them one by one: several of this
# repo's own file names open with a host-shaped prefix, and allow-listing each new one as it appears
# is a treadmill. A real host is not immediately followed by a source-file extension. Written
# without quoting those prefixes, because this file is scanned too - the first draft of this very
# comment tripped the check.
HOST_PATTERN = (
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){1,}"
    r"(?:com|org|net|gov|edu|io|dev|cloud|local|localhost|corp|internal|intranet|lan|"
    r"invalid|example|test|localdomain)\b"
    r"(?!\.(?:md|ya?ml|json|py|txt|toml|lock|cfg|ini|sql|sh|ps1|log|csv|html?|example)\b)"
)
_HOST = re.compile(HOST_PATTERN, re.IGNORECASE)

# Suffixes reserved for documentation (RFC 2606 / RFC 6761) plus this repo's own file-name
# fragments. Anything ending in one of these is free to appear anywhere.
HOST_ALLOW_SUFFIXES = (
    ".invalid",
    ".example",
    ".example.com",
    ".test",
    ".localdomain",
)

# Real public domains the repo legitimately cites: dependency homes, standards, licences, docs.
# Every entry is a deliberate decision, which is the point - an internal host cannot arrive by
# accident, only by someone adding it here.
HOST_ALLOW = {
    # RFC 2606's reserved second-level domains, as bare tokens. The suffix rule above covers
    # "h.example.com" but not "example.com" itself, which appears in this file's own allow rules.
    "example.com",
    "example.org",
    "example.net",
    "github.com",
    "gofastmcp.com",
    "keepachangelog.com",
    "modelcontextprotocol.io",
    "semver.org",
    "www.apache.org",
    "www.sap.com",
    "help.sap.com",
    "www.w3.org",
    "pypi.org",
    "psycopg.org",
    "www.psycopg.org",
    # A public, global Microsoft endpoint, not a customer host. The BI connector detects it to
    # report that a BOBJ deployment authenticates through Entra ID rather than accepting a password,
    # and naming it is the whole point of that detection - the alternative is reporting an
    # SSO-protected endpoint as broken. Allow-listed deliberately, as this file's own failure
    # message instructs, rather than by widening the pattern.
    "login.microsoftonline.com",
}


def _host_tokens(text: str) -> set[str]:
    """Dotted host names that are neither a documentation form nor an allow-listed public domain."""
    hits: set[str] = set()
    for match in _HOST.finditer(text):
        token = match.group(0).lower()
        if token in HOST_ALLOW:
            continue
        if any(token.endswith(suffix) for suffix in HOST_ALLOW_SUFFIXES):
            continue
        hits.add(token)
    return hits


# Working-tree directories never scanned (git-ignored artefacts, caches, and the fixtures dir where
# synthetic sample names deliberately live).
SKIP_DIRS = {
    ".git",
    ".venv",
    "fixtures",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "output",
    "extracts",
    "cache",
}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
    ".example",
    "",
    ".lock",
}
_ALWAYS_SCAN_NAMES = {".gitignore", ".gitattributes"}


def _tokens(text: str) -> set[str]:
    # Strip backslashes first: they are SQL-LIKE / regex escape artefacts, never part of a BW
    # object name, and they would otherwise split a real name mid-token and hide it from the
    # pattern (e.g. an escaped "ABC\_O3" reads as the too-short "ABC"). Found by a live miss.
    normalized = text.replace("\\", "")
    return {m.group(0) for m in _COMBINED.finditer(normalized)} - ALLOW


def _committable_paths() -> list[Path] | None:
    """Files that could end up in a commit: tracked, plus untracked-but-not-ignored.

    The check is about what gets *committed*, so a git-ignored file (``profiles.yaml``,
    ``landscape.local.md``, ``tmp_*``) is out of scope by definition — it cannot reach a commit.
    Asking git rather than re-implementing ``.gitignore`` keeps the two from drifting, and a check
    that reports unactionable hits is a check people learn to ignore.

    Returns ``None`` when git is unavailable, so the caller can fall back to a filesystem walk.
    """
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [_ROOT / name for name in proc.stdout.split("\0") if name]


def _walk_paths() -> list[Path]:
    """Fallback filesystem walk (no git): skips known artefact directories."""
    return [
        path
        for path in _ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(_ROOT).parts)
    ]


def scan_working_tree() -> tuple[set[str], set[str], set[str]]:
    """Object-name, host-name and IP-address hits across everything that could be committed."""
    paths = _committable_paths()
    git_aware = paths is not None
    if paths is None:
        paths = _walk_paths()
    found: set[str] = set()
    hosts: set[str] = set()
    addresses: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(_ROOT)
        # tests/fixtures/ holds deliberately synthetic sample names.
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in _ALWAYS_SCAN_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = _tokens(text)
        if hits:
            print(f"  working tree {rel}: {sorted(hits)}")
            found |= hits
        host_hits = _host_tokens(text)
        if host_hits:
            print(f"  working tree {rel}: host name(s) {sorted(host_hits)}")
            hosts |= host_hits
        ip_hits = _ipv4_tokens(text)
        if ip_hits:
            print(f"  working tree {rel}: IP address(es) {sorted(ip_hits)}")
            addresses |= ip_hits
    if not git_aware:
        print("  (git unavailable: scanned the filesystem, so git-ignored files were included)")
    return found, hosts, addresses


def _history_grep(pattern: str) -> list[str]:
    """Every token in any commit matching ``pattern``, outside the fixtures directory."""
    revs = subprocess.run(
        ["git", "rev-list", "--all"], cwd=_ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    if not revs:
        return []
    proc = subprocess.run(
        ["git", "grep", "-hIoP", pattern, *revs, "--", ".", ":(exclude)tests/fixtures/*"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def scan_history() -> tuple[set[str], set[str], set[str]]:
    """Object-name, host-name and IP-address hits anywhere in history.

    A clean tip is meaningless if an earlier commit leaked, and a squashed commit does not remove
    history - so every pattern is replayed over every reachable revision.
    """
    found = {
        token
        for token in _history_grep(PATTERN)
        if token not in ALLOW and _COMBINED.fullmatch(token)
    }
    hosts: set[str] = set()
    for token in _history_grep(HOST_PATTERN):
        if not _HOST.fullmatch(token):
            continue
        hosts |= _host_tokens(token)
    addresses: set[str] = set()
    for token in _history_grep(IPV4_PATTERN):
        if not _IPV4.fullmatch(token):
            continue
        addresses |= _ipv4_tokens(token)
    return found, hosts, addresses


def main() -> int:
    working_tree, tree_hosts, tree_ips = scan_working_tree()
    history, history_hosts, history_ips = scan_history()
    failed = False
    if working_tree or history:
        failed = True
        print("\nPotential customer object tokens detected:")
        if working_tree:
            print(f"  working tree: {sorted(working_tree)}")
        if history:
            print(f"  git history:  {sorted(history)}")
        print(
            "\nCustomer object names must never be committed (working tree or history). "
            "Move synthetic examples into tests/fixtures/; use placeholder forms elsewhere. "
            "If a name reached history, purge it with git filter-repo, not just a delete."
        )
    if tree_hosts or history_hosts:
        failed = True
        print("\nPotential host names detected:")
        if tree_hosts:
            print(f"  working tree: {sorted(tree_hosts)}")
        if history_hosts:
            print(f"  git history:  {sorted(history_hosts)}")
        print(
            "\nHost names must never be committed (mission Rule 5). Connection details belong in "
            "the git-ignored .env / profiles.yaml only. Use a reserved documentation form "
            "(.invalid, .example, .example.com) in anything tracked. If the host is a genuine "
            "public domain this project cites, add it to HOST_ALLOW here on purpose."
        )
    if tree_ips or history_ips:
        failed = True
        print("\nPotential IP addresses detected:")
        if tree_ips:
            print(f"  working tree: {sorted(tree_ips)}")
        if history_ips:
            print(f"  git history:  {sorted(history_ips)}")
        print(
            "\nIP addresses must never be committed (mission Rule 5): an address names a server "
            "and a subnet whether or not it is globally routable. Use an RFC 5737 documentation "
            "range (192.0.2.x, 198.51.100.x, 203.0.113.x) in anything tracked. A real address "
            "most often arrives quoted verbatim out of a driver error message - scrub the message, "
            "do not paste it. If the value is genuinely not an address, add its prefix to "
            "_IP_ALLOW_PREFIXES here on purpose."
        )
    if failed:
        return 1
    print("Customer-metadata leak check clean (working tree + full history).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
