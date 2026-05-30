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