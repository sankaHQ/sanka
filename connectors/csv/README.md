# CSV connector (bundled)

Reads a single CSV or TSV file as a Sanka Migrate **source**: the header
row defines the fields, every data row becomes one record, and the sanitized
file stem is the object key. The delimiter is sniffed, falling back to `,`
(tab for `.tsv`). Inventory infers number/boolean column types by sampling
every row and warns on mixed-type columns; record values themselves are
passed through as strings. A column named `id` (case-insensitive) is the
identity — otherwise a synthetic 1-based `row` field is exposed and used,
with a warning. Apache-2.0; depends only on the bundled `sanka.connector` interface (stdlib
`csv`).
