"""Check the frozen code decodes against the connected system's own dictionary (D44).

One bulk read of ``DD07L``/``DD07T`` for every registered domain - measured at 299 rows across 22
domains in 0.22s on the reference system, so cost is not a consideration - then a diff against what
the source has frozen.

**Why the registry imports private symbols from other modules.** Each decode table lives next to the
code that uses it, which is where it belongs: a reader of ``queries.py`` should see the
``ALERTLEVEL`` mapping in ``queries.py``. Moving fifty into one module to make this check tidy would
trade real locality for the appearance of structure. So the registry reaches for them, and that is
the one place where reaching for a private is the honest option - its entire job is to inventory
mappings it does not own.

**Every domain name here was resolved against a live dictionary before being registered.** A wrong
name and a genuinely absent domain look identical in the result, so a name that did not resolve
would make the report cry wolf on every system. All were confirmed to hold values first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.dialect import quote_ident, record_capability_read
from ..models.domains import DomainCheck, DomainDriftReport, DomainFinding
from ..models.provenance import UnsupportedResult
from ..repositories.base import Repository
from ..repositories.chains import _STEP_STATE_LABEL

# --- the registry ------------------------------------------------------------------------------
#
# Imported here rather than re-declared, so the check can never drift from the mapping it checks.
from ..repositories.health import (
    _RSPM_HOUSEKEEPING,
    _RSPM_STATUS_MAP,
    _STATUS_MAP,
    _TLOGO_TO_KIND,
)
from ..repositories.queries import (
    _ALERT_LEVELS,
    _CONTYPE_LABELS,
    _DEFTP_TO_TYPE,
    _EXCABSREL_LABELS,
    _LAYTP_TO_ROLE,
    _LOWFLAG_TO_SOURCE,
    _OPT_DECLARED,
    _SUBDEFTP_TO_TYPE,
)
from ..repositories.security import _OPERATORS, _SIGNS
from ..repositories.transformations import (
    _AGGR_TO_BEHAVIOUR,
    _GROUPTYPE_TO_KIND,
    _MPER_TO_KEY_DATE,
)
from ..services.aggregation import (
    _EXCEPTION_AGGREGATION,
    _KEY_FIGURE_TYPE,
    _KEYFIGURE_GENERAL,
    _NON_CUMULATIVE,
)


@dataclass(frozen=True)
class DomainExpectation:
    """What the source assumes about one dictionary domain.

    **A domain can legitimately be decoded by more than one mapping**, and assuming otherwise was
    this checker's first defect. ``RSPM_REQUEST_STATUS`` is split deliberately in
    ``repositories.health``: nine codes are load outcomes and four are housekeeping states, because
    a deleted or moved request says nothing about whether data is present, so decoding it as success
    *or* a failure would be wrong. Pointing the expectation at the outcome map alone reported the
    other four as unmapped at ``high`` severity - a false alarm against correct, carefully reasoned
    code, which is the most expensive thing a validator can do. So :func:`expect` takes the mappings
    as a list and unions them, which makes the partitioned case the ordinary one.

    ``labels`` is optional on purpose. Many decodes map a code to the server's *own* vocabulary -
    ``RSZDEFTP`` ``'REP'`` becomes ``"query"`` - and comparing that against SAP's text would report
    a difference on every row. The code set is what the important finding rests on, so a mapping
    without SAP labels is still fully checked for coverage.
    """

    domain: str
    column: str
    owner: str
    codes: frozenset[str]
    claims_complete: bool = True
    labels: Mapping[str, str] | None = None


def expect(
    domain: str,
    column: str,
    sources: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    claims_complete: bool = True,
    labels: Mapping[str, str] | None = None,
) -> DomainExpectation:
    """Build an expectation from every mapping that decodes ``domain``.

    ``sources`` is a list of ``(symbol name, mapping)`` even when there is only one, so adding a
    domain makes the author decide whether its values are partitioned rather than defaulting to the
    assumption that they are not.
    """
    codes: set[str] = set()
    for _symbol, mapping in sources:
        codes.update(str(key).strip().upper() for key in mapping)
    return DomainExpectation(
        domain=domain,
        column=column,
        owner=" + ".join(symbol for symbol, _mapping in sources),
        codes=frozenset(codes),
        claims_complete=claims_complete,
        labels=labels,
    )


#: Every entry's domain name was confirmed present in a live ``DD07L`` before it was added.
REGISTRY: tuple[DomainExpectation, ...] = (
    expect("RSZDEFTP", "RSZELTDIR.DEFTP", [("queries._DEFTP_TO_TYPE", _DEFTP_TO_TYPE)]),
    expect(
        "RSZSUBDEFTP",
        "RSZELTDIR.SUBDEFTP",
        [("queries._SUBDEFTP_TO_TYPE", _SUBDEFTP_TO_TYPE)],
        # Deliberately partial: 4 of 14. The unmapped values fall through to the older 1KYFNM
        # heuristic, which is the documented D26 decision, not an omission.
        claims_complete=False,
    ),
    expect(
        "RRXCONTYPE",
        "RSZSELECT.CONTYPE",
        [("queries._CONTYPE_LABELS", _CONTYPE_LABELS)],
        labels=_CONTYPE_LABELS,
    ),
    expect(
        "RSZ_OPERATOR_DOMAIN",
        "RSZRANGE.OPT",
        [("queries._OPT_DECLARED", _OPT_DECLARED)],
        labels=_OPT_DECLARED,
    ),
    expect(
        "RSZTYPEFLAG",
        "RSZRANGE.LOWFLAG",
        [("queries._LOWFLAG_TO_SOURCE", _LOWFLAG_TO_SOURCE)],
        labels={code: entry[0] for code, entry in _LOWFLAG_TO_SOURCE.items()},
    ),
    expect(
        "RSRA_ALERT_LEVEL",
        "RSZRANGE.ALERTLEVEL",
        [("queries._ALERT_LEVELS", _ALERT_LEVELS)],
        labels=_ALERT_LEVELS,
    ),
    expect(
        "RSRA_ABS_REL",
        "RSZSELECT.EXCABSREL",
        [("queries._EXCABSREL_LABELS", _EXCABSREL_LABELS)],
        labels=_EXCABSREL_LABELS,
    ),
    expect("RSZLAYTP", "RSZELTXREF.LAYTP", [("queries._LAYTP_TO_ROLE", _LAYTP_TO_ROLE)]),
    expect(
        "RSSTATUS",
        "RSSTATMANPART.STATUS",
        [("health._STATUS_MAP", _STATUS_MAP)],
        # BW ships more status icons than the three outcomes the server distinguishes, and an
        # unrecognised icon is already reported rather than guessed.
        claims_complete=False,
    ),
    # PARTITIONED, and getting this wrong was the checker's first false positive. The nine load
    # outcomes and the four housekeeping states cover the domain exactly - verified, 9 + 4 = 13
    # declared, nothing missing either way. The split is deliberate: a deleted or moved request
    # says nothing about whether data is present, so decoding it as a success or a failure would
    # both be wrong, and health.py reports it as a caveat instead. Naming only the outcome map
    # reported the other four as unmapped at 'high' severity, against correct code.
    expect(
        "RSPM_REQUEST_STATUS",
        "RSPMREQUEST.REQUEST_STATUS",
        [
            ("health._RSPM_STATUS_MAP", _RSPM_STATUS_MAP),
            ("health._RSPM_HOUSEKEEPING", _RSPM_HOUSEKEEPING),
        ],
    ),
    expect(
        "RSTLOGO",
        "RSPMREQUEST.TLOGO",
        [("health._TLOGO_TO_KIND", _TLOGO_TO_KIND)],
        # 140 declared values; the server maps the four provider kinds it reasons about.
        claims_complete=False,
    ),
    expect(
        "RSTRAN_AGGREGATION",
        "RSTRANRULE.AGGR",
        [("transformations._AGGR_TO_BEHAVIOUR", _AGGR_TO_BEHAVIOUR)],
    ),
    expect(
        "RSTRAN_GROUPTYPE",
        "RSTRANRULE.GROUPTYPE",
        [("transformations._GROUPTYPE_TO_KIND", _GROUPTYPE_TO_KIND)],
    ),
    expect(
        "RSMPER",
        "RSTRANRULE.MPER",
        [("transformations._MPER_TO_KEY_DATE", _MPER_TO_KEY_DATE)],
    ),
    expect(
        "RSDAGGREXC",
        "RSZCALC.AGGREXC",
        [("aggregation._EXCEPTION_AGGREGATION", _EXCEPTION_AGGREGATION)],
    ),
    expect(
        "RSDAGGRGEN",
        "RSDKYF.AGGRGEN",
        [("aggregation._KEYFIGURE_GENERAL", _KEYFIGURE_GENERAL)],
    ),
    expect("RSKYFTP", "RSDKYF.KYFTP", [("aggregation._KEY_FIGURE_TYPE", _KEY_FIGURE_TYPE)]),
    expect("RSNCUMFL", "RSDKYF.NCUMFL", [("aggregation._NON_CUMULATIVE", _NON_CUMULATIVE)]),
    # The two security decodes (D53). Registered late because the subsystem they belong to was dead
    # on the reference release and so never exercised: its column-name candidates predated the
    # capability resolver and named columns BW does not have, so nothing ever reached a decode to be
    # wrong about. Worth more than the usual drift cover here - an operator that falls through to
    # "unknown" in a permission payload hides an *exclusion*, so a release that adds a code to
    # RSZ_OPERATOR_DOMAIN should be a reported finding rather than a quiet gap. Note this is the
    # same domain as RSZRANGE.OPT above, decoded independently into a different vocabulary by a
    # different module, which is exactly why both are registered separately.
    expect(
        "RALDB_SIGN",
        "RSECVAL.TCTSIGN",
        [("security._SIGNS", _SIGNS)],
    ),
    expect(
        "RSZ_OPERATOR_DOMAIN",
        "RSECVAL.TCTOPTION",
        [("security._OPERATORS", _OPERATORS)],
    ),
    # Process-chain step state (D65). Registered for a reason the other entries do not have: this
    # decode does not merely label a value, it **classifies** one, and the classification decides
    # whether a step is reported to a customer as a failure. D65 proposed treating everything
    # outside
    # ('G','F') as failed; measured against this domain that is wrong, because 'S' (Skipped at
    # restart) and 'A' (Active) are outside the success set and are not failures - on the reference
    # system they account for 103,347 steps, appearing in 32,405 runs whose own status is green.
    #
    # So a release that adds a state must be a reported finding rather than a quiet fall-through: an
    # unrecognised code lands in neither the failed nor the OK set and would be silently counted as
    # indeterminate, which is the safe default and also invisible. This check is what makes it
    # visible.
    expect(
        "RSPC_STATE",
        "RSPCPROCESSLOG.STATE",
        [("chains._STEP_STATE_LABEL", _STEP_STATE_LABEL)],
    ),
)

#: Domains the dictionary declares that no decode table covers at all. Reported so the report is
#: about the server's coverage rather than only about its correctness.
UNMAPPED_DOMAINS: tuple[tuple[str, str, str], ...] = (
    (
        "RSZ_DISP_TUPLE",
        "RSZSELECT.EXC_DISP_TUPLE",
        "nothing - read by the condition reader and discarded (D42)",
    ),
)

_LANGUAGE = "E"
#: Index of ``DDTEXT`` in a domain-value row. Absent when ``DD07T`` could not be joined, so the row
#: length is checked rather than assumed.
_TEXT_COLUMN = 2


class DomainDriftService(Repository):
    """Reads the connected dictionary's domain values and diffs them against the frozen decodes."""

    def check(self) -> DomainDriftReport | UnsupportedResult:
        unsupported = self.require("dict_domain_values")
        if unsupported is not None:
            return unsupported

        wanted = sorted(
            {e.domain for e in REGISTRY} | {domain for domain, _c, _o in UNMAPPED_DOMAINS}
        )
        declared = self._read_domains(wanted)

        report = DomainDriftReport(
            system=self.capability.system,
            bw_release=self.capability.bw_release,
            provenance=[
                self.provenance("dict_domain_values", {"DOMNAME": f"{len(wanted)} domain(s)"}),
            ],
        )
        for expectation in REGISTRY:
            report.checks.append(self._check_one(expectation, declared))
        for domain, column, owner in UNMAPPED_DOMAINS:
            values = declared.get(domain)
            report.checks.append(
                DomainCheck(
                    domain=domain,
                    column=column,
                    owner=owner,
                    present=values is not None,
                    claims_complete=False,
                    declared_codes=len(values or {}),
                    mapped_codes=0,
                    findings=[
                        DomainFinding(
                            kind="code_unmapped",
                            severity="info",
                            domain=domain,
                            column=column,
                            owner=owner,
                            detail=(
                                f"the dictionary declares {len(values or {})} value(s) for this "
                                "domain and no decode table covers any of them, so the column is "
                                "read and its meaning never reported"
                            ),
                        )
                    ]
                    if values
                    else [],
                )
            )

        report.findings = [f for check in report.checks for f in check.findings]
        report.domains_checked = sum(1 for c in report.checks if c.present)
        report.domains_absent = sum(1 for c in report.checks if not c.present)
        report.limitations = self._limitations()
        return report

    # --- one domain ----------------------------------------------------------------------

    def _check_one(
        self, expectation: DomainExpectation, declared: dict[str, dict[str, str]]
    ) -> DomainCheck:
        values = declared.get(expectation.domain)
        check = DomainCheck(
            domain=expectation.domain,
            column=expectation.column,
            owner=expectation.owner,
            present=values is not None,
            claims_complete=expectation.claims_complete,
            declared_codes=len(values or {}),
            mapped_codes=len(expectation.codes),
        )
        if values is None:
            check.findings.append(
                DomainFinding(
                    kind="domain_absent",
                    severity="high",
                    domain=expectation.domain,
                    column=expectation.column,
                    owner=expectation.owner,
                    detail=(
                        "this system's dictionary does not declare the domain at all, so every "
                        f"code {expectation.owner} decodes is unvalidated here. Either the release "
                        "structures this column differently, or the connected user cannot read "
                        "DD07L - bw_access_report distinguishes those."
                    ),
                )
            )
            return check

        # The finding that matters: a value the dictionary declares and the source does not map. On
        # a complete mapping this is the D24 shape - it will not raise, it lands in a fallback.
        for code in sorted(set(values) - expectation.codes):
            complete = expectation.claims_complete
            check.findings.append(
                DomainFinding(
                    kind="code_unmapped",
                    severity="high" if complete else "info",
                    domain=expectation.domain,
                    column=expectation.column,
                    owner=expectation.owner,
                    code=code,
                    actual=values[code],
                    detail=(
                        "the dictionary declares this value and the decode does not map it, so a "
                        "row carrying it falls through to the decode's fallback and is reported as "
                        "though it were understood"
                        if complete
                        else "not mapped, which is expected here: this decode deliberately covers "
                        "only part of the domain"
                    ),
                )
            )

        for code in sorted(expectation.codes - set(values)):
            check.findings.append(
                DomainFinding(
                    kind="code_undeclared",
                    severity="info",
                    domain=expectation.domain,
                    column=expectation.column,
                    owner=expectation.owner,
                    code=code,
                    detail=(
                        "the decode maps this value but the dictionary does not declare it, so its "
                        "meaning rests on observation rather than on SAP's own statement"
                    ),
                )
            )

        if expectation.labels:
            for code, expected_label in sorted(expectation.labels.items()):
                actual = values.get(str(code).strip().upper())
                if actual is None or actual == expected_label:
                    continue
                check.findings.append(
                    DomainFinding(
                        kind="label_differs",
                        severity="low",
                        domain=expectation.domain,
                        column=expectation.column,
                        owner=expectation.owner,
                        code=code,
                        expected=expected_label,
                        actual=actual,
                        detail=(
                            "the recorded label is not this dictionary's text for the code. The "
                            "code still exists, so decoding continues - but a label shown as "
                            "SAP's wording is not this system's wording"
                        ),
                    )
                )
        return check

    # --- reading -------------------------------------------------------------------------

    def _read_domains(self, domains: Sequence[str]) -> dict[str, dict[str, str]]:
        """``domain -> {code: text}`` in one statement.

        The text join is a LEFT join and matched on ``AS4LOCAL`` as well as the key: a domain value
        whose text row is missing must still be reported as declared, because its *existence* is
        what the coverage finding rests on, and dropping it would understate the dictionary.
        """
        if not domains:
            return {}
        placeholders = ", ".join("?" for _ in domains)
        rows: list[tuple[Any, ...]]
        if self.capability.is_available("dict_domain_text"):
            # The aliased LEFT JOIN is beyond build_select, so the statement is assembled here - but
            # both table names still come from the capability record rather than from a literal.
            values = self._qualified("dict_domain_values")
            texts = self._qualified("dict_domain_text")
            # The statement bypasses build_select, so the reads are recorded explicitly - otherwise
            # the capability contract reports both tables as declared and never read.
            record_capability_read("dict_domain_values")
            record_capability_read("dict_domain_text")
            sql = (
                f"SELECT V.DOMNAME, V.DOMVALUE_L, T.DDTEXT FROM {values} V "
                f"LEFT JOIN {texts} T ON T.DOMNAME = V.DOMNAME AND T.VALPOS = V.VALPOS "
                f"AND T.AS4LOCAL = V.AS4LOCAL AND T.DDLANGUAGE = ? "
                f"WHERE V.AS4LOCAL = 'A' AND V.DOMNAME IN ({placeholders})"
            )
            rows = self._connection.execute_select(sql, [_LANGUAGE, *domains])
        else:
            plain = self.select(
                self.dialect.build_select(
                    columns=["DOMNAME", "DOMVALUE_L"],
                    from_logical="dict_domain_values",
                    where=["AS4LOCAL = 'A'", f"DOMNAME IN ({placeholders})"],
                    params=list(domains),
                )
            )
            rows = [(r[0], r[1], None) for r in plain]

        out: dict[str, dict[str, str]] = {}
        for row in rows:
            domain = str(row[0]).strip().upper()
            code = str(row[1]).strip().upper()
            text_value = row[_TEXT_COLUMN] if len(row) > _TEXT_COLUMN else None
            out.setdefault(domain, {})[code] = (
                str(text_value).strip() if text_value is not None else ""
            )
        return out

    def _qualified(self, logical: str) -> str:
        """``"SCHEMA"."TABLE"`` for a logical name, using the capability-resolved physical name."""
        table = self.physical(logical)
        status = self.capability.table(logical)
        schema = status.schema_name if status is not None else None
        return f'{quote_ident(schema)}.{quote_ident(table)}' if schema else quote_ident(table)

    def _limitations(self) -> list[str]:
        limits = [
            "This checks whether the codes are still DECLARED. It cannot detect a code that is "
            "still declared and has come to mean something different - no metadata can.",
            "It checks the dictionary, not the data. A value occurring in table rows that the "
            "domain never declared is a separate question; OBJVERS='R' on 870 transformation rows "
            "of the reference system is the known example.",
            "Only decodes whose domain name has been confirmed against a live dictionary are "
            f"registered - {len(REGISTRY)} of roughly fifty code tables. The rest are still frozen "
            "and unchecked, and a clean report says nothing at all about them.",
        ]
        if not self.capability.is_available("dict_domain_text"):
            limits.append(
                "DD07T is unavailable, so labels could not be compared: only code coverage was "
                "checked on this system."
            )
        return limits


