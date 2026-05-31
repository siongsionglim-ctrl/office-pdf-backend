import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from typing import List
from pypdf import PdfReader, PdfWriter

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from io import BytesIO
from fastapi import Form

APP_NAME = "Office PDF Backend"
BASE_DIR = Path("/tmp/office_pdf_jobs")
MAX_FILE_SIZE_MB = 25
JOB_EXPIRE_SECONDS = 30 * 60

ALLOWED_EXTENSIONS = {
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx",
}

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_loop())


@app.get("/")
def root():
    return {
        "ok": True,
        "app": APP_NAME,
        "message": "Office PDF conversion backend is running.",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/convert/office-to-pdf")
async def convert_office_to_pdf(file: UploadFile = File(...)):
    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}",
        )

    job_id = uuid.uuid4().hex
    job_dir = BASE_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_input_name = f"input{ext}"
    input_path = input_dir / safe_input_name

    total_size = 0
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    try:
        with input_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total_size += len(chunk)
                if total_size > max_bytes:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max {MAX_FILE_SIZE_MB}MB.",
                    )

                buffer.write(chunk)

        pdf_path = convert_with_libreoffice(input_path, output_dir)

        if not pdf_path.exists():
            raise RuntimeError("PDF output not found after conversion.")

        meta_path = job_dir / "meta.txt"
        meta_path.write_text(str(time.time()), encoding="utf-8")

        return {
            "ok": True,
            "job_id": job_id,
            "download_url": f"/job/{job_id}/download",
            "expires_in_seconds": JOB_EXPIRE_SECONDS,
        }

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/pdf/merge")
async def merge_pdfs(files: List[UploadFile] = File(...)):
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Please upload at least 2 PDF files.")

    job_id = uuid.uuid4().hex
    job_dir = BASE_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        writer = PdfWriter()
        total_size = 0
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        for index, upload in enumerate(files):
            ext = Path(upload.filename or "").suffix.lower()
            if ext != ".pdf":
                raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

            input_path = input_dir / f"input_{index}.pdf"

            with input_path.open("wb") as buffer:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break

                    total_size += len(chunk)
                    if total_size > max_bytes:
                        shutil.rmtree(job_dir, ignore_errors=True)
                        raise HTTPException(
                            status_code=413,
                            detail=f"Total files too large. Max {MAX_FILE_SIZE_MB}MB.",
                        )

                    buffer.write(chunk)

            writer.append(str(input_path))

        output_path = output_dir / "merged.pdf"

        with output_path.open("wb") as f:
            writer.write(f)

        writer.close()

        (job_dir / "meta.txt").write_text(str(time.time()), encoding="utf-8")

        return {
            "ok": True,
            "job_id": job_id,
            "download_url": f"/job/{job_id}/download",
        }

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pdf/compress")
async def compress_pdf(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

    job_id = uuid.uuid4().hex
    job_dir = BASE_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = input_dir / "input.pdf"
    output_path = output_dir / "compressed.pdf"

    try:
        total_size = 0
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        with input_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total_size += len(chunk)
                if total_size > max_bytes:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max {MAX_FILE_SIZE_MB}MB.",
                    )

                buffer.write(chunk)

        reader = PdfReader(str(input_path))
        writer = PdfWriter()

        for page in reader.pages:
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)

        try:
            writer.compress_identical_objects(
                remove_duplicates=True,
                remove_unreferenced=True,
            )
        except Exception:
            pass

        with output_path.open("wb") as f:
            writer.write(f)

        writer.close()

        (job_dir / "meta.txt").write_text(str(time.time()), encoding="utf-8")

        original_size = input_path.stat().st_size
        compressed_size = output_path.stat().st_size

        return {
            "ok": True,
            "job_id": job_id,
            "download_url": f"/job/{job_id}/download",
            "original_size": original_size,
            "compressed_size": compressed_size,
        }

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


