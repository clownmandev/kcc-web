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
ZIP_TEMP = os.path.join(BASE_DIR, 'kcc_temp_zips')     # Safe zone for zip creation
COMBINE_DIR = os.path.join(BASE_DIR, 'kcc_combined')    # Staging area for merging chapters

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure all directories exist on startup
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, DOWNLOAD_FOLDER, ZIP_TEMP, COMBINE_DIR]:
    os.makedirs(d, exist_ok=True)

# --- HELPER FUNCTIONS ---

def run_command_with_retry(cmd, max_retries=3):
    """Runs a shell command and yields the output line-by-line. Retries on failure."""
    attempt = 0
    while attempt < max_retries:
        try:
            # bufsize=1 enables line-buffering for real-time logs
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
        time.sleep(2) # Cooldown before retry
        
    yield "FAILURE: Max retries reached."

def extract_id_from_url(url):
    """Extracts MangaDex ID from a direct URL."""
    match = re.search(r'title/([a-f0-9\-]+)', url)
    return match.group(1) if match else None

# --- API ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    """Searches MangaDex for a title."""
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
            # Handle titles in different languages
            title = attr['title'].get('en') or list(attr['title'].values())[0]
            desc = attr['description'].get('en', 'No description available.')
            
            # Find cover image
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
    """Resolves a direct MangaDex link to ID and Title."""
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
    """Fetches stats (Volume count, latest chapter) for a specific manga."""
    manga_id = request.form.get('manga_id')
    if not manga_id: return jsonify({'error': 'No ID provided'}), 400
    
    try:
        # 'aggregate' endpoint gives us a summary of volumes and chapters
        url = f"https://api.mangadex.org/manga/{manga_id}/aggregate"
        r = requests.get(url, params={'translatedLanguage[]': ['en']})
        data = r.json()
        
        volumes = data.get('volumes', {})
        
        max_vol = 0
        max_chap = 0
        
        # Iterate through the messy aggregate data to find max values
        for vol_key, vol_data in volumes.items():
            if vol_key and vol_key.lower() != 'none':
                try:
                    v_num = float(vol_key)
                    if v_num > max_vol: max_vol = v_num
                except:
                    pass
            
            chapters = vol_data.get('chapters', {})
            for chap_key in chapters.keys():
                try:
                    c_num = float(chap_key)
                    if c_num > max_chap: max_chap = c_num
                except:
                    pass

        # Clean up numbers (10.0 -> 10)
        if isinstance(max_vol, float) and max_vol.is_integer(): max_vol = int(max_vol)
        if isinstance(max_chap, float) and max_chap.is_integer(): max_chap = int(max_chap)

        return jsonify({
            'total_volumes': max_vol,
            'latest_chapter': max_chap
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handles local file uploads."""
    if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No selected file'}), 400
    
    if file:
        filename = secure_filename(file.filename)
        # Clear upload folder to prevent clutter
        if os.path.exists(UPLOAD_FOLDER): shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER)
        
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(save_path)
        return jsonify({'status': 'success', 'filename': filename})

# --- CORE CONVERSION LOGIC ---

@app.route('/stream_convert')
def stream_convert():
    def generate():
        # 1. Parse Arguments
        mode = request.args.get('mode', 'mangadex') 
        profile = request.args.get('profile', 'KPW') 
        format_type = request.args.get('format', 'EPUB')
        upscale = request.args.get('upscale') == 'true'
        manga_style = request.args.get('manga_style') == 'true'
        splitter = request.args.get('splitter') == 'true'
        combine = request.args.get('combine') == 'true'

        # 2. Cleanup & Setup
        if os.path.exists(OUTPUT_FOLDER): shutil.rmtree(OUTPUT_FOLDER)
        os.makedirs(OUTPUT_FOLDER)
        
        yield "data: STATUS: Initializing... \n\n"

        target_files = []
        final_title = "Converted_Manga"
        
        # --- DOWNLOAD PHASE ---
        if mode == 'mangadex':
            manga_id = request.args.get('manga_id')
            manga_title = request.args.get('manga_title')
            vol_start = request.args.get('vol_start')
            vol_end = request.args.get('vol_end')
            
            # Create a clean title for the filename
            clean_title = re.sub(r'[\\/*?:"<>|]', "", manga_title)
            vol_label = f"Vol {vol_start}" if vol_start == vol_end else f"Vol {vol_start}-{vol_end}"
            final_title = f"{clean_title} {vol_label}"

            dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
            if os.path.exists(dl_path): shutil.rmtree(dl_path)

            # Mangadex Downloader Command
            cmd_dl = [
                'mangadex-downloader', 
                f"https://mangadex.org/title/{manga_id}",
                '--language', 'en',
                '--folder', dl_path,
                '--no-group-name',
                '--save-as', 'cbz' # Always DL as CBZ first
            ]
            
            # Apply Volume Range
            if vol_start and vol_end:
                 cmd_dl.extend(['--start-volume', vol_start, '--end-volume', vol_end])
            
            yield "data: STATUS: Downloading from Mangadex... \n\n"
            for line in run_command_with_retry(cmd_dl):
                yield f"data: LOG: {line.strip()}\n\n"

            # Locate Downloaded Files
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

        # Check if we actually have files
        if not target_files:
            yield "data: ERROR: No files found to convert (Download may have failed or no chapters exist in this range).\n\n"
            return

        # --- COMBINE / MERGE PHASE ---
        # If 'Combine' is checked AND we are in Mangadex mode (or just want to repack a local file)
        if combine and mode == 'mangadex':
            yield f"data: STATUS: Merging {len(target_files)} chapters into single volume '{final_title}'... \n\n"
            
            # 1. Prepare Directory
            safe_combined_name = secure_filename(final_title)
            # Ensure we don't have empty spaces or weird chars
            if not safe_combined_name: safe_combined_name = "Combined_Manga"
            
            combined_dir_path = os.path.join(COMBINE_DIR, safe_combined_name)
            if os.path.exists(combined_dir_path): shutil.rmtree(combined_dir_path)
            os.makedirs(combined_dir_path)
            
            # 2. Sort input files (Critical: Chapter 1 must come before Chapter 2)
            target_files.sort()

            # 3. Extract and Rename
            total_img_counter = 0
            
            for cbz_file in target_files:
                try:
                    with zipfile.ZipFile(cbz_file, 'r') as z:
                        # Filter valid images
                        images = sorted([f for f in z.namelist() if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))])
                        
                        for img in images:
                            total_img_counter += 1
                            ext = os.path.splitext(img)[1]
                            
                            # RENAME to strictly sequential: "img_00001.jpg", "img_00002.jpg"
                            # This forces KCC to see one continuous stream of pages.
                            new_name = f"img_{total_img_counter:05d}{ext}"
                            
                            with open(os.path.join(combined_dir_path, new_name), 'wb') as f_out:
                                f_out.write(z.read(img))
                except Exception as e:
                    yield f"data: LOG: Warning: Skipped {os.path.basename(cbz_file)} due to error: {e}\n\n"
            
            # 4. Point KCC to the FOLDER, not the files
            target_files = [combined_dir_path]
            yield f"data: LOG: Merging complete. Created source folder: {safe_combined_name}\n\n"

        # --- KCC CONVERSION PHASE ---
        yield f"data: STATUS: Executing KCC Conversion... \n\n"
        
        # Build Command
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
        
        # Find results
        output_files = [f for f in os.listdir(OUTPUT_FOLDER) if f.endswith(('.mobi', '.epub', '.azw3', '.cbz'))]
        
        if output_files:
             if len(output_files) == 1:
                 # Case A: Single File (Combined or just one chapter downloaded)
                 result_file = output_files[0]
                 yield f"data: DONE: {result_file}\n\n"
             else:
                 # Case B: Multiple Files (No Combine, or converting multiple local files)
                 # Zip them safely using ZIP_TEMP to avoid infinite loops
                 zip_name = f"{final_title} - Pack"
                 temp_zip_path = os.path.join(ZIP_TEMP, zip_name)
                 
                 shutil.make_archive(temp_zip_path, 'zip', OUTPUT_FOLDER)
                 
                 # Move finalized zip to output
                 final_zip_name = f"{zip_name}.zip"
                 shutil.move(f"{temp_zip_path}.zip", os.path.join(OUTPUT_FOLDER, final_zip_name))
                 
                 yield f"data: DONE: {final_zip_name}\n\n"
        else:
             yield "data: ERROR: Conversion finished but no output files were found.\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    """Serves the final file to the user."""
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    # Listen on all interfaces
    app.run(host='0.0.0.0', port=5000)
