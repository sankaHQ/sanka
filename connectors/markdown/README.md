# sanka-migrate-connector-markdown

Reads a directory of Markdown files as a Sanka Migrate **source**: YAML
frontmatter becomes structured fields, the body becomes `content`, the
relative path is the identity. Inventory reports the frontmatter field union
with inferred types and warns on mixed-type fields and unparseable
frontmatter. Apache-2.0; depends only on `sanka-migrate-connector-sdk` (+ PyYAML).
