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
            # Check if cmd is a list (safe) or string (unsafe but sometimes needed)
            if isinstance(cmd, list):
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            else:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, shell=True)
                
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
    try:
        query = request.form.get('query')
        url = "https://api.mangadex.org/manga"
        params = {'title': query, 'limit': 10, 'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'], 'order[relevance]': 'desc', 'includes[]': ['cover_art']}
        r = requests.get(url, params=params)
        data = r.json()
        results = []
        for m in data.get('data', []):
            title = m['attributes']['title'].get('en') or list(m['attributes']['title'].values())[0]
            desc = m['attributes']['description'].get('en', 'No Desc')[:100]
            cover_file = next((rel['attributes']['fileName'] for rel in m.get('relationships', []) if rel['type'] == 'cover_art'), None)
            cover_url = f"https://uploads.mangadex.org/covers/{m['id']}/{cover_file}.256.jpg" if cover_file else ""
            results.append({'id': m['id'], 'title': title, 'desc': desc, 'cover': cover_url})
        return jsonify(results)
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    try:
        m_id = request.form.get('manga_id')
        r = requests.get(f"https://api.mangadex.org/manga/{m_id}/aggregate", params={'translatedLanguage[]': ['en']})
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
            yield "data: ERROR: Server Busy. Wait for current job.\n\n"; return
        
        try:
            mode = request.args.get('mode')
            manga_id = request.args.get('manga_id')
            profile = request.args.get('profile', 'KPW')
            format_type = request.args.get('format', 'EPUB')
            
            job_id = str(uuid.uuid4())
            final_output_dir = os.path.join(OUTPUT_FOLDER, job_id)
            os.makedirs(final_output_dir, exist_ok=True)
            
            yield f"data: STATUS: Job Started {job_id[:8]} \n\n"

            # --- SMART BATCH LOGIC ---
            if mode == 'mangadex':
                # Parse inputs safely
                try:
                    vol_start = int(request.args.get('vol_start', 1))
                    vol_end = int(request.args.get('vol_end', 1))
                except:
                    vol_start = 1
                    vol_end = 1
                    
                manga_title = re.sub(r'[\\/*?:"<>|]', "", request.args.get('manga_title', 'Manga')).strip()

                # Loop through volumes
                for current_vol in range(vol_start, vol_end + 1):
                    yield f"data: STATUS: Processing Volume {current_vol} of {vol_end}... \n\n"
                    
                    vol_uuid = str(uuid.uuid4())
                    vol_dl_path = os.path.join(DOWNLOAD_FOLDER, vol_uuid)
                    os.makedirs(vol_dl_path, exist_ok=True)

                    # BUILD COMMAND SAFELY USING APPEND
                    cmd_dl = []
                    cmd_dl.append('mangadex-downloader')
                    cmd_dl.append(f"https://mangadex.org/title/{manga_id}")
                    cmd_dl.append('--language')
                    cmd_dl.append('en')
                    cmd_dl.append('--folder')
                    cmd_dl.append(vol_dl_path)
                    cmd_dl.append('--no-group-name')
                    cmd_dl.append('--start-volume')
                    cmd_dl.append(str(current_vol))
                    cmd_dl.append('--end-volume')
                    cmd_dl.append(str(current_vol))
                    
                    dl_success = False
                    for line in run_command_with_retry(cmd_dl):
                        if "api.mangadex.network/report" not in line: 
                            yield f"data: LOG: {line.strip()}\n\n"
                        if "Download finished" in line or "Getting images" in line:
                            dl_success = True

                    # Find images
                    inputs = []
                    for root, dirs, files in os.walk(vol_dl_path):
                        if is_image_dir(root): inputs.append(root)

                    if inputs:
                        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', final_output_dir]
                        kcc_cmd.extend(inputs)
                        yield f"data: STATUS: Converting Volume {current_vol}... \n\n"
                        for line in run_command_with_retry(kcc_cmd): yield f"data: LOG: {line.strip()}\n\n"
                    else:
                        yield f"data: LOG: Skipped Vol {current_vol} (No images found)\n\n"

                    # CLEANUP RAW IMAGES
                    try:
                        shutil.rmtree(vol_dl_path)
                    except:
                        pass

            # --- LOCAL / SCRAPER LOGIC ---
            else:
                temp_dl = os.path.join(DOWNLOAD_FOLDER, job_id)
                inputs = []
                if mode == 'local': 
                    inputs.append(os.path.join(UPLOAD_FOLDER, request.args.get('filename')))
                elif mode in ['mangabat', 'mangabuddy', 'mangakakalot']:
                    yield f"data: STATUS: Scraping {mode}... \n\n"
                    os.makedirs(temp_dl, exist_ok=True)
                    for log in scrape_website_images(request.args.get('chapter_url'), temp_dl):
                        yield f"data: LOG: {log}"
                    inputs.append(temp_dl)

                if inputs:
                    kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', final_output_dir]
                    kcc_cmd.extend(inputs)
                    for line in run_command_with_retry(kcc_cmd): yield f"data: LOG: {line.strip()}\n\n"

            # --- FINAL PACKAGING ---
            yield "data: STATUS: Packaging... \n\n"
            generated_files = os.listdir(final_output_dir)
            
            if not generated_files:
                yield "data: ERROR: No files generated.\n\n"; return

            if len(generated_files) == 1:
                final_name = generated_files[0]
                shutil.move(os.path.join(final_output_dir, final_name), os.path.join(OUTPUT_FOLDER, final_name))
                yield f"data: DONE: {final_name}\n\n"
            else:
                # Zip multiple volumes
                safe_title = re.sub(r'[\\/*?:"<>|]', "", request.args.get('manga_title', 'Batch')).strip()
                zip_name = f"{safe_title}_Vol_{request.args.get('vol_start')}-{request.args.get('vol_end')}.zip"
                shutil.make_archive(os.path.join(OUTPUT_FOLDER, zip_name.replace('.zip','')), 'zip', final_output_dir)
                yield f"data: DONE: {zip_name}\n\n"

            try:
                shutil.rmtree(final_output_dir)
            except:
                pass

        except Exception as e:
            yield f"data: ERROR: {str(e)}\n\n"
        finally:
            job_lock.release()

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    clean_name = filename
    # Remove UUID prefix if present (36 chars + 1 underscore)
    if len(filename) > 37 and filename[36] == '_':
        try:
            # Check if first 36 chars are a UUID
            uuid.UUID(filename[:36])
            clean_name = filename[37:]
        except:
            pass
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True, download_name=clean_name)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
