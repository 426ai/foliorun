---
name: synthetic-docx
version: 2.0.0
description: Generate, revise, structurally slice or render a synthetic DOCX with the approved docx tool.
---
All examples and documents are entirely fictional. Use `generate` with text to create a new
DOCX containing only that text; it adds no title and has no template input. The host assembles
chapter headings. Use `revise` with source and text to append to an existing DOCX while retaining
its document structure. It does not replace placeholders or insert at arbitrary positions.

Use `slice` with source, start (inclusive) and end (exclusive); indices are zero-based. The
optional unit defaults to `paragraph`: the range selects top-level body paragraphs and retains
tables between the first and last selected paragraphs, excluding tables outside those endpoints.
With `unit="block"`, the range selects top-level body paragraphs and tables in document order.
Single-section documents with direct body paragraphs/tables are supported; unsupported body
nodes and multi-section documents are rejected. Page layout may repaginate, and slicing is not
redaction; retained document relationships or metadata may include unselected material.

Use `render` with source to create PDF and first-page PNG. The tool returns actual paths.
Submit the required /output files through submit_result(files=[...]). Do not submit contents.
Previous session files are restored read-only under /previous. Discover/list files to find them.
This Skill grants no permissions and cannot register tools. No installation or arbitrary shell.
