"""Editor HTML → draft Markdown (SIM-216). Fictional text only; no I/O."""

from app.processing.docx_generator import (
    BulletList,
    Heading,
    OrderedList,
    Table,
    docx_structure,
    parse_markdown_blocks,
    render_markdown_to_docx,
)
from app.processing.draft_html import html_to_markdown
from app.processing.draft_markdown import count_placeholders, normalize_draft_markdown

EDITOR_HTML = """
<h1>Legal Notice</h1>
<p>To,<br><strong>&lt;Bank Name&gt;</strong></p>
<h2>Facts</h2>
<p>My client, <em>Asha Traders</em>, states as follows:</p>
<ol>
  <li><p>The account was opened in 2019.</p>
    <ul>
      <li>Statement A</li>
      <li>Statement B
        <ol><li>Page 1</li><li>Page 2</li></ol>
      </li>
    </ul>
  </li>
  <li><p>The amount below remains unpaid.</p></li>
</ol>
<table>
  <thead><tr><th>Item</th><th>Amount (Rs.)</th></tr></thead>
  <tbody>
    <tr><td>Principal</td><td>1,00,000</td></tr>
    <tr><td>Interest</td><td>12,000</td></tr>
    <tr><td><strong>Total</strong></td><td><strong>1,12,000</strong></td></tr>
  </tbody>
</table>
<p>Pay within 15 days to &lt;Account Number&gt;.</p>
"""

EQUIVALENT_MARKDOWN = """# Legal Notice

To,<br>**<Bank Name>**

## Facts

My client, *Asha Traders*, states as follows:

1. The account was opened in 2019.
   - Statement A
   - Statement B
     1. Page 1
     2. Page 2
2. The amount below remains unpaid.

| Item | Amount (Rs.) |
| --- | --- |
| Principal | 1,00,000 |
| Interest | 12,000 |
| **Total** | **1,12,000** |

Pay within 15 days to <Account Number>."""


def _shape(blocks):
    """Comparable structure: block types, heading levels, list nesting, cells."""

    def runs_text(runs):
        return "".join(r.text if not getattr(r, "br", False) else "\n" for r in runs)

    def lst(block):
        items = []
        for item in block.items:
            runs = getattr(item, "runs", None) or []
            children = [lst(c) for c in (getattr(item, "children", None) or [])]
            items.append((runs_text(runs), children))
        return (type(block).__name__, items)

    out = []
    for b in blocks:
        if isinstance(b, Heading):
            out.append(("h", b.level, runs_text(b.runs)))
        elif isinstance(b, (BulletList, OrderedList)):
            out.append(lst(b))
        elif isinstance(b, Table):
            out.append(("table", b.has_header,
                        [[runs_text(c) for c in row] for row in b.rows]))
        else:
            out.append(("p", runs_text(getattr(b, "runs", []))))
    return out


def test_html_converts_to_the_equivalent_markdown():
    md = normalize_draft_markdown(html_to_markdown(EDITOR_HTML))
    assert md == normalize_draft_markdown(EQUIVALENT_MARKDOWN)


def test_same_blocks_as_markdown_tables_headings_nested_lists():
    from_html = parse_markdown_blocks(normalize_draft_markdown(html_to_markdown(EDITOR_HTML)))
    from_md = parse_markdown_blocks(normalize_draft_markdown(EQUIVALENT_MARKDOWN))
    assert _shape(from_html) == _shape(from_md)
    kinds = [type(b).__name__ for b in from_html]
    assert kinds.count("Heading") == 2
    assert "Table" in kinds and "OrderedList" in kinds


def test_same_word_file_structure_as_markdown():
    a = docx_structure(render_markdown_to_docx(
        normalize_draft_markdown(html_to_markdown(EDITOR_HTML)),
        template_docx_bytes=None, title="Legal Notice"))
    b = docx_structure(render_markdown_to_docx(
        normalize_draft_markdown(EQUIVALENT_MARKDOWN),
        template_docx_bytes=None, title="Legal Notice"))
    assert a["tables"] == b["tables"] == 1
    assert a["text"] == b["text"]
    assert a["leftover_markup"] == 0


def test_script_style_iframe_dropped_with_their_content():
    html = (
        "<p>Before</p><script>alert('x')</script><style>p{color:red}</style>"
        "<iframe src='https://example.invalid'>frame text</iframe>"
        "<noscript>ns</noscript><p>After</p>"
    )
    md = html_to_markdown(html)
    assert md == "Before\n\nAfter"


def test_no_attribute_passthrough_and_unknown_tags_unwrapped():
    html = (
        '<p onclick="steal()" style="color:red">See <a href="https://example.invalid/x">'
        'the order</a> <span class="c1">dated</span> <font face="Arial">today</font>'
        '<img src="https://example.invalid/i.png" onerror="x()"></p>'
    )
    md = html_to_markdown(html)
    assert md == "See the order dated today"
    assert "example.invalid" not in md and "onclick" not in md


def test_placeholders_survive_escaped_or_raw():
    html = (
        "<p>Dear &lt;Recipient Name&gt;,</p>"
        "<p>Account <Account Number> at <Branch_Name> on <Date>.</p>"
        "<p>Signed: <strong>&lt;Advocate Name&gt;</strong></p>"
    )
    md = html_to_markdown(html)
    for label in ("<Recipient Name>", "<Account Number>", "<Branch_Name>",
                  "<Date>", "<Advocate Name>"):
        assert label in md
    assert count_placeholders(normalize_draft_markdown(md)) == 5


def test_markdown_syntax_in_text_is_escaped():
    html = "<p>1. not a list</p><p># not a heading</p><p>a | b and *c* and snake_case</p>"
    blocks = parse_markdown_blocks(html_to_markdown(html))
    assert [type(b).__name__ for b in blocks] == ["Paragraph"] * 3
    texts = ["".join(r.text for r in b.runs) for b in blocks]
    assert texts == ["1. not a list", "# not a heading", "a | b and *c* and snake_case"]


def test_table_without_header_row_and_ragged_rows():
    html = "<table><tr><td>Name</td><td>X</td><td>Y</td></tr><tr><td>Only</td></tr></table>"
    md = html_to_markdown(html)
    blocks = parse_markdown_blocks(normalize_draft_markdown(md))
    table = [b for b in blocks if isinstance(b, Table)][0]
    assert table.has_header is False  # the empty header row is dropped
    assert len(table.rows) == 2 and all(len(r) == 3 for r in table.rows)


def test_malformed_and_empty_input():
    assert html_to_markdown("") == ""
    assert html_to_markdown("   ") == ""
    md = html_to_markdown("<p>open <strong>bold<ul><li>one<li>two</ul>")
    assert "- one" in md and "- two" in md
    deep = "<div>" * 500 + "deep text" + "</div>" * 500
    assert html_to_markdown(deep) == "deep text"


def test_unclosed_dropped_element_drops_only_what_follows():
    md = html_to_markdown("<p>Keep</p><iframe><p>gone</p>")
    assert md == "Keep"
