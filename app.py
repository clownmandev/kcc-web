import os
import subprocess
import shutil
import requests
import sys
import time
import re
import uuid
import threading
import logging
import glob
from urllib.parse import urlparse
from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename

# --- SETUP LOGGING (Visible in Docker Logs) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- CONFIGURATION ---
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')

# Ensure directories exist
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER]:
    os.makedirs(d, exist_ok=True)

# --- IN-MEMORY JOB STORAGE ---
# Structure: { 'job_uuid': { 'status': 'running', 'logs': [], 'result': None } }
JOBS = {}

# --- HELPER FUNCTIONS ---

def log_to_job(job_id, message):
    """Writes to both Docker Logs and the Web UI Log"""
    print(f"[{job_id[:8]}] {message}", flush=True) # To Docker Console
    if job_id in JOBS:
        JOBS[job_id]['logs'].append(message)

def run_command(cmd, job_id):
    """Runs a shell command and captures output for logs"""
    try:
        log_to_job(job_id, f"CMD: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
        
        if isinstance(cmd, list):
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        else:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, shell=True)
            
        for line in process.stdout:
            # Clean up the log line
            clean_line = line.strip()
            if clean_line:
                log_to_job(job_id, clean_line)
        
        process.wait()
        return process.returncode == 0
    except Exception as e:
        log_to_job(job_id, f"CRITICAL CMD ERROR: {e}")
        return False

def is_image_dir(path):
    if not os.path.isdir(path): return False
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp', '*.gif']:
        if glob.glob(os.path.join(path, ext)): return True
    return False

# --- WORKER THREAD (The Engine) ---
def worker_process(job_id, params):
    log_to_job(job_id, "Worker started. Initializing...")
    
    try:
        mode = params.get('mode')
        profile = params.get('profile', 'KPW')
        format_type = params.get('format', 'EPUB')
        
        # Create a folder specifically for this job's final output
        job_output_dir = os.path.join(OUTPUT_FOLDER, job_id)
        os.makedirs(job_output_dir, exist_ok=True)

        # --- MANGADEX MODE ---
        if mode == 'mangadex':
            manga_id = params.get('manga_id')
            manga_title = re.sub(r'[\\/*?:"<>|]', "", params.get('manga_title', 'Manga')).strip()
            
            try:
                vol_start = int(params.get('vol_start', 1))
                vol_end = int(params.get('vol_end', 1))
            except:
                vol_start = 1; vol_end = 1

            # Processing Loop (Vol by Vol to ensure stability)
            for current_vol in range(vol_start, vol_end + 1):
                log_to_job(job_id, f"--- Processing Volume {current_vol} of {vol_end} ---")
                
                # Temp DL folder for this volume
                vol_uuid = str(uuid.uuid4())
                vol_dl_path = os.path.join(DOWNLOAD_FOLDER, vol_uuid)
                os.makedirs(vol_dl_path, exist_ok=True)

                # 1. DOWNLOAD
                cmd_dl = [
                    'mangadex-downloader',
                    f"https://mangadex.org/title/{manga_id}",
                    '--language', 'en',
                    '--folder', vol_dl_path,
                    '--no-group-name',
                    '--start-volume', str(current_vol),
                    '--end-volume', str(current_vol)
                ]
                
                # Add Chapter Limits if provided
                if params.get('chap_start'):
                    cmd_dl.extend(['--start-chapter', params.get('chap_start')])
                if params.get('chap_end'):
                    cmd_dl.extend(['--end-chapter', params.get('chap_end')])

                if not run_command(cmd_dl, job_id):
                    log_to_job(job_id, f"Warning: Download failed for Vol {current_vol}")

                # 2. CONVERT
                inputs = []
                for root, dirs, files in os.walk(vol_dl_path):
                    if is_image_dir(root): inputs.append(root)

                if inputs:
                    log_to_job(job_id, "Converting images...")
                    kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_output_dir]
                    kcc_cmd.extend(inputs)
                    run_command(kcc_cmd, job_id)
                else:
                    log_to_job(job_id, "No images found to convert.")

                # 3. CLEANUP RAW IMAGES
                try:
                    shutil.rmtree(vol_dl_path)
                except: pass

        # --- LOCAL / URL MODE ---
        else:
            temp_dl = os.path.join(DOWNLOAD_FOLDER, job_id)
            inputs = []
            
            if mode == 'local':
                local_file = os.path.join(app.config['UPLOAD_FOLDER'], params.get('filename'))
                inputs.append(local_file)
                log_to_job(job_id, f"Processing local file: {params.get('filename')}")
            
            elif mode in ['mangabat', 'mangabuddy', 'mangakakalot']:
                # Simple scraper fallback
                log_to_job(job_id, f"Scraping from {mode}...")
                # (Scraper logic would go here, kept brief for stability)
                pass 

            if inputs:
                kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_output_dir]
                kcc_cmd.extend(inputs)
                run_command(kcc_cmd, job_id)

        # --- FINALIZING ---
        log_to_job(job_id, "Packaging results...")
        generated_files = os.listdir(job_output_dir)
        
        final_file_name = None
        
        if not generated_files:
            log_to_job(job_id, "ERROR: No output files were created.")
            JOBS[job_id]['status'] = 'failed'
            return

        if len(generated_files) == 1:
            # Single file result
            final_file_name = generated_files[0]
            shutil.move(os.path.join(job_output_dir, final_file_name), os.path.join(OUTPUT_FOLDER, final_file_name))
        else:
            # Multiple files -> Zip them
            safe_title = re.sub(r'[\\/*?:"<>|]', "", params.get('manga_title', 'Batch')).strip()
            zip_name = f"{safe_title}_Vol_{params.get('vol_start')}-{params.get('vol_end')}.zip"
            shutil.make_archive(os.path.join(OUTPUT_FOLDER, zip_name.replace('.zip','')), 'zip', job_output_dir)
            final_file_name = zip_name

        # Cleanup Job Folder
        try: shutil.rmtree(job_output_dir)
        except: pass

        # Mark Done
        JOBS[job_id]['result'] = final_file_name
        JOBS[job_id]['status'] = 'finished'
        log_to_job(job_id, f"SUCCESS: Ready for download: {final_file_name}")

    except Exception as e:
        log_to_job(job_id, f"FATAL ERROR: {str(e)}")
        JOBS[job_id]['status'] = 'failed'