def convert_with_libreoffice(input_path: Path, output_dir: Path) -> Path:
    cmd = [
        "libreoffice",
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice conversion failed: {result.stderr or result.stdout}"
        )

    pdf_files = list(output_dir.glob("*.pdf"))
    if not pdf_files:
        raise RuntimeError(f"No PDF generated. Output: {result.stdout}")

    return pdf_files[0]


@app.get("/job/{job_id}/download")
def download_job(job_id: str):
    job_dir = BASE_DIR / job_id
    output_dir = job_dir / "output"

    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    pdf_files = list(output_dir.glob("*.pdf"))
    if not pdf_files:
        raise HTTPException(status_code=404, detail="PDF not found.")

    pdf_path = pdf_files[0]

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename="converted.pdf",
    )


@app.delete("/job/{job_id}")
def delete_job(job_id: str):
    job_dir = BASE_DIR / job_id

    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)

    return {"ok": True, "deleted": True}


async def cleanup_loop():
    while True:
        try:
            cleanup_expired_jobs()
        except Exception:
            pass

        await asyncio.sleep(180)


def cleanup_expired_jobs():
    now = time.time()

    for job_dir in BASE_DIR.iterdir():
        if not job_dir.is_dir():
            continue

        meta_path = job_dir / "meta.txt"

        try:
            if meta_path.exists():
                created_at = float(meta_path.read_text(encoding="utf-8"))
            else:
                created_at = job_dir.stat().st_mtime

            if now - created_at > JOB_EXPIRE_SECONDS:
                shutil.rmtree(job_dir, ignore_errors=True)

        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)

@app.post("/pdf/sign")
async def sign_pdf(
    file: UploadFile = File(...),
    signature: UploadFile = File(...),
    page_number: int = Form(1),
    x: float = Form(50),
    y: float = Form(50),
    width: float = Form(180),
    height: float = Form(80),
):
      if Path(file.filename or "").suffix.lower() != ".pdf":
          raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

      job_id = uuid.uuid4().hex
      job_dir = BASE_DIR / job_id
      input_dir = job_dir / "input"
      output_dir = job_dir / "output"

      input_dir.mkdir(parents=True, exist_ok=True)
      output_dir.mkdir(parents=True, exist_ok=True)

      input_pdf = input_dir / "input.pdf"
      signature_png = input_dir / "signature.png"
      output_pdf = output_dir / "signed.pdf"

      try:
          input_pdf.write_bytes(await file.read())
          signature_png.write_bytes(await signature.read())

          reader = PdfReader(str(input_pdf))
          writer = PdfWriter()

          first_page = reader.pages[0]
          page_width = float(first_page.mediabox.width)
          page_height = float(first_page.mediabox.height)

          packet = BytesIO()
          c = canvas.Canvas(packet, pagesize=(page_width, page_height))

          flutter_view_width = 390
          flutter_view_height = 650

          scale_x = page_width / flutter_view_width
          scale_y = page_height / flutter_view_height

          sig_width = max(20, min(width * scale_x, page_width))
          sig_height = max(10, min(height * scale_y, page_height))

          sig_x = max(0, min(x * scale_x, page_width - sig_width))
          sig_y = page_height - (y * scale_y) - sig_height
          sig_y = max(0, min(sig_y, page_height - sig_height))

          c.drawImage(
                ImageReader(str(signature_png)),
                sig_x,
                sig_y,
                width=sig_width,
                height=sig_height,
                mask="auto",
            )
          c.save()

          packet.seek(0)
          overlay_pdf = PdfReader(packet)
          overlay_page = overlay_pdf.pages[0]

          for index, page in enumerate(reader.pages):
              if index == page_number - 1:
                    page.merge_page(overlay_page)
              writer.add_page(page)

          with output_pdf.open("wb") as f:
              writer.write(f)

          writer.close()

          (job_dir / "meta.txt").write_text(str(time.time()), encoding="utf-8")

          return {
              "ok": True,
              "job_id": job_id,
              "download_url": f"/job/{job_id}/download",
          }

      except Exception as e:
          shutil.rmtree(job_dir, ignore_errors=True)
          raise HTTPException(status_code=500, detail=str(e))
