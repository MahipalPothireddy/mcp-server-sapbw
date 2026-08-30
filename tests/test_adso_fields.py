"""An Advanced DSO's field list comes from the dictionary, not from its text table (defect D14).

Synthetic names only. The ``/BIC/`` names are built by concatenation so the customer-metadata scan
stays clean.

**What was wrong.** The field list was assembled from ``RSOADSOT`` - the *text* table - as "every
column that happens to have a description, plus the key fields". Two failures in one, both measured
against a production Advanced DSO whose generated active table has **332** columns:

* it reported **15** fields, because a field with no maintained description was invisible
  unless it was part of the semantic key. The field list was a description inventory wearing
  a field list's name, and nothing in the response said so;
* ``RSOADSOT.COLNAME`` also holds BW's escape-encoded internal identifiers, so entries like
  ``!23!2F!2F!2F0COUNTRY!2F0COUNTRY`` (``#///0COUNTRY`` encoded) arrived as *fields*, citing the
  text table as provenance. Nothing downstream could tell them from real columns.

Advanced DSOs are the dominant provider type on the reference system - 248 of them - so this was the
field list for most of the landscape.

The fixture therefore contains all three shapes on purpose: described columns, an
**undescribed** column, and an encoded text row that is not a column at all. A reader that
keeps using the text table fails on the first two; one that does not filter fails on the third.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.providers import Provider
from mcp_server_sapbw.repositories.providers import (
    ProvidersRepository,
    _infoobject_candidates,
)

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "adso_header": "RSOADSO",
    "adso_text": "RSOADSOT",
    "adso_keyfields": "RSOADSOKEYFIELDS",
    "dict_columns": "DD03L",
    "infoobject": "RSDIOBJ",
}

ADSO = "SALES_ADSO"
ACTIVE_TABLE = "/BIC/" + "A" + ADSO + "2"

# A synthetic customer-namespace InfoObject and the column BW would generate for it. Assembled
# from fragments because the customer-metadata scan cannot tell a synthetic Z-namespace name from
# a real one, and the Z namespace is precisely what the naming-convention test needs.
_CUSTOM_IOBJ = "Z" + "SYNTH_DT"
_CUSTOM_COLUMN = "/BIC/" + _CUSTOM_IOBJ

#: (FIELDNAME, POSITION, KEYFLAG). The last two are the point: an undescribed field must still be
#: reported, and a customer-namespace column must resolve to its InfoObject.
_COLUMNS: list[tuple[str, int, str]] = [
    ("BILL_NUM", 1, "X"),
    ("BILL_ITEM", 2, "X"),
    ("RECORDMODE", 3, ""),
    ("NO_TEXT_FIELD", 4, ""),
    (_CUSTOM_COLUMN, 5, ""),
    ("NOT_AN_INFOOBJECT", 6, ""),
]

#: Only some columns have a maintained description - and one text row is not a column at all.
_ENCODED_NON_COLUMN = "!23!2F!2F!2F0COUNTRY!2F0COUNTRY"
_FIELD_TEXTS: list[tuple[str, str, str]] = [
    ("BILL_NUM", "E", "Billing document"),
    ("BILL_ITEM", "E", "Billing item"),
    ("RECORDMODE", "E", "BW delta process update mode"),
    (_CUSTOM_COLUMN, "E", "Billing date"),
    (_ENCODED_NON_COLUMN, "E", "Sold to Country"),
]

#: The InfoObject catalogue. ``NOT_AN_INFOOBJECT`` is deliberately absent so one column stays
#: ``inferred`` while the rest are ``confirmed``.
_INFOOBJECTS = {"0BILL_NUM", "0BILL_ITEM", "0RECORDMODE", "0NO_TEXT_FIELD", _CUSTOM_IOBJ}


class ScriptedConnection:
    def __init__(self, *, dictionary: bool = True) -> None:
        self.dictionary = dictionary
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        params = [str(p) for p in (parameters or [])]
        if "RSOADSOKEYFIELDS" in sql:
            return [("BILL_NUM",), ("BILL_ITEM",)]
        if "RSOADSOT" in sql:
            if "COLNAME" in sql and "LANGU" in sql and "DESCRIPTION" in sql:
                return list(_FIELD_TEXTS)
            return [("E", "Sales billing store", "tooltip")]
        if "RSOADSO" in sql:
            return [("SD", "DEVUSER", "SD")]  # INFOAREA, OWNER, BWAPPL
        if "DD03L" in sql:
            if not self.dictionary:
                return []
            # Emulate the predicate: only the requested table's columns come back.
            if params and params[0] != ACTIVE_TABLE:
                return []
            return list(_COLUMNS)
        if "RSDIOBJ" in sql:
            return [(name,) for name in params if name in _INFOOBJECTS]
        return []


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _provider(connection: ScriptedConnection | None = None, present: set[str] | None = None) -> Any:
    repo = ProvidersRepository(connection or ScriptedConnection(), _capability(present))
    described = repo.describe(ADSO, object_type="adso")
    assert isinstance(described, Provider), described
    return described


# --- the field list ---------------------------------------------------------------------------


def test_every_dictionary_column_is_a_field() -> None:
    """Including the ones nobody wrote a description for, which is what the defect dropped."""
    fields = _provider().fields
    assert [f.name for f in fields] == [name for name, _pos, _key in _COLUMNS]


def test_an_undescribed_field_is_reported_with_no_description_rather_than_omitted() -> None:
    field = next(f for f in _provider().fields if f.name == "NO_TEXT_FIELD")
    assert field.description is None
    assert field.position == 4


def test_an_encoded_text_row_is_not_a_field() -> None:
    """The other half of D14: RSOADSOT.COLNAME holds internal identifiers, not only column names."""
    names = {f.name for f in _provider().fields}
    assert _ENCODED_NON_COLUMN not in names
    assert not any(name.startswith("!") for name in names), (
        "an escape-encoded internal identifier reached the field list"
    )


def test_fields_carry_their_dictionary_position_in_order() -> None:
    fields = _provider().fields
    assert [f.position for f in fields] == [1, 2, 3, 4, 5, 6]


def test_provenance_cites_the_dictionary_and_the_generated_table() -> None:
    """The field's authority is the dictionary now, and the record has to say so."""
    field = next(f for f in _provider().fields if f.name == "BILL_NUM")
    assert field.provenance.source_table == "DD03L"
    assert field.provenance.source_key["TABNAME"] == ACTIVE_TABLE


