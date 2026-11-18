# Image asset inventory and review boundary

The synthetic Skill and generic document entrypoint were authored for this package; no
Anthropic Skill or product splitter is included. No public distribution license for
project-owned code is asserted. Local build only; publication requires owner review.

Python parent: Docker official Python 3.12.16 slim trixie, pinned OCI index in Dockerfile.
Debian snapshot: 20261115T000000Z, direct package versions in os-packages.lock.
LibreOffice, pandoc, poppler, fonts: Debian distributions; copyright/license texts remain
under /usr/share/doc and parent Python licenses remain in the image. Python wheels:
python-docx (MIT), lxml (BSD plus bundled library notices), typing-extensions (PSF).
Exact wheel bytes are hash locked. Transitive Debian versions are captured in the image.

The inventories are not a full SBOM, source-offer audit or legal acceptance. Public
redistribution, corresponding source obligations, transitive notices, vulnerability
review, signatures and complete SBOM remain release gates. This task does not publish.
