"""PDF validation processor.

Attempts to open each PDF with pypdfium2 before it is stored. Files that
cannot be opened -- password-protected PDFs, truncated files, and other
unreadable documents -- raise
:class:`~soliplex.agents.common.processors.ProcessorRejected`, so the document
is never written and the caller only has to skip recording its URI.
"""

import io

import pypdfium2 as pdfium

from soliplex.agents.common.processors import FileProcessor
from soliplex.agents.common.processors import ProcessorRejected
from soliplex.agents.common.processors import register


@register("application/pdf")
class PdfValidator(FileProcessor):
    """Reject PDF files that cannot be opened without a password."""

    def process(self, data: bytes, mime_type: str) -> bytes:
        try:
            doc = pdfium.PdfDocument(io.BytesIO(data))
            doc.close()
        except pdfium.PdfiumError as e:
            raise ProcessorRejected(str(e)) from e
        return data