def test_the_key_flag_comes_from_both_sources() -> None:
    """KEYFLAG is the table's key; RSOADSOKEYFIELDS is the declared semantic key - union.

    They agreed on the object measured on production. A union means that if a release disagrees,
    the answer understates neither side rather than silently picking one.
    """
    by_name = {f.name: f for f in _provider().fields}
    assert by_name["BILL_NUM"].is_key is True
    assert by_name["BILL_ITEM"].is_key is True
    assert by_name["RECORDMODE"].is_key is False


# --- the naming layer, which is the original question ------------------------------------------


def test_fields_say_which_naming_layer_their_name_belongs_to() -> None:
    """The response handed back ``BILL_NUM`` without saying it was a column and not an InfoObject.

    A reader comparing that against BW's own display sees ``0BILL_NUM`` and cannot tell a naming
    convention from a wrong answer.
    """
    assert all(f.name_layer == "hana_column" for f in _provider().fields)


def test_a_standard_infoobject_is_resolved_by_regaining_its_leading_zero() -> None:
    field = next(f for f in _provider().fields if f.name == "BILL_NUM")
    assert field.infoobject == "0BILL_NUM"
    assert field.infoobject_resolution == "confirmed"


def test_a_customer_infoobject_is_resolved_by_dropping_its_namespace() -> None:
    field = next(f for f in _provider().fields if f.name.endswith(_CUSTOM_IOBJ))
    assert field.infoobject == _CUSTOM_IOBJ
    assert field.infoobject_resolution == "confirmed"


def test_a_column_the_catalogue_does_not_know_stays_inferred() -> None:
    """The distinction the field exists for: the convention suggested it, nothing confirmed it."""
    field = next(f for f in _provider().fields if f.name == "NOT_AN_INFOOBJECT")
    assert field.infoobject == "0NOT_AN_INFOOBJECT"
    assert field.infoobject_resolution == "inferred"


def test_the_response_says_the_mapping_is_derived_not_declared() -> None:
    caveats = " ".join(_provider().caveats)
    assert "name_layer" in caveats
    assert "no table on this release declares the mapping" in caveats


# --- degrading honestly ------------------------------------------------------------------------


def test_without_the_dictionary_the_thin_field_list_says_it_is_thin() -> None:
    """The old behaviour is still the fallback, but it no longer passes itself off as complete."""
    provider = _provider(present={"adso_header", "adso_text", "adso_keyfields"})
    caveats = " ".join(provider.caveats)
    assert "dictionary could not be read" in caveats
    # The fallback keeps the semantic key, which is what made the old list usable at all.
    assert {f.name for f in provider.fields} >= {"BILL_NUM", "BILL_ITEM"}


def test_an_empty_dictionary_for_the_generated_table_is_reported() -> None:
    provider = _provider(ScriptedConnection(dictionary=False))
    caveats = " ".join(provider.caveats)
    assert "carries no dictionary columns" in caveats


