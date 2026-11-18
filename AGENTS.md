# Foliorun Working Agreements

- Foliorun is an independent Python document execution framework. Hosts own
  authorization, budgets, storage bindings and deployment.
- Use entirely fictional synthetic inputs in tests, examples and Skills. Never
  commit credentials, authenticated URLs, personal or business data, raw model
  messages, workflow histories, traces or local verification output.
- Do not depend on product packages, monorepo layout or developer machine paths.
- Do not automatically push, tag, publish packages, create remotes or deploy.
  Use the existing local Git identity; do not invent author or license metadata.
- Paid models, Temporal services and OpenSandbox instances require explicit
  separate authorization. Offline tests do not establish live acceptance.
- Keep audit records and packaging verification reports outside the repository.
- Preserve unrelated edits. Run the locked offline lint/tests and packaging
  checks documented in README.md before committing Python changes.
- Image builds are separate from package validation. Do not claim a built or
  published image unless that operation was explicitly authorized and verified.
