"""Synthetic sample values for the live BI connector tests (``tests/test_external_bi.py``).

**Why these live here.** Two of the connector's guarantees can only be tested against strings that
*look like* the thing being excluded:

* host scrubbing needs a host under a real-looking TLD - a reserved ``.invalid`` name is not
  matched by the scrubber, so testing with one would prove nothing;
* the BW generated-view parser needs a customer-namespace object name.

Both are exactly what the leak check rejects, and ``tests/fixtures/`` is the one directory it
skips - the remedy its own failure message names. Importing from here keeps the check free of
per-file carve-outs, which matters most in the files that test security properties.

Everything below is invented. The organisation names are fictional, the object names use a ``ZDEMO``
stem that exists on no system, and no value came from a customer landscape.
"""

from __future__ import annotations

# --- host-shaped values, for the scrubber -------------------------------------------------------

#: Hosts the scrubber must redact. Fictional organisations under real TLDs, plus the single-label
#: internal suffixes a corporate network actually uses.
HOSTS_TO_SCRUB: tuple[str, ...] = (
    "srv01.somecompany.org",
    "Connection to bihost.fictionalco.com",
    "box.corp",
    "node.internal",
)

#: A content name that has a host embedded in it - the exact shape that leaked. On the reference
#: landscape a Tableau connection's caption *is* the database server's FQDN, so a name built from
#: one carries a host into the payload.
NAME_WITH_EMBEDDED_HOST = "report on srv01.somecompany.org"

#: The organisation fragment asserted absent from scrubbed output.
SCRUBBED_DOMAIN = "somecompany.org"

#: Values the scrubber must leave exactly as they are. A scrubber that mangles ordinary names is
#: worse than none, because the damage is silent.
NAMES_TO_KEEP: tuple[str, ...] = (
    "Demo Margin Dashboard",
    "ZSS_ZDEMO_L01_Q01_H",
    "Extract",
    "",
)


# --- BW generated SAP HANA view paths -----------------------------------------------------------

#: A BW-generated HANA view path as Tableau stores it in ``data_connections.dbname``: the package
#: path with separators stripped, so the ``_SYS_BIC`` marker survives but the structure does not.
#: Shaped exactly like the real ones, with an invented provider and query.
BW_VIEW_PATH = "system-local_bw_bw2hana_query_zdemo_l01ZSS_ZDEMO_L01_Q01_H_SYS_BIC"

#: What must be parsed out of it.
BW_VIEW_NAME = "ZSS_ZDEMO_L01_Q01_H"
BW_VIEW_PROVIDER = "ZDEMO_L01"

#: ``(dbname, expected view, expected provider)``. Covers the path with a provider segment, two
#: without one, and three that carry no marker at all and must therefore claim nothing.
BW_VIEW_CASES: tuple[tuple[str | None, str | None, str | None], ...] = (
    (BW_VIEW_PATH, BW_VIEW_NAME, BW_VIEW_PROVIDER),
    ("SomeFolderZSS_ZDEMO_L04_Q01_H_SYS_BIC", "ZSS_ZDEMO_L04_Q01_H", None),
    ("SomeFolderZSS_ZDEMO_L04_Q01_H_SYS_BICextract", "ZSS_ZDEMO_L04_Q01_H", None),
    ("plain_database", None, None),
    ("", None, None),
    (None, None, None),
)

#: A published-datasource name and a dimension table for the non-BW rows, so the classifier is
#: exercised on traffic it must *not* claim as a calc view.
PLAIN_DATABASE = "salesdb"
PLAIN_TABLE = "DIM_CUSTOMER"
