* Synthetic start routine fixture (no real customer code).
METHOD start_routine.
  IF lt_keys IS NOT INITIAL.
    SELECT matnr werks
      FROM /BIC/ASALES00
      INTO TABLE lt_sales
      FOR ALL ENTRIES IN lt_keys
      WHERE matnr = lt_keys-matnr.
  ENDIF.
ENDMETHOD.