# --- naming conventions the reference system actually uses ------------------------------------


def test_a_namespaced_column_is_not_given_a_leading_zero() -> None:
    """``/B299/S_IPNUM_CR`` was resolved to ``0/B299/S_IPNUM_CR``, which names nothing.

    381 InfoObjects on the reference system carry a namespace in their own name, so a ``/NS/``
    column that is not ``/BIC/`` already *is* the InfoObject name. Prepending a zero produced
    nonsense - the exact failure the resolution flag exists to prevent, since it was reported with
    the same shape as a correct answer.
    """
    assert _infoobject_candidates("/B299/S_IPNUM_CR") == ["/B299/S_IPNUM_CR"]
    assert all(not form.startswith("0/") for form in _infoobject_candidates("/B299/S_IPNUM_CR"))


def test_a_generated_customer_column_offers_the_stripped_name_first() -> None:
    assert _infoobject_candidates("/BIC/" + "ZFOO")[0] == "ZFOO"


def test_a_plain_column_offers_the_zero_prefixed_name_first() -> None:
    assert _infoobject_candidates("BILL_NUM")[0] == "0BILL_NUM"


def test_an_empty_column_offers_nothing() -> None:
    assert _infoobject_candidates("   ") == []


def test_the_first_confirmed_candidate_wins_over_the_first_guess() -> None:
    """A column whose plain form is in the catalogue but whose zero-prefixed form is not.

    Offering every form and taking the first *confirmed* one is what stops the answer depending on
    which convention was guessed first.
    """

    class OnlyPlainForm(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDIOBJ" in sql:
                wanted = {str(p) for p in (parameters or [])}
                return [("NOT_AN_INFOOBJECT",)] if "NOT_AN_INFOOBJECT" in wanted else []
            return super().execute_select(sql, parameters)

    field = next(f for f in _provider(OnlyPlainForm()).fields if f.name == "NOT_AN_INFOOBJECT")
    assert field.infoobject == "NOT_AN_INFOOBJECT"
    assert field.infoobject_resolution == "confirmed"


# --- descriptions are keyed by InfoObject, not by column --------------------------------------


def test_a_description_is_found_through_the_resolved_infoobject() -> None:
    """``RSOADSOT.COLNAME`` holds InfoObject names, so a column-keyed join finds nothing.

    Measured on the reference system: 52,949 text rows read like ``0CALDAY`` - the InfoObject - and
    for one production ADSO **none** of its 10 text rows matched a column of its active table. The
    consequence was every field of every Advanced DSO coming back with no description while the
    descriptions sat there unjoined.
    """

    class TextsByInfoObject(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSOADSOT" in sql and "COLNAME" in sql and "DESCRIPTION" in sql:
                # Keyed by InfoObject name, as the real table is.
                return [("0BILL_NUM", "E", "Billing document"), (_CUSTOM_IOBJ, "E", "Billing date")]
            return super().execute_select(sql, parameters)

    by_name = {f.name: f for f in _provider(TextsByInfoObject()).fields}
    assert by_name["BILL_NUM"].description == "Billing document"
    assert by_name[_CUSTOM_COLUMN].description == "Billing date"
    # And a field with no text row either way is still reported, just undescribed.
    assert by_name["NO_TEXT_FIELD"].description is None


def test_text_rows_that_key_to_nothing_are_reported_as_unavailable_not_unmaintained() -> None:
    """Two different answers, and only one of them is about the object.

    On the reference system every text row for a production Advanced DSO is an escape-encoded
    internal identifier, so none of them key to a field. Reporting that as "nobody wrote
    descriptions" would send a reader to maintain texts that already exist and cannot be joined.
    """

    class OnlyEncodedTexts(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSOADSOT" in sql and "COLNAME" in sql and "DESCRIPTION" in sql:
                return [(_ENCODED_NON_COLUMN, "E", "Sold to Country")]
            return super().execute_select(sql, parameters)

    caveats = " ".join(_provider(OnlyEncodedTexts()).caveats)
    assert "unavailable on this release rather than unmaintained" in caveats
    assert "escape-encoded" in caveats


def test_partially_described_fields_report_a_plain_count_instead() -> None:
    """The other branch: some texts resolved, so the rest really are unmaintained."""

    class SomeTextsResolve(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSOADSOT" in sql and "COLNAME" in sql and "DESCRIPTION" in sql:
                return [("0BILL_NUM", "E", "Billing document")]
            return super().execute_select(sql, parameters)

    caveats = " ".join(_provider(SomeTextsResolve()).caveats)
    assert "carry no maintained description" in caveats
    assert "unavailable on this release" not in caveats