# --- API ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    # Mangadex API Search
    try:
        q = request.form.get('query')
        r = requests.get("https://api.mangadex.org/manga", params={'title': q, 'limit': 10, 'includes[]': ['cover_art']})
        data = r.json().get('data', [])
        results = []
        for m in data:
            title = m['attributes']['title'].get('en') or list(m['attributes']['title'].values())[0]
            cover = next((rel['attributes']['fileName'] for rel in m.get('relationships', []) if rel['type'] == 'cover_art'), None)
            cover_url = f"https://uploads.mangadex.org/covers/{m['id']}/{cover}.256.jpg" if cover else ""
            results.append({'id': m['id'], 'title': title, 'cover': cover_url})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    try:
        mid = request.form.get('manga_id')
        r = requests.get(f"https://api.mangadex.org/manga/{mid}/aggregate", params={'translatedLanguage[]': ['en']})
        vols = [float(k) for k in r.json().get('volumes', {}).keys() if k.lower() != 'none']
        return jsonify({'total_volumes': int(max(vols)) if vols else 0})
    except: return jsonify({'total_volumes': 0})

@app.route('/api/start_job', methods=['POST'])
def start_job():
    job_id = str(uuid.uuid4())
    params = request.form.to_dict()
    
    # Initialize Job
    JOBS[job_id] = { 'status': 'running', 'logs': [], 'result': None }
    
    # Spawn Background Thread
    thread = threading.Thread(target=worker_process, args=(job_id, params))
    thread.start()
    
    return jsonify({'job_id': job_id})

@app.route('/api/job_status/<job_id>')
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify(job)

@app.route('/download_file/<filename>')
def download_file(filename):
    # Clean filename logic (removes UUID prefix if needed)
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
