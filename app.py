import os, subprocess, shutil, requests, sys, time, re, uuid, threading, logging, glob
from urllib.parse import urlparse
from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
app = Flask(__name__)

BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER]:
    os.makedirs(d, exist_ok=True)

JOBS = {}

def log_to_job(job_id, message):
    print(f"[{job_id[:8]}] {message}", flush=True)
    if job_id in JOBS: JOBS[job_id]['logs'].append(message)

def run_command(cmd, job_id):
    log_to_job(job_id, f"RUNNING: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in process.stdout:
        if line.strip(): log_to_job(job_id, line.strip())
    process.wait()
    return process.returncode == 0

def is_image_dir(path):
    for ext in ['*.jpg', ['*.jpeg'], '*.png', '*.webp', '*.gif']:
        if glob.glob(os.path.join(path, ext)): return True
    return False

def worker_process(job_id, params):
    try:
        mode = params.get('mode')
        profile = params.get('profile', 'KPW')
        format_type = params.get('format', 'EPUB').upper()
        m_title = re.sub(r'[\\/*?:"<>|]', "", params.get('manga_title', 'Manga')).strip()
        
        # This is where we will collect ALL images for the final book
        master_collection_path = os.path.join(DOWNLOAD_FOLDER, f"full_{job_id}")
        os.makedirs(master_collection_path, exist_ok=True)

        if mode in ['mangadex', 'scraper']:
            url = params.get('url') if mode == 'scraper' else f"https://mangadex.org/title/{params.get('manga_id')}"
            v_start = int(params.get('vol_start', 1))
            v_end = int(params.get('vol_end', 1))

            log_to_job(job_id, f"--- Starting Full Export: {m_title} ---")

            for v in range(v_start, v_end + 1):
                log_to_job(job_id, f"Downloading Volume {v}...")
                # Download into the master collection folder
                dl_cmd = ['mangadex-downloader', url, '--language', 'en', '--folder', master_collection_path, '--no-group-name', '--start-volume', str(v), '--end-volume', str(v)]
                if params.get('chap_start'): dl_cmd.extend(['--start-chapter', params.get('chap_start')])
                if params.get('chap_end'): dl_cmd.extend(['--end-chapter', params.get('chap_end')])
                
                run_command(dl_cmd, job_id)

        elif mode == 'local':
            file_path = os.path.join(UPLOAD_FOLDER, params.get('filename'))
            shutil.copy(file_path, master_collection_path)

        # --- CONVERT EVERYTHING INTO ONE FILE ---
        log_to_job(job_id, "Merging all chapters into a single EPUB. This may take a while...")
        
        # Temp dir for KCC output
        job_work_dir = os.path.join(OUTPUT_FOLDER, job_id)
        os.makedirs(job_work_dir, exist_ok=True)

        # Run KCC on the WHOLE folder at once
        k_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_work_dir, master_collection_path]
        run_command(k_cmd, job_id)

        # Cleanup raw images to save disk
        shutil.rmtree(master_collection_path)

        # --- FIND THE SINGLE BOOK ---
        found_books = []
        for root, dirs, files in os.walk(job_work_dir):
            for f in files:
                if f.lower().endswith(('.epub', '.mobi', '.azw3')):
                    found_books.append(os.path.join(root, f))

        if not found_books:
            log_to_job(job_id, "FAILED: KCC did not produce a file."); JOBS[job_id]['status'] = 'failed'; return

        # Final rename: Clean Manga Title
        final_filename = f"{m_title}.{format_type.lower()}"
        final_path = os.path.join(OUTPUT_FOLDER, final_filename)
        
        # Move the first found book (there should only be one now)
        shutil.move(found_books[0], final_path)
        JOBS[job_id]['result'] = final_filename

        shutil.rmtree(job_work_dir)
        JOBS[job_id]['status'] = 'finished'
        log_to_job(job_id, f"SUCCESS: Single file created: {final_filename}")

    except Exception as e:
        log_to_job(job_id, f"ERROR: {str(e)}"); JOBS[job_id]['status'] = 'failed'

# (Rest of routes: /api/search, /api/manga_details, /api/start_job, etc. stay the same as v4.1)
@app.route('/api/search', methods=['POST'])
def search_manga():
    r = requests.get("https://api.mangadex.org/manga", params={'title': request.form.get('query'), 'limit': 10, 'includes[]': ['cover_art']})
    results = []
    for m in r.json().get('data', []):
        t = m['attributes']['title'].get('en') or list(m['attributes']['title'].values())[0]
        cv = next((rel['attributes']['fileName'] for rel in m.get('relationships', []) if rel['type'] == 'cover_art'), None)
        results.append({'id': m['id'], 'title': t, 'cover': f"https://uploads.mangadex.org/covers/{m['id']}/{cv}.256.jpg" if cv else ""})
    return jsonify(results)

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    mid = request.form.get('manga_id')
    r = requests.get(f"https://api.mangadex.org/manga/{mid}/aggregate", params={'translatedLanguage[]': ['en']})
    data = r.json()
    vols = [float(k) for k in data.get('volumes', {}).keys() if k.lower() != 'none']
    total_ch = sum(len(v.get('chapters', {})) for v in data.get('volumes', {}).values())
    return jsonify({'total_volumes': int(max(vols)) if vols else 0, 'total_chapters': total_ch})

@app.route('/api/start_job', methods=['POST'])
def start_job():
    jid = str(uuid.uuid4())
    JOBS[jid] = {'status': 'running', 'logs': [], 'result': None}
    threading.Thread(target=worker_process, args=(jid, request.form.to_dict())).start()
    return jsonify({'job_id': jid})

@app.route('/api/job_status/<jid>')
def job_status(jid): return jsonify(JOBS.get(jid, {'status': 'not_found'}))

@app.route('/download_file/<filename>')
def download_file(filename): return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__': app.run(host='0.0.0.0', port=5000)
