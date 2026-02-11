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
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp', '*.gif']:
        if glob.glob(os.path.join(path, ext)): return True
    return False

def worker_process(job_id, params):
    try:
        mode = params.get('mode')
        profile = params.get('profile', 'KPW')
        format_type = params.get('format', 'EPUB')
        
        job_output_dir = os.path.join(OUTPUT_FOLDER, job_id)
        os.makedirs(job_output_dir, exist_ok=True)

        # --- UNIFIED DOWNLOADER LOGIC ---
        if mode in ['mangadex', 'scraper']:
            url = params.get('url') if mode == 'scraper' else f"https://mangadex.org/title/{params.get('manga_id')}"
            v_start = int(params.get('vol_start', 1))
            v_end = int(params.get('vol_end', 1))

            for v in range(v_start, v_end + 1):
                log_to_job(job_id, f"--- Processing Volume {v} ---")
                v_path = os.path.join(DOWNLOAD_FOLDER, str(uuid.uuid4()))
                os.makedirs(v_path, exist_ok=True)
                
                dl_cmd = ['mangadex-downloader', url, '--language', 'en', '--folder', v_path, '--no-group-name', '--start-volume', str(v), '--end-volume', str(v)]
                if params.get('chap_start'): dl_cmd.extend(['--start-chapter', params.get('chap_start')])
                if params.get('chap_end'): dl_cmd.extend(['--end-chapter', params.get('chap_end')])
                
                run_command(dl_cmd, job_id)
                
                # Find image folders and convert
                inputs = [root for root, d, f in os.walk(v_path) if is_image_dir(root)]
                if inputs:
                    log_to_job(job_id, f"Converting Volume {v} to {format_type}...")
                    k_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_output_dir]
                    k_cmd.extend(inputs)
                    run_command(k_cmd, job_id)
                shutil.rmtree(v_path)

        elif mode == 'local':
            file_path = os.path.join(UPLOAD_FOLDER, params.get('filename'))
            run_command(['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_output_dir, file_path], job_id)

        # --- FINAL PACKAGING ---
        files = os.listdir(job_output_dir)
        if not files:
            log_to_job(job_id, "FAILED: No output generated."); JOBS[job_id]['status'] = 'failed'; return

        if len(files) == 1:
            fname = files[0]
            shutil.move(os.path.join(job_output_dir, fname), os.path.join(OUTPUT_FOLDER, fname))
            JOBS[job_id]['result'] = fname
        else:
            zip_name = f"Batch_{params.get('manga_title', 'Manga')}_{job_id[:4]}.zip"
            shutil.make_archive(os.path.join(OUTPUT_FOLDER, zip_name.replace('.zip','')), 'zip', job_output_dir)
            JOBS[job_id]['result'] = zip_name

        shutil.rmtree(job_output_dir)
        JOBS[job_id]['status'] = 'finished'
    except Exception as e:
        log_to_job(job_id, f"ERROR: {str(e)}"); JOBS[job_id]['status'] = 'failed'

@app.route('/')
def index(): return render_template('index.html')

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

@app.route('/api/upload', methods=['POST'])
def upload():
    f = request.files['file']
    fname = secure_filename(f.filename)
    f.save(os.path.join(UPLOAD_FOLDER, fname))
    return jsonify({'filename': fname})

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
