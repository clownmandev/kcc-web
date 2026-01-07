import os
import subprocess
import shutil
import requests
import sys
import time
import re
import zipfile
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- CONFIGURATION ---
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_temp_zips')
COMBINE_DIR = os.path.join(BASE_DIR, 'kcc_combined')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure all directories exist on startup
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER, ZIP_TEMP, COMBINE_DIR]:
    os.makedirs(d, exist_ok=True)

# --- HELPER FUNCTIONS ---

def run_command_with_retry(cmd, max_retries=3):
    """Runs a shell command and yields output. Retries on failure."""
    attempt = 0
    while attempt < max_retries:
        try:
            # bufsize=0 and text=True line buffering
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in process.stdout:
                yield line
            
            process.wait()
            
            if process.returncode == 0:
                return
            
            yield f"WARNING: Process failed with code {process.returncode}. Retrying ({attempt+1}/{max_retries})..."
            
        except Exception as e:
            yield f"ERROR: System execution failed: {str(e)}"
        
        attempt += 1
        time.sleep(2)
        
    yield "FAILURE: Max retries reached."

def extract_id_from_url(url):
    match = re.search(r'title/([a-f0-9\-]+)', url)
    return match.group(1) if match else None

# --- API ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    query = request.form.get('query')
    if not query: return jsonify({'error': 'No query provided'}), 400
    
    url = "https://api.mangadex.org/manga"
    params = {
        'title': query, 
        'limit': 10, 
        'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'], 
        'order[relevance]': 'desc',
        'includes[]': ['cover_art']
    }
    
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

            results.append({
                'id': manga['id'], 
                'title': title, 
                'desc': desc[:150] + '...',
                'cover': cover_url
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/resolve_link', methods=['POST'])
def resolve_link():
    link = request.form.get('link')
    manga_id = extract_id_from_url(link)
    
    if not manga_id:
        return jsonify({'error': 'Invalid Mangadex URL'}), 400

    try:
        r = requests.get(f"https://api.mangadex.org/manga/{manga_id}")
        data = r.json()
        attr = data['data']['attributes']
        title = attr['title'].get('en') or list(attr['title'].values())[0]
        return jsonify({'id': manga_id, 'title': title})
    except Exception as e:
        return jsonify({'error': f'Failed to fetch details: {str(e)}'}), 500

@app.route('/api/manga_details', methods=['POST'])
def get_manga_details():
    manga_id = request.form.get('manga_id')
    if not manga_id: return jsonify({'error': 'No ID provided'}), 400
    
    try:
        url = f"https://api.mangadex.org/manga/{manga_id}/aggregate"
        r = requests.get(url, params={'translatedLanguage[]': ['en']})
        data = r.json()
        
        volumes = data.get('volumes', {})
        max_vol = 0
        max_chap = 0
        
        for vol_key, vol_data in volumes.items():
            if vol_key and vol_key.lower() != 'none':
                try:
                    v_num = float(vol_key)
                    if v_num > max_vol: max_vol = v_num
                except: pass
            
            for chap_key in vol_data.get('chapters', {}).keys():
                try:
                    c_num = float(chap_key)
                    if c_num > max_chap: max_chap = c_num
                except: pass

        if isinstance(max_vol, float) and max_vol.is_integer(): max_vol = int(max_vol)
        if isinstance(max_chap, float) and max_chap.is_integer(): max_chap = int(max_chap)

        return jsonify({'total_volumes': max_vol, 'latest_chapter': max_chap})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No selected file'}), 400
    
    if file:
        filename = secure_filename(file.filename)
        if os.path.exists(UPLOAD_FOLDER): shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER)
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        return jsonify({'status': 'success', 'filename': filename})

@app.route('/stream_convert')
def stream_convert():
    def generate():
        # --- PARSE ARGS ---
        mode = request.args.get('mode', 'mangadex') 
        profile = request.args.get('profile', 'KPW') 
        format_type = request.args.get('format', 'EPUB')
        upscale = request.args.get('upscale') == 'true'
        manga_style = request.args.get('manga_style') == 'true'
        splitter = request.args.get('splitter') == 'true'
        combine = request.args.get('combine') == 'true'

        # Cleanup Output
        if os.path.exists(OUTPUT_FOLDER): shutil.rmtree(OUTPUT_FOLDER)
        os.makedirs(OUTPUT_FOLDER)
        
        yield "data: STATUS: Initializing... \n\n"

        target_files = []
        final_title = "Converted_Manga"
        
        # --- DOWNLOAD ---
        if mode == 'mangadex':
            manga_id = request.args.get('manga_id')
            manga_title = request.args.get('manga_title', 'Manga')
            vol_start = request.args.get('vol_start')
            vol_end = request.args.get('vol_end')
            
            # Safe Filename Generation
            clean_title = re.sub(r'[\\/*?:"<>|]', "", manga_title).strip()
            if not clean_title: clean_title = "Manga_Download"
            
            vol_label = f"Vol {vol_start}" if vol_start == vol_end else f"Vol {vol_start}-{vol_end}"
            final_title = f"{clean_title} {vol_label}"

            dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
            if os.path.exists(dl_path): shutil.rmtree(dl_path)

            cmd_dl = ['mangadex-downloader', f"https://mangadex.org/title/{manga_id}", '--language', 'en', '--folder', dl_path, '--no-group-name', '--save-as', 'cbz']
            if vol_start and vol_end: cmd_dl.extend(['--start-volume', vol_start, '--end-volume', vol_end])
            
            yield "data: STATUS: Downloading from Mangadex... \n\n"
            for line in run_command_with_retry(cmd_dl):
                yield f"data: LOG: {line.strip()}\n\n"

            for root, dirs, files in os.walk(dl_path):
                for file in files:
                    if file.endswith(('.cbz', '.zip', '.cb7', '.epub')):
                        target_files.append(os.path.abspath(os.path.join(root, file)))

        elif mode == 'local':
            filename = request.args.get('filename')
            final_title = os.path.splitext(filename)[0]
            local_path = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(local_path):
                target_files.append(os.path.abspath(local_path))
            else:
                yield "data: ERROR: File not found.\n\n"
                return

        if not target_files:
            yield "data: ERROR: No content found to convert.\n\n"
            return

        # --- COMBINE LOGIC ---
        if combine and mode == 'mangadex':
            yield f"data: STATUS: Merging {len(target_files)} files into one... \n\n"
            
            safe_name = secure_filename(final_title)
            if not safe_name: safe_name = "Combined_Manga"
            
            combined_path = os.path.join(COMBINE_DIR, safe_name)
            if os.path.exists(combined_path): shutil.rmtree(combined_path)
            os.makedirs(
