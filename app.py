import os
import subprocess
import shutil
import requests
import sys
import time
import re
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configuration
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
# Temp folder for zipping to avoid recursion bug
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_zips')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure directories exist
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER, ZIP_TEMP]:
    os.makedirs(d, exist_ok=True)

def run_command_with_retry(cmd, max_retries=3):
    """Runs a command and streams output, retrying on failure."""
    attempt = 0
    while attempt < max_retries:
        try:
            # Using Popen with text=True and line buffering
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
            results.append({'id': manga['id'], 'title': title, 'desc': desc[:150] + '...', 'cover': cover_url})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/resolve_link', methods=['POST'])
def resolve_link():
    link = request.form.get('link')
    manga_id = extract_id_from_url(link)
    if not manga_id: return jsonify({'error': 'Invalid Mangadex URL'}), 400
    try:
        r = requests.get(f"https://api.mangadex.org/manga/{manga_id}")
        data = r.json()
        attr = data['data']['attributes']
        title = attr['title'].get('en') or list(attr['title'].values())[0]
        return jsonify({'id': manga_id, 'title': title})
    except Exception as e:
        return jsonify({'error': f'Failed to fetch details: {str(e)}'}), 500

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
        mode = request.args.get('mode', 'mangadex') 
        profile = request.args.get('profile', 'KPW') 
        format_type = request.args.get('format', 'EPUB')
        upscale = request.args.get('upscale') == 'true'
        manga_style = request.args.get('manga_style') == 'true'
        splitter = request.args.get('splitter') == 'true'

        # Clean previous runs
        if os.path.exists(OUTPUT_FOLDER): shutil.rmtree(OUTPUT_FOLDER)
        os.makedirs(OUTPUT_FOLDER)
        
        yield "data: STATUS: Initializing... \n\n"

        target_files = []
        final_title = "Converted_Manga" 

        # --- DOWNLOAD ---
        if mode == 'mangadex':
            manga_id = request.args.get('manga_id')
            manga_title = request.args.get('manga_title')
            vol_start = request.args.get('vol_start')
            vol_end = request.args.get('vol_end')
            final_title = manga_title

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
            local_file_path = os.path.join(UPLOAD_FOLDER, filename)
            if not os.path.exists(local_file_path):
                yield "data: ERROR: Uploaded file not found.\n\n"
                return
            target_files.append(os.path.abspath(local_file_path))
            yield f"data: STATUS: Found local file: {filename} \n\n"

        # --- CONVERT ---
        if not target_files:
            yield "data: ERROR: No files found to convert.\n\n"
            return
        
        yield f"data: LOG: Found {len(target_files)} file(s). Processing... \n\n"

        # CMD Construction
        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
        
        if upscale: kcc_cmd.append('--upscale')
        if manga_style: kcc_cmd.append('--manga-style')
        
        # Use Standard Splitter flag (1=Split, 2=Rotate&Split usually, but 1 is safer for basic split)
        if splitter: kcc_cmd.extend(['--splitter', '1'])
        
        kcc_cmd.extend(target_files)
        
        yield f"data: STATUS: Executing KCC... \n\n"
        yield f"data: LOG: CMD: {' '.join(kcc_cmd)} \n\n"
        
        for line in run_command_with_retry(kcc_cmd):
             yield f"data: LOG: {line.strip()}\n\n"

        # --- PACKAGING (FIXED RECURSION BUG) ---
        yield "data: STATUS: Finalizing... \n\n"
        output_files = [f for f in os.listdir(OUTPUT_FOLDER) if f.endswith(('.mobi', '.epub', '.azw3', '.cbz'))]
        
        if output_files:
             if len(output_files) == 1:
                 yield f"data: DONE: {output_files[0]}\n\n"
             else:
                 # ZIP to TEMP folder first, then move to OUTPUT
                 zip_name = f"{final_title} - Pack"
                 zip_path_temp = os.path.join(ZIP_TEMP, zip_name)
                 
                 shutil.make_archive(zip_path_temp, 'zip', OUTPUT_FOLDER)
                 
                 final_zip_name = f"{zip_name}.zip"
                 shutil.move(f"{zip_path_temp}.zip", os.path.join(OUTPUT_FOLDER, final_zip_name))
                 
                 yield f"data: DONE: {final_zip_name}\n\n"
        else:
             yield "data: ERROR: No output files generated. Check KCC logs above.\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
