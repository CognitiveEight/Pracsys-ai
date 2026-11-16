# SIM-208: Standard layouts — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | 14 sample titles across all families | inferred | the right profile for each | `test_infer_layout` |
| AC-2 | a bail application | rendered | left margin 1.5", 14 pt; court name, case no., VERSUS and title centred; party lines and body not centred | `test_court_filing_layout` |
| AC-3 | a legal notice with a letterhead | rendered | 1.15 spacing; LEGAL NOTICE / mode / CONFIDENTIAL centred; letterhead and addressee not centred | `test_notice_layout_keeps_letterhead_left` |
| AC-4 | a rent agreement | rendered | title centred; body after "This Agreement is made" not centred | `test_agreement_layout_title_centred_body_justified` |
| AC-5 | an untitled draft | rendered | previous defaults (12 pt, 1.5, nothing centred) | `test_untitled_draft_keeps_previous_defaults` |
