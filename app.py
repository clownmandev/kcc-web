import os
import subprocess
import shutil
import requests
import sys
import time
import re
import zipfile
import glob
import uuid
import threading
from urllib.parse import urlparse
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)
job_lock = threading.Lock()

# --- CONFIGURATION ---
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_temp_zips')
COMBINE_DIR = os.path.join(BASE_DIR, 'kcc_combined')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER, ZIP_TEMP, COMBINE_DIR]:
    os.makedirs(d, exist_ok=True)

# --- HELPER FUNCTIONS ---
def run_command_with_retry(cmd, max_retries=3):
    attempt = 0
    while attempt < max_retries:
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                yield line
            process.wait()
            if process.returncode == 0: return
            yield f"WARNING: Process failed code {process.returncode}. Retrying...\n"
        except Exception as e:
            yield f"ERROR: Execution failed: {str(e)}\n"
        attempt += 1
        time.sleep(2)
    yield "FAILURE: Max retries reached.\n"

def is_image_dir(path):
    if not os.path.isdir(path): return False
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp', '*.gif']:
        if glob.glob(os.path.join(path, ext)): return True
    return False

def scrape_website_images(url, save_folder):
    domain = urlparse(url).netloc
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': f"https://{domain}/"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        img_urls = re.findall(r'(?:src|data-src)="([^"]+?\.(?:jpg|jpeg|png|webp))"', response.text)
        img_urls = list(dict.fromkeys(img_urls))
        if not img_urls: yield "LOG: No images found.\n"; return
        yield f"LOG: Found {len(img_urls)} images.\n"
        for i, img_url in enumerate(img_urls):
            try:
                if not img_url.startswith('http'): continue
                with requests.get(img_url, headers=headers, stream=True, timeout=10) as r:
                    with open(os.path.join(save_folder, f"page_{i:04d}.jpg"), 'wb') as f:
                        for chunk in r.iter_content(1024*1024): f.write(chunk)
                if i % 10 == 0: yield f"LOG: DL Page {i+1}\n"
            except: pass
    except Exception as e: yield f"ERROR: {e}\n"

# --- API ROUTES ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    # (Same search logic as before)
    try:
        r = requests.get("https://api.mangadex.org/manga", params={'title': request.form.get('query'), 'limit': 10, 'includes[]': ['cover_art']})
        results = []
        for m in r.json().get('data', []):
            title = m['attributes']['title'].get('en') or list(m['attributes']['title'].values())[0]
            cover = next((rel['attributes']['fileName'] for rel in m.get('relationships', []) if rel['type'] == 'cover_art'), None)
            cover_url = f"https://uploads.mangadex.org/covers/{m['id']}/{cover}.256.jpg" if cover else ""
            results.append({'id': m['id'], 'title': title, 'desc': m['attributes']['description'].get('en', '')[:100], 'cover': cover_url})
        return jsonify(results)
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    try:
        r = requests.get(f"https://api.mangadex.org/manga/{request.form.get('manga_id')}/aggregate", params={'translatedLanguage[]': ['en']})
        vols = [float(k) for k in r.json().get('volumes', {}).keys() if k.lower() != 'none']
        return jsonify({'total_volumes': int(max(vols)) if vols else 0, 'latest_chapter': 0})
    except: return jsonify({'total_volumes': 0})

@app.route('/api/upload', methods=['POST'])
def upload_file():
    f = request.files['file']
    f.save(os.path.join(UPLOAD_FOLDER, secure_filename(f.filename)))
    return jsonify({'status': 'success', 'filename': secure_filename(f.filename)})

