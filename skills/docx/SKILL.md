---
name: synthetic-docx
version: 1.0.0
description: Generate, revise, slice or render a synthetic DOCX with the approved docx tool.
---
All examples and documents are entirely fictional. Use docx_tool with operation generate
and text to create a file. Use revise with source and text to append to an existing DOCX.
Use slice with source, start (inclusive) and end (exclusive) for paragraph-only slicing.
Use render with source to create PDF and first-page PNG. The tool returns actual paths.
Submit the required /output files through submit_result(files=[...]). Do not submit contents.
Previous session files are restored read-only under /previous. Discover/list files to find them.
This Skill grants no permissions and cannot register tools. No installation or arbitrary shell.
