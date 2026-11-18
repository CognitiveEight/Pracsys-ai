# SIM-209: Draft table totals — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a cap table whose share total is wrong, while its currency and % totals are right | checked | only the share total is flagged, with the right figure | `test_wrong_total_is_flagged_right_ones_left` |
| AC-2 | the same table with fix on | checked | the figure is replaced, keeping its bold | `test_fix_mode_replaces_the_figure` |
| AC-3 | Indian grouping with an INR prefix | fixed | `INR 1,65,00,000` | `test_indian_grouping_and_currency_kept` |
| AC-4 | a placeholder in the column | checked | no flag | `test_placeholder_or_text_makes_column_unverifiable` |
| AC-5 | rounded percentages summing to 99.99 | checked | no flag | `test_percent_rounding_tolerated` |
| AC-6 | two sub-totals | checked | each covers its own block | `test_subtotals_each_cover_their_own_block` |
| AC-7 | a table without a total, and prose | checked | unchanged | `test_tables_without_totals_and_prose_untouched` |