@app.route('/stream_convert')
def stream_convert():
    def generate():
        if not job_lock.acquire(blocking=False):
            yield "data: ERROR: Server Busy. Try again later.\n\n"; return
        
        try:
            mode = request.args.get('mode')
            manga_id = request.args.get('manga_id')
            profile = request.args.get('profile', 'KPW')
            format_type = request.args.get('format', 'EPUB')
            
            # Master Job ID (The Folder that will hold the final Zips/EPUBs)
            job_id = str(uuid.uuid4())
            final_output_dir = os.path.join(OUTPUT_FOLDER, job_id)
            os.makedirs(final_output_dir, exist_ok=True)
            
            yield f"data: STATUS: Job Started {job_id[:8]} \n\n"

            # --- SMART BATCH LOGIC ---
            if mode == 'mangadex':
                vol_start = int(request.args.get('vol_start', 1))
                vol_end = int(request.args.get('vol_end', 1))
                manga_title = re.sub(r'[\\/*?:"<>|]', "", request.args.get('manga_title', 'Manga')).strip()

                # Loop through volumes one by one to save RAM
                for current_vol in range(vol_start, vol_end + 1):
                    yield f"data: STATUS: Processing Volume {current_vol} of {vol_end}... \n\n"
                    
                    # Create a TEMPORARY folder just for this volume
                    vol_uuid = str(uuid.uuid4())
                    vol_dl_path = os.path.join(DOWNLOAD_FOLDER, vol_uuid)
                    os.makedirs(vol_dl_path, exist_ok=True)

                    # 1. Download JUST this volume
                    cmd_dl = ['mangadex-downloader', f"https://mangadex.org/title/{manga_id}", 
                              '--language', 'en', '--folder', vol_dl_path, '--no-group-name',
                              '--start-volume', str(current_vol), '--end-volume', str(current_vol)]
                    
                    for line in run_command_with_retry(cmd_dl):
                        if "api.mangadex.network/report" not in line: yield f"data: LOG: {line.strip()}\n\n"

                    # 2. Find images
                    inputs = []
                    for root, dirs, files in os.walk(vol_dl_path):
                        if is_image_dir(root): inputs.append(root)

                    if inputs:
                        # 3. Convert JUST this volume
                        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', final_output_dir]
                        kcc_cmd.extend(inputs)
                        yield f"data: STATUS: Converting Volume {current_vol}... \n\n"
                        for line in run_command_with_retry(kcc_cmd): yield f"data: LOG: {line.strip()}\n\n"
                    else:
                        yield f"data: LOG: Skipped Vol {current_vol} (No images found)\n\n"

                    # 4. NUCLEAR CLEANUP of RAW IMAGES immediately to free space
                    yield f"data: STATUS: Cleaning temp files for Vol {current_vol}... \n\n"
                    shutil.rmtree(vol_dl_path)

            # --- LOCAL / SCRAPER LOGIC (Simple) ---
            else:
                # (Existing logic for local/scraper kept simple for brevity)
                temp_dl = os.path.join(DOWNLOAD_FOLDER, job_id)
                inputs = []
                if mode == 'local': 
                    inputs.append(os.path.join(UPLOAD_FOLDER, request.args.get('filename')))
                elif mode in ['mangabat', 'mangabuddy', 'mangakakalot']:
                    scrape_website_images(request.args.get('chapter_url'), temp_dl)
                    inputs.append(temp_dl)

                kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', final_output_dir]
                kcc_cmd.extend(inputs)
                for line in run_command_with_retry(kcc_cmd): yield f"data: LOG: {line.strip()}\n\n"

            # --- FINAL PACKAGING ---
            yield "data: STATUS: Packaging... \n\n"
            generated_files = os.listdir(final_output_dir)
            
            if not generated_files:
                yield "data: ERROR: No files generated.\n\n"; return

            if len(generated_files) == 1:
                # Move single file to root output for download
                final_name = generated_files[0]
                shutil.move(os.path.join(final_output_dir, final_name), os.path.join(OUTPUT_FOLDER, final_name))
                yield f"data: DONE: {final_name}\n\n"
            else:
                # Zip multiple volumes
                zip_name = f"{manga_title}_Batch_Vol_{vol_start}-{vol_end}.zip"
                shutil.make_archive(os.path.join(OUTPUT_FOLDER, zip_name.replace('.zip','')), 'zip', final_output_dir)
                yield f"data: DONE: {zip_name}\n\n"

            # Cleanup the job output folder (since we moved files out)
            shutil.rmtree(final_output_dir)

        except Exception as e:
            yield f"data: ERROR: {str(e)}\n\n"
        finally:
            job_lock.release()

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
