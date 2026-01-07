import os
import subprocess
import shutil
import requests
import sys
import time
import re
import zipfile  # NEW: Required for merging chapters
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Configuration
BASE_DIR = '/tmp'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'kcc_output')
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'kcc_downloads')
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_temp_zips')
# NEW: Folder to assemble combined volumes
COMBINE_DIR = os.path.join(BASE_DIR, 'kcc_combined')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure directories exist
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER, ZIP_TEMP, COMBINE_DIR]:
    os.makedirs(d, exist_ok=True)

def run_command_with_retry(cmd, max_retries=3):
    """Runs a command and streams output, retrying on failure."""
    attempt = 0
    while attempt < max_retries:
        try:
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
        # NEW: Checkbox for combining
        combine = request.args.get('combine') == 'true'

        # Cleanup Output
        if os.path.exists(OUTPUT_FOLDER): shutil.rmtree(OUTPUT_FOLDER)
        os.makedirs(OUTPUT_FOLDER)
        
        yield "data: STATUS: Initializing... \n\n"

        target_files = []
        final_title = "Converted_Manga" 
        manga_title_clean = "Manga"
        volume_label = ""

        # --- DOWNLOAD PHASE ---
        if mode == 'mangadex':
            manga_id = request.args.get('manga_id')
            manga_title = request.args.get('manga_title')
            vol_start = request.args.get('vol_start')
            vol_end = request.args.get('vol_end')
            
            manga_title_clean = re.sub(r'[\\/*?:"<>|]', "", manga_title)
            volume_label = f"Vol {vol_start}" if vol_start == vol_end else f"Vol {vol_start}-{vol_end}"
            final_title = f"{manga_title_clean} {volume_label}"

            dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
            if os.path.exists(dl_path): shutil.rmtree(dl_path)

            cmd_dl = ['mangadex-downloader', f"https://mangadex.org/title/{manga_id}", '--language', 'en', '--folder', dl_path, '--no-group-name', '--save-as', 'cbz']
            if vol_start and vol_end: cmd_dl.extend(['--start-volume', vol_start, '--end-volume', vol_end])
            
            yield "data: STATUS: Downloading from Mangadex... \n\n"
            for line in run_command_with_retry(cmd_dl):
                yield f"data: LOG: {line.strip()}\n\n"

            # Gather all CBZ files
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

        if not target_files:
            yield "data: ERROR: No files found to convert.\n\n"
            return

        # --- COMBINE PHASE (NEW) ---
        # If the user wants to combine AND we have multiple files (or even one file we want to rename)
        if combine and mode == 'mangadex':
            yield f"data: STATUS: Merging {len(target_files)} chapters into single volume '{final_title}'... \n\n"
            
            # Prepare Combined Directory
            safe_combined_name = secure_filename(final_title)
            combined_dir_path = os.path.join(COMBINE_DIR, safe_combined_name)
            
            if os.path.exists(combined_dir_path): shutil.rmtree(combined_dir_path)
            os.makedirs(combined_dir_path)
            
            # Sort files so Chapter 1 is before Chapter 2
            target_files.sort()

            chapter_count = 0
            for cbz_file in target_files:
                chapter_count += 1
                try:
                    with zipfile.ZipFile(cbz_file, 'r') as z:
                        # Get valid images
                        images = sorted([f for f in z.namelist() if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))])
                        page_count = 0
                        for img in images:
                            page_count += 1
                            # Rename: Ch001_Page001.jpg -> Ensures proper sort order
                            ext = os.path.splitext(img)[1]
                            new_name = f"C{chapter_count:04d}_P{page_count:04d}{ext}"
                            
                            with open(os.path.join(combined_dir_path, new_name), 'wb') as f_out:
                                f_out.write(z.read(img))
                except Exception as e:
                    yield f"data: LOG: Warning: Failed to extract {os.path.basename(cbz_file)}: {e}\n\n"
            
            # KCC targets the FOLDER now, not the individual files
            target_files = [combined_dir_path]
            yield f"data: LOG: Merging complete. Created source folder: {safe_combined_name}\n\n"

        # --- CONVERT PHASE ---
        yield f"data: STATUS: Executing KCC Conversion... \n\n"
        
        # KCC command
        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
        if upscale: kcc_cmd.append('-u')
        if manga_style: kcc_cmd.append('-m')
        if splitter: kcc_cmd.extend(['-s', '1'])
        
        kcc_cmd.extend(target_files)
        
        yield f"data: LOG: Command: {' '.join(kcc_cmd)} \n\n"
        
        for line in run_command_with_retry(kcc_cmd):
             yield f"data: LOG: {line.strip()}\n\n"

        # --- PACKAGING PHASE ---
        yield "data: STATUS: Finalizing... \n\n"
        output_files = [f for f in os.listdir(OUTPUT_FOLDER) if f.endswith(('.mobi', '.epub', '.azw3', '.cbz'))]
        
        if output_files:
             if len(output_files) == 1:
                 # If we combined, the file is already named like "Chainsaw Man Vol 1.epub" (based on folder name)
                 result_file = output_files[0]
                 yield f"data: DONE: {result_file}\n\n"
             else:
                 # Fallback for multiple files
                 zip_name = f"{final_title} - Pack"
                 temp_zip_path = os.path.join(ZIP_TEMP, zip_name)
                 shutil.make_archive(temp_zip_path, 'zip', OUTPUT_FOLDER)
                 final_zip_name = f"{zip_name}.zip"
                 shutil.move(f"{temp_zip_path}.zip", os.path.join(OUTPUT_FOLDER, final_zip_name))
                 yield f"data: DONE: {final_zip_name}\n\n"
        else:
             yield "data: ERROR: No output files generated.\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
