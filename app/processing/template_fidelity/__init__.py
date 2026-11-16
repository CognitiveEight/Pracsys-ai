"""Template fidelity (SIM-203): follow any uploaded template's format.

A template can arrive as a .docx, a text PDF or a scan/photo. Every source is
read into one neutral format model (``ir.TemplateIR``), rebuilt as a clean,
editable .docx (``docx_builder``), and drafts are then written *into* that
.docx (``docx_map`` + ``docx_fill``) so fonts, numbering, tables, letterhead,
header/footer and spacing all come from the template.

Nothing in this package is specific to a template or a document type.
"""
