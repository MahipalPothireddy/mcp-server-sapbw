* Synthetic end routine fixture exercising several anti-patterns (no real customer code).
METHOD end_routine.
  LOOP AT result_package ASSIGNING <r>.
    SELECT SINGLE bezei FROM /BIC/ASALES00 INTO lv_txt WHERE spras = 'E'.
  ENDLOOP.
  SELECT * FROM /BI0/PMATERIAL INTO TABLE lt_mat FOR ALL ENTRIES IN lt_x WHERE matnr = lt_x-matnr.
  DELETE ADJACENT DUPLICATES FROM lt_mat.
  DELETE lt_mat WHERE matnr = ''.
  CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'.
  lo_helper->transform( ).
ENDMETHOD.
