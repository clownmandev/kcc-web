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
# We write EVERYTHING to /tmp, which MUST be mapped to your host
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_temp_zips')
COMBINE_DIR = os.path.join(BASE_DIR, 'kcc_combined')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure directories exist (Don't try to delete them, just make sure they exist)
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
            if process.returncode == 0:
                return
            yield f"WARNING: Process failed with code {process.returncode}. Retrying...\n"
        except Exception as e:
            yield f"ERROR: Execution failed: {str(e)}\n"
        attempt += 1
        time.sleep(2)
    yield "FAILURE: Max retries reached.\n"

def is_image_dir(path):
    if not os.path.isdir(path): return False
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp', '*.gif']:
        if glob.glob(os.path.join(path, ext)):
            return True
    return False

def scrape_website_images(url, save_folder):
    domain = urlparse(url).netloc
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': f"https://{domain}/" 
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        html = response.text
        img_urls = re.findall(r'(?:src|data-src)="([^"]+?\.(?:jpg|jpeg|png|webp))"', html)
        img_urls = list(dict.fromkeys(img_urls))
        if not img_urls:
            yield "LOG: No images found. Ensure this is a CHAPTER URL.\n"
            return
        yield f"LOG: Found {len(img_urls)} images. Downloading...\n"
        for i, img_url in enumerate(img_urls):
            try:
                if not img_url.startswith('http'): continue 
                img_name = f"page_{i:04d}.jpg"
                img_save_path = os.path.join(save_folder, img_name)
                with requests.get(img_url, headers=headers, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    with open(img_save_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            f.write(chunk)
                if i % 10 == 0: yield f"LOG: Downloaded page {i+1}/{len(img_urls)}\n"
            except Exception as e:
                yield f"LOG: Failed to download image {i+1}: {e}\n"
    except Exception as e:
        yield f"ERROR: Scraping failed: {e}\n"

# --- API ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    query = request.form.get('query')
    url = "https://api.mangadex.org/manga"
    params = {'title': query, 'limit': 10, 'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'], 'order[relevance]': 'desc', 'includes[]': ['cover_art']}
    try:
        r = requests.get(url, params=params)
        data = r.json()
        results = []
        for manga in data.get('data', []):
            attr = manga['attributes']
            title = attr['title'].get('en') or list(attr['title'].values())[0]
            desc = attr['description'].get('en', 'No description available.')
            cover_file = None
            for rel in manga.get('relationships', []):
                if rel['type'] == 'cover_art':
                    cover_file = rel['attributes']['fileName']
                    break
            cover_url = f"https://uploads.mangadex.org/covers/{manga['id']}/{cover_file}.256.jpg" if cover_file else "https://via.placeholder.com/100x150?text=No+Cover"
            results.append({'id': manga['id'], 'title': title, 'desc': desc[:150] + '...', 'cover': cover_url})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    manga_id = request.form.get('manga_id')
    try:
        url = f"https://api.mangadex.org/manga/{manga_id}/aggregate"
        r = requests.get(url, params={'translatedLanguage[]': ['en']})
        data = r.json()
        volumes = data.get('volumes', {})
        max_vol = 0
        for vol_key, vol_data in volumes.items():
            try:
                v_num = float(vol_key)
                if v_num > max_vol: max_vol = v_num
            except: pass
        return jsonify({'total_volumes': int(max_vol) if max_vol else 0, 'latest_chapter': 0})
    except:
        return jsonify({'total_volumes': 0, 'latest_chapter': 0})

@app.route('/api/upload', methods=['POST'])
def upload_file():
    file = request.files['file']
    filename = secure_filename(file.filename)
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)
    return jsonify({'status': 'success', 'filename': filename})

@app.route('/stream_convert')
def stream_convert():
    def generate():
        if not job_lock.acquire(blocking=False):
            yield "data: ERROR: Server is busy. Wait for current job.\n\n"
            return
        
        try:
            mode = request.args.get('mode', 'mangadex') 
            profile = request.args.get('profile', 'KPW') 
            format_type = request.args.get('format', 'EPUB')
            combine = request.args.get('combine') == 'true'

            # 1. GENERATE UNIQUE ID - NO DELETING OLD FILES
            job_id = str(uuid.uuid4())
            job_dl_path = os.path.join(DOWNLOAD_FOLDER, job_id)
            job_out_path = os.path.join(OUTPUT_FOLDER, job_id)
            os.makedirs(job_dl_path, exist_ok=True)
            os.makedirs(job_out_path, exist_ok=True)
            
            yield f"data: STATUS: Starting Job {job_id} \n\n"

            target_inputs = [] 
            final_title = "Converted_Manga"
            
            # --- DOWNLOADER ---
            if mode == 'mangadex':
                manga_id = request.args.get('manga_id')
                manga_title = request.args.get('manga_title', 'Manga')
                clean_title = re.sub(r'[\\/*?:"<>|]', "", manga_title).strip()
                final_title = clean_title

                cmd_dl = ['mangadex-downloader', f"https://mangadex.org/title/{manga_id}", '--language', 'en', '--folder', job_dl_path, '--no-group-name']
                if request.args.get('vol_start') and request.args.get('vol_end'): 
                    cmd_dl.extend(['--start-volume', request.args.get('vol_start'), '--end-volume', request.args.get('vol_end')])
                if request.args.get('chap_start'): cmd_dl.extend(['--start-chapter', request.args.get('chap_start')])
                if request.args.get('chap_end'): cmd_dl.extend(['--end-chapter', request.args.get('chap_end')])
                
                yield "data: STATUS: Downloading... \n\n"
                for line in run_command_with_retry(cmd_dl):
                    if "api.mangadex.network/report" not in line:
                        yield f"data: LOG: {line.strip()}\n\n"

                for root, dirs, files in os.walk(job_dl_path):
                    if is_image_dir(root): target_inputs.append(root)

            elif mode in ['mangabat', 'mangabuddy', 'mangakakalot']:
                chapter_url = request.args.get('chapter_url')
                yield f"data: STATUS: Scraping... \n\n"
                for log in scrape_website_images(chapter_url, job_dl_path):
                    yield f"data: LOG: {log}"
                if is_image_dir(job_dl_path): target_inputs.append(job_dl_path)

            elif mode == 'local':
                filename = request.args.get('filename')
                final_title = os.path.splitext(filename)[0]
                target_inputs.append(os.path.join(UPLOAD_FOLDER, filename))

            if not target_inputs:
                yield "data: ERROR: No content found.\n\n"
                return

            # --- COMBINE ---
            is_doc = any(f.endswith(('.pdf', '.epub', '.mobi')) for f in target_inputs if isinstance(f, str))
            
            if combine and not is_doc and (len(target_inputs) > 1 or mode == 'mangadex'):
                yield f"data: STATUS: Merging... \n\n"
                safe_name = secure_filename(final_title)
                job_combine_path = os.path.join(COMBINE_DIR, job_id, safe_name)
                os.makedirs(job_combine_path, exist_ok=True)
                
                target_inputs.sort()
                img_idx = 0
                for src in target_inputs:
                    try:
                        if os.path.isdir(src):
                            images = sorted(glob.glob(os.path.join(src, '*')))
                            for img_path in images:
                                if img_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                                    img_idx += 1
                                    ext = os.path.splitext(img_path)[1]
                                    new_name = f"img_{img_idx:06d}{ext}"
                                    shutil.copy(img_path, os.path.join(job_combine_path, new_name))
                    except: pass
                target_inputs = [job_combine_path]

            # --- CONVERT ---
            yield "data: STATUS: Converting... \n\n"
            kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', job_out_path]
            kcc_cmd.extend(target_inputs)
            
            for line in run_command_with_retry(kcc_cmd):
                 yield f"data: LOG: {line.strip()}\n\n"

            # --- FINALIZE ---
            yield "data: STATUS: Finalizing... \n\n"
            output_files = [f for f in os.listdir(job_out_path) if f.endswith(('.mobi', '.epub', '.azw3', '.cbz', '.kpub'))]
            
            if not output_files:
                yield "data: ERROR: No output generated.\n\n"
                return

            final_filename = output_files[0]
            # Copy to global output with unique name so download link works
            unique_name = f"{job_id}_{final_filename}"
            shutil.copy(os.path.join(job_out_path, final_filename), os.path.join(OUTPUT_FOLDER, unique_name))
            
            yield f"data: DONE: {unique_name}\n\n"
        
        except Exception as e:
            yield f"data: ERROR: {str(e)}\n\n"
        finally:
            job_lock.release()

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    # Default to the full name
    clean_name = filename
    
    # Check if it starts with a UUID (36 chars) + Underscore (1 char) = 37 chars
    # Example: ead8c778-a1ec-410d-b868-de6e66654605_Chainsaw_Man.epub
    if len(filename) > 37 and filename[36] == '_':
        # Slice off the first 37 characters
        clean_name = filename[37:]

    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True, download_name=clean_name)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
