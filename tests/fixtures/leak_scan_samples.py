"""Synthetic sample tokens for the leak-scanner tests (``tests/test_leak_scan.py``).

**Why these live here and not in the test module.** The scanner's patterns can only be tested
against strings that match them -- object-name shapes and host names -- and any file holding those
strings is a hit. ``tests/fixtures/`` is the one directory the scanner skips, and its own failure
message names this as the remedy: *"Move synthetic examples into tests/fixtures/"*. Following that
rule keeps the check free of per-file exceptions, which matters more here than anywhere: a carve-out
inside the mechanism enforcing mission Rules 4 and 5 is the first thing that would be abused.

Every name below is invented. No token here came from a customer system:

* object names use an obviously fictional ``DEMO`` / ``SOMETHING`` stem;
* host names use invented organisation names, or the RFC 2606 / RFC 6761 reserved forms
  (``.invalid``, ``.example``, ``example.com``) where the point is that they are *not* flagged.
"""

from __future__ import annotations

#: Shapes the object-name patterns must catch: generated tables and customer-namespace tokens.
OBJECT_HITS: tuple[str, ...] = (
    "/BIC/AZDEMO00100",
    "the table /BI0/PMATERIAL holds",
    "ZSOMECHAIN ran overnight",
    "YSOMETHING is a customer object",
)

#: Placeholder forms documentation is required to use instead of a real name, plus tokens too short
#: to be a customer object. None may be flagged.
OBJECT_MISSES: tuple[str, ...] = (
    "tables in the /BIC/ namespace",
    "a /BIC/<name> generated table",
    "ZAB and YCD",
)

#: A SQL-LIKE escape must not split a name mid-token and hide it from the pattern. Found by a live
#: miss: an escaped name read as a too-short prefix and went unreported.
ESCAPED_HIT = r"ABC\_O3 and ZDEMO\_TABLE"

#: Host shapes the pattern must catch. The first is the shape that prompted it: a hyphenated box
#: name under a corporate domain, which is how real connection details arrive.
HOST_HITS: tuple[str, ...] = (
    "aos-demo01.somecompany.org",
    "bihost01.fictionalco.com",
    "https://reports.fictionalco.com:8443/BOE/BI",
    "tableau.fictionalco.com",
    "postgres://user@repo.fictionalco.net:8060/workgroup",
    "box.corp",
    "server.internal",
    "node.intranet",
    "host.lan",
)

#: Shouted FQDN: hosts are case-insensitive and a pasted one is as likely to arrive upper-case.
HOST_HIT_UPPERCASE = "AOS-DEMO01.SOMECOMPANY.ORG"

#: Reserved documentation forms and the public domains this project deliberately cites. None may be
#: flagged, or the check becomes unusable in the very files that explain it.
HOST_MISSES: tuple[str, ...] = (
    "prd.example.invalid",
    "your-prd-host.example.invalid",
    "prd-hana.internal.example.com",
    "bwhost.internal.invalid",
    "host.invalid",
    "example.com",
    "github.com",
    "pypi.org",
    "gofastmcp.com",
    "modelcontextprotocol.io",
)

#: File names opening with a host-shaped prefix. Excluded structurally by the pattern's trailing
#: lookahead rather than by an allow-list entry per file, which would be a treadmill.
HOST_SHAPED_FILE_NAMES: tuple[str, ...] = (
    "see landscape.local.md for endpoints",
    "profiles.local.yaml is git-ignored",
    "settings.local.json",
    "conf.local.toml",
)

#: The false positive that made the first version of the host pattern unusable.
#: ``core/identity.py``, ``core/snapshots.py`` and ``test_identity.py`` all document the five
#: profile-alias spellings that used to collapse onto one cache file; one of them is a single label,
#: a dot and a two-letter suffix, host-shaped to a naive pattern. It appeared twelve times.
STORAGE_KEY_ALIASES = "prd/eu, prd_eu, prd.eu, prd eu and prd:eu all produced prd_eu.sqlite"
