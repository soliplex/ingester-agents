"""PDF validation processor.

Attempts to open each PDF with pypdfium2 before it is committed to the
download directory. Files that cannot be opened — password-protected PDFs,
truncated files, and other unreadable documents — raise
:class:`~soliplex.agents.common.processors.ProcessorRejected` so the caller
can remove the file and its sidecar and skip recording the URI in local state.
"""

from pathlib import Path

import pypdfium2 as pdfium

from soliplex.agents.common.processors import FileProcessor
from soliplex.agents.common.processors import ProcessorRejected
from soliplex.agents.common.processors import register


@register("application/pdf")
class PdfValidator(FileProcessor):
    """Reject PDF files that cannot be opened without a password."""

    def process(self, path: Path, mime_type: str) -> None:
        try:
            doc = pdfium.PdfDocument(path)
            doc.close()
        except pdfium.PdfiumError as e:
            raise ProcessorRejected(str(e)) from e
