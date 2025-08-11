#!/usr/bin/env python3
"""
Stark Print Server
- Flask upload endpoint
- FIFO queue with worker threads
- LibreOffice headless conversion for docx/pptx/xlsx -> PDF
- Print via lp (CUPS)
- Optional LAN-only restrictions via ALLOWED_NETWORKS (CIDR)
"""

import os
import io
import sys
import time
import shutil
import logging
import tempfile
import threading
import queue
import subprocess
from datetime import datetime
from ipaddress import ip_network, ip_address

from flask import Flask, request, jsonify, abort
from werkzeug.utils import secure_filename

# ----------------------
# Configuration
# ----------------------
UPLOAD_FOLDER = "/var/lib/stark/uploads"       # must be writable by service user
WORK_FOLDER = "/var/lib/stark/work"            # temp working folder
LOG_FILE = "/var/log/stark_server.log"

SERVER_HOST = "0.0.0.0"                        # listen address (0.0.0.0 ok for LAN)
SERVER_PORT = 5000

# Allowed networks (CIDR) - only these client IPs can upload.
# Change to your LAN ranges, e.g. ["192.168.1.0/24", "10.0.0.0/24"]
ALLOWED_NETWORKS = ["192.168.0.0/16", "10.0.0.0/8"]

# printing options
PRINTER_NAME = None   # e.g. "HP_LaserJet" or None to use system default

# limits & behaviour
ALLOWED_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx"}
MAX_FILE_SIZE_MB = 50        # reject uploads bigger than this
WORKER_COUNT = 1             # number of worker threads (1 enforces strict FIFO)
CONVERT_TIMEOUT = 60         # seconds allowed for libreoffice conversion
PRINT_TIMEOUT = 30           # seconds allowed for lp command

