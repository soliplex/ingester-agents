# PDF fixtures

Real PDFs for the `check_pdf_password` pre-process step and the
`soliplex-pdf-metadata` haiku-rag metadata provider, so their tests exercise
pdfium's actual error codes, security-handler detection, page counting and
information-dictionary reading rather than mocks.

| File | What it is | Expected outcome |
| --- | --- | --- |
| `valid.pdf` | one blank page, unencrypted | CONTINUE |
| `user_password.pdf` | AES-256, user password `user` | SKIP, `password protected` |
| `owner_only.pdf` | AES-256, owner password only, printing disallowed | CONTINUE with a message (SKIP with `skip_owner_restricted`) |
| `truncated.pdf` | the first 100 bytes of `valid.pdf` | SKIP, `unreadable PDF: ...` |
| `with_metadata.pdf` | three blank pages, PDF 1.4, every information entry set | `page_count` 3 and all `pdf_*` keys |
| `with_attachments.pdf` | one page embedding `inner report.pdf` (two pages, itself embedding `deep notes.txt`) and `readme.txt` | attachments back-filled from the parent, one level of nesting |

## Regenerating

`valid.pdf` and `truncated.pdf` come from pypdfium2:

```python
import io
import pypdfium2 as pdfium

pdf = pdfium.PdfDocument.new()
pdf.new_page(200, 200)
buffer = io.BytesIO()
pdf.save(buffer)
open("valid.pdf", "wb").write(buffer.getvalue())
open("truncated.pdf", "wb").write(buffer.getvalue()[:100])
```

`with_metadata.pdf` is written by hand, since pypdfium2 cannot set the
information dictionary:

```python
info = (
    "<< /Title (Quarterly Report) /Author (Jane Doe) /Subject (Finance)"
    " /Keywords (budget, forecast) /Creator (Writer) /Producer (LibreOffice 7.6)"
    " /CreationDate (D:20240115093000-05'00') /ModDate (D:20240220) >>"
)
objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    info,
]
out = bytearray(b"%PDF-1.4\n")
offsets = []
for number, body in enumerate(objects, 1):
    offsets.append(len(out))
    out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
xref = len(out)
out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
for offset in offsets:
    out += f"{offset:010d} 00000 n \n".encode()
out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
open("with_metadata.pdf", "wb").write(out)
```

`with_attachments.pdf` comes from pypdfium2's attachment API (pdfium stamps
a creation date, so a regenerated file differs in bytes, not in content):

```python
import io
import pypdfium2 as pdfium


def with_attachments(pages, attachments):
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 200)
    for name, data in attachments.items():
        pdf.new_attachment(name).set_data(data)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


inner = with_attachments(2, {"deep notes.txt": b"nested attachment\n"})
outer = with_attachments(1, {"inner report.pdf": inner, "readme.txt": b"attached text\n"})
open("with_attachments.pdf", "wb").write(outer)
```

The encrypted files come from qpdf 11.9.1, run in a container from this
directory:

```bash
podman run --rm --cgroups=disabled -v "$PWD:/w" -w /w docker.io/library/alpine:3.20 sh -c '
  apk add --no-cache qpdf >/dev/null &&
  qpdf --encrypt user owner 256 -- valid.pdf user_password.pdf &&
  qpdf --encrypt "" owner 256 --print=none -- valid.pdf owner_only.pdf'
```
