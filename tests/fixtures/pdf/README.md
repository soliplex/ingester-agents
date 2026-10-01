# PDF fixtures

Real PDFs for the `check_pdf_password` pre-process step, so its tests exercise
pdfium's actual error codes and security-handler detection rather than mocks.

| File | What it is | Expected outcome |
| --- | --- | --- |
| `valid.pdf` | one blank page, unencrypted | CONTINUE |
| `user_password.pdf` | AES-256, user password `user` | SKIP, `password protected` |
| `owner_only.pdf` | AES-256, owner password only, printing disallowed | CONTINUE with a message (SKIP with `skip_owner_restricted`) |
| `truncated.pdf` | the first 100 bytes of `valid.pdf` | SKIP, `unreadable PDF: ...` |

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

The encrypted files come from qpdf 11.9.1, run in a container from this
directory:

```bash
podman run --rm --cgroups=disabled -v "$PWD:/w" -w /w docker.io/library/alpine:3.20 sh -c '
  apk add --no-cache qpdf >/dev/null &&
  qpdf --encrypt user owner 256 -- valid.pdf user_password.pdf &&
  qpdf --encrypt "" owner 256 --print=none -- valid.pdf owner_only.pdf'
```
