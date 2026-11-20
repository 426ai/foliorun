# Tool image built only from the audited package wheel; no project data, no credentials, no network installs.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
ARG WHEEL
COPY ${WHEEL} /tmp/wheel/
RUN python -m pip install --no-deps --no-index --no-compile /tmp/wheel/*.whl \
 && rm -rf /tmp/wheel \
 && python -I -c "import tender_harness.docx_ops.engine" \
 && find /usr/local/lib/python3.12/site-packages/tender_harness -name __pycache__ -prune -exec rm -rf {} + \
 && chmod -R a-w /usr/local/lib/python3.12/site-packages/tender_harness \
 && find /usr/local/lib/python3.12/site-packages/tender_harness -type f -name '*.py' -exec chmod 0444 {} +
