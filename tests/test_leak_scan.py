"""Tests for the customer-metadata leak check (``scripts/customer_metadata_scan.py``, mission §10).

**Why this file exists.** The scanner enforces mission Rule 4 (no customer metadata in the
repository) and Rule 5 (no hosts anywhere tracked), it runs in CI as a merge gate, and it had **no
tests of any kind**. That is the same gap that let D66 through: the code was right, nobody had ever
asserted it, so a later change had nothing to contradict it. The scanner earned its keep twice in
one session -- it rejected a register entry quoting real ABAP program prefixes, and it found a
125-line credentials file named ``env`` sitting untracked and *un-ignored* in the working tree, one
``git add .`` from being committed.

**The sample strings live in ``tests/fixtures/leak_scan_samples.py``, not here.** A test for these
patterns has to contain strings that match them, and any file holding those strings is a hit.
``tests/fixtures/`` is the one directory the scanner skips, and its own failure message names that
as the remedy. Importing them keeps the check free of per-file carve-outs, which matters more in
this mechanism than anywhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT / "scripts", _ROOT / "tests" / "fixtures"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from customer_metadata_scan import (  # noqa: E402
    _HOST,
    ALLOW,
    HOST_ALLOW,
    SKIP_DIRS,
    _host_tokens,
    _ipv4_tokens,
    _tokens,
)
from leak_scan_samples import (  # noqa: E402
    ESCAPED_HIT,
    HOST_HIT_UPPERCASE,
    HOST_HITS,
    HOST_MISSES,
    HOST_SHAPED_FILE_NAMES,
    IP_BARE,
    IP_DRIVER_ERROR,
    IP_DRIVER_ERROR_ENDPOINTS,
    IP_HITS,
    IP_MISSES,
    OBJECT_HITS,
    OBJECT_MISSES,
    STORAGE_KEY_ALIASES,
)

# --- patterns 1 and 2: generated tables and the customer namespace ------------------------------


@pytest.mark.parametrize("text", OBJECT_HITS)
def test_object_name_shapes_are_caught(text: str) -> None:
    assert _tokens(text), f"expected a hit in {text!r}"


@pytest.mark.parametrize("text", OBJECT_MISSES)
def test_placeholder_forms_are_not_flagged(text: str) -> None:
    assert not _tokens(text), f"unexpected hit in {text!r}"


def test_escaped_names_are_still_caught() -> None:
    """A SQL-LIKE escape must not split a name mid-token and hide it. Found by a live miss."""
    assert _tokens(ESCAPED_HIT)


def test_the_allow_list_only_holds_non_customer_tokens() -> None:
    """Every entry must be a token the patterns match, or it is dead weight hiding nothing."""
    for token in ALLOW:
        assert _tokens(token) == set(), f"{token} is allow-listed but still reported"


# --- pattern 3: host names (added when a real landscape's FQDNs were handed over) ---------------


@pytest.mark.parametrize("text", HOST_HITS)
def test_host_names_are_caught(text: str) -> None:
    assert _host_tokens(text), f"expected a host hit in {text!r}"


@pytest.mark.parametrize("text", HOST_MISSES)
def test_documentation_and_allowed_domains_are_not_flagged(text: str) -> None:
    assert not _host_tokens(text), f"unexpected host hit in {text!r}"


@pytest.mark.parametrize("text", HOST_SHAPED_FILE_NAMES)
def test_host_shaped_file_names_are_not_flagged(text: str) -> None:
    """Excluded structurally by the trailing lookahead, not by an allow-list entry per file."""
    assert not _host_tokens(text), f"unexpected host hit in {text!r}"


def test_a_storage_key_collision_example_is_not_read_as_a_host() -> None:
    """The false positive that made the first version of the host pattern unusable.

    Dropping the short generic suffixes is what fixed it, so this pins the decision rather than
    leaving it as a comment somebody later widens.
    """
    assert not _host_tokens(STORAGE_KEY_ALIASES)


def test_case_is_ignored_when_matching_hosts() -> None:
    assert _host_tokens(HOST_HIT_UPPERCASE)


def test_the_host_allow_list_entries_would_otherwise_be_reported() -> None:
    """An allow-listed domain the pattern never matches is hiding nothing and should be removed."""
    for domain in HOST_ALLOW:
        assert _HOST.fullmatch(domain), f"{domain} is allow-listed but the pattern never matches it"


# --- the guard on the guard ---------------------------------------------------------------------


def test_the_scanner_source_is_itself_clean() -> None:
    """The scanner is scanned too, and its own comments have tripped it. Assert it stays clean.

    Not circular: the risk is real and has happened twice. Explaining *why* a host-shaped file-name
    prefix is excluded invites quoting one, and quoting one is a hit.
    """
    source = (_ROOT / "scripts" / "customer_metadata_scan.py").read_text(encoding="utf-8")
    assert _tokens(source) == set()
    assert _host_tokens(source) == set()
    assert _ipv4_tokens(source) == set()


def test_this_test_module_is_itself_clean() -> None:
    """A test proving these patterns fire must not itself be a leak. Hence the fixture import.

    The address rule was added with its samples written inline here, and the scan then failed on
    its own test suite - caught by running the check against a rewritten clone rather than in
    place. That is what this assertion is for.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    assert _tokens(source) == set()
    assert _host_tokens(source) == set()
    assert _ipv4_tokens(source) == set()


def test_the_sample_fixture_is_exempt_only_by_living_under_fixtures() -> None:
    """The samples *are* hits -- that is the point. They are legal only because of where they sit.

    Asserted so nobody moves the module up a directory to tidy the imports and quietly breaks the
    CI gate, and so the exemption stays a property of the location rather than of the file name.
    """
    samples = _ROOT / "tests" / "fixtures" / "leak_scan_samples.py"
    assert samples.is_file()
    assert "fixtures" in SKIP_DIRS
    assert "fixtures" in samples.relative_to(_ROOT).parts
    source = samples.read_text(encoding="utf-8")
    assert _tokens(source), "the object samples stopped matching, so those tests prove nothing"
    assert _host_tokens(source), "the host samples stopped matching, so those tests prove nothing"
    assert _ipv4_tokens(source), (
        "the address samples stopped matching, so those tests prove nothing"
    )


# --- pattern 4: IPv4 addresses -----------------------------------------------------------------
#
# D76. An address is not a BW object name and not a dotted host name, so patterns 1-3 could not
# see one, and two internal addresses reached a public repository through a driver error message
# pasted into a comment, a test and the CHANGELOG. These cases are the regression.


@pytest.mark.parametrize("text", IP_HITS)
def test_ip_addresses_are_caught(text: str) -> None:
    assert _ipv4_tokens(text), f"expected an IP hit in {text!r}"


@pytest.mark.parametrize("text", IP_MISSES)
def test_reserved_ranges_and_versions_are_not_flagged(text: str) -> None:
    assert not _ipv4_tokens(text), f"unexpected IP hit in {text!r}"


def test_the_driver_error_shape_is_caught_in_full() -> None:
    """Both endpoints, not just the first: the message names the server *and* the client."""
    assert _ipv4_tokens(IP_DRIVER_ERROR) == IP_DRIVER_ERROR_ENDPOINTS


def test_an_address_is_caught_even_where_a_host_name_would_not_be() -> None:
    """The two rules are independent; neither substitutes for the other."""
    assert not _host_tokens(IP_BARE), (
        "an address has no alphabetic TLD, so the host rule cannot see it"
    )
    assert _ipv4_tokens(IP_BARE)