# ----------------------
# Setup
# ----------------------
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(WORK_FOLDER, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("stark")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE_MB * 1024 * 1024

# job queue and history
job_queue = queue.Queue()
job_history = []  # in-memory short history (could be persisted)



# ----------------------
# Utility helpers
# ----------------------
def ip_allowed(remote_addr):
    """Check if remote_addr (string) is inside ALLOWED_NETWORKS."""
    try:
        ip = ip_address(remote_addr)
    except Exception:
        return False
    for net in ALLOWED_NETWORKS:
        try:
            if ip in ip_network(net):
                return True
        except Exception:
            continue
    return False

def allowed_file(filename):
    ext = filename.rsplit(".", 1)
    return len(ext) == 2 and ext[1].lower() in ALLOWED_EXTENSIONS

def run_subprocess(cmd, timeout=None):
    """Run subprocess and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return proc.returncode, proc.stdout.decode(errors='ignore'), proc.stderr.decode(errors='ignore')
    except subprocess.TimeoutExpired as e:
        return -1, "", f"Timeout expired: {e}"
    except Exception as e:
        return -1, "", str(e)

def convert_to_pdf(input_path, out_dir):
    """
    Convert docx/pptx/xlsx to PDF using libreoffice headless.
    Returns path to PDF on success, or raises Exception.
    """
    # libreoffice converts based on working dir and outputs with same base name
    cmd = [
        "libreoffice", "--headless", "--invisible",
        "--convert-to", "pdf", "--outdir", out_dir, input_path
    ]
    code, out, err = run_subprocess(cmd, timeout=CONVERT_TIMEOUT)
    if code != 0:
        raise RuntimeError(f"LibreOffice failed: {err or out}")
    base = os.path.splitext(os.path.basename(input_path))[0]
    pdf_path = os.path.join(out_dir, base + ".pdf")
    if not os.path.exists(pdf_path):
        raise RuntimeError(f"Conversion did not produce PDF: expected {pdf_path}")
    return pdf_path

def print_pdf(pdf_path):
    """Send PDF to printer with lp. Return (success_bool, message)."""
    cmd = ["lp"]
    if PRINTER_NAME:
        cmd += ["-d", PRINTER_NAME]
    cmd += [pdf_path]
    code, out, err = run_subprocess(cmd, timeout=PRINT_TIMEOUT)
    if code != 0:
        return False, err or out
    return True, out.strip()

def record_history(entry):
    """Keep a small rotating history in memory."""
    job_history.append(entry)
    # cap history to last 200 entries
    if len(job_history) > 200:
        del job_history[0]

# ----------------------
# Worker thread
# ----------------------
def worker_loop(worker_id):
    logger.info(f"Worker {worker_id} started.")
    while True:
        job = job_queue.get()
        if job is None:
            logger.info(f"Worker {worker_id} received shutdown signal.")
            break
        try:
            logger.info(f"Worker {worker_id} processing job {job['id']} file={job['filename']}")
            record_history({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "job_id": job['id'],
                "filename": job['filename'],
                "status": "processing",
                "client": job.get("client"),
            })

            # create isolated temp dir for conversion
            with tempfile.TemporaryDirectory(dir=WORK_FOLDER) as tmpdir:
                # copy input file to tmpdir to avoid issues with mount permissions
                tmp_input = os.path.join(tmpdir, secure_filename(job['filename']))
                shutil.copy(job['filepath'], tmp_input)

                ext = job['filename'].rsplit(".", 1)[1].lower()
                if ext == "pdf":
                    pdf_path = tmp_input
                else:
                    # convert other office files to pdf
                    try:
                        pdf_path = convert_to_pdf(tmp_input, tmpdir)
                    except Exception as e:
                        logger.exception("Conversion failed")
                        record_history({
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                            "job_id": job['id'],
                            "filename": job['filename'],
                            "status": "conversion_failed",
                            "error": str(e)
                        })
                        continue

                # print
                ok, msg = print_pdf(pdf_path)
                if ok:
                    logger.info(f"Job {job['id']} printed successfully.")
                    record_history({
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "job_id": job['id'],
                        "filename": job['filename'],
                        "status": "printed",
                        "printer_response": msg
                    })
                else:
                    logger.error(f"Job {job['id']} print failed: {msg}")
                    record_history({
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "job_id": job['id'],
                        "filename": job['filename'],
                        "status": "print_failed",
                        "error": msg
                    })

        except Exception as e:
            logger.exception("Unhandled exception in worker")
        finally:
            # cleanup uploaded file (safe removal)
            try:
                os.remove(job['filepath'])
            except Exception:
                pass
            job_queue.task_done()

# Start worker threads
for i in range(WORKER_COUNT):
    t = threading.Thread(target=worker_loop, args=(i+1,), daemon=True)
    t.start()

# ----------------------
# Flask endpoints
# ----------------------
@app.before_request
def restrict_remote():
    # reject if remote addr not in allowed networks
    remote = request.remote_addr
    if not ip_allowed(remote):
        logger.warning(f"Denied connection from {remote}")
        abort(403, description="Forbidden")

@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept multipart form with 'file' field.
    Returns JSON with job id and status.
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files['file']
    if f.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(f.filename)
    if not allowed_file(filename):
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"}), 400

    # size already limited by MAX_CONTENT_LENGTH but double-check
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > app.config['MAX_CONTENT_LENGTH']:
        return jsonify({"error": f"File too large (> {MAX_FILE_SIZE_MB} MB)"}), 413

    # save to upload folder under unique name
    timestamp = int(time.time() * 1000)
    unique_name = f"{timestamp}_{filename}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    try:
        f.save(save_path)
    except Exception as e:
        logger.exception("Failed to save uploaded file")
        return jsonify({"error": "Failed to save file"}), 500

    # push job to queue
    job_id = f"job-{timestamp}"
    job = {
        "id": job_id,
        "filename": filename,
        "filepath": save_path,
        "client": request.remote_addr,
        "received_at": datetime.utcnow().isoformat() + "Z"
    }
    job_queue.put(job)
    record_history({
        "timestamp": job['received_at'],
        "job_id": job_id,
        "filename": filename,
        "status": "queued",
        "client": job['client']
    })
    logger.info(f"Queued job {job_id} from {job['client']} -> {filename}")

    return jsonify({"status": "queued", "job_id": job_id}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "queue_size": job_queue.qsize()}), 200

@app.route("/history", methods=["GET"])
def history():
    # return last N history entries
    return jsonify({"history": job_history[-100:]}), 200

# ----------------------
# Graceful shutdown handling (optional)
# ----------------------
def shutdown_workers():
    logger.info("Shutting down workers...")
    for _ in range(WORKER_COUNT):
        job_queue.put(None)  # sentinel

if __name__ == "__main__":
    try:
        logger.info("Starting Stark server...")
        app.run(host=SERVER_HOST, port=SERVER_PORT)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        shutdown_workers()
        logger.info("Server exiting.")
