# Synthetic PDFs

No PDF is checked in. `tests/unit/conftest_pdf.py::make_pdf` builds small fictional PDFs
(Helvetica text, one block per page) in each test's temporary directory. Tests that need the real
`pdftotext` are skipped when poppler is not installed; the extraction logic itself is exercised
through an injected runner that returns the page texts.
