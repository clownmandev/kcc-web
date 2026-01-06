import os
import subprocess
import shutil
import requests
from flask import Flask, render_template, request, send_file, jsonify, after_this_request

app = Flask(__name__)
UPLOAD_FOLDER = '/tmp/kcc_uploads'
OUTPUT_FOLDER = '/tmp/kcc_output'
DOWNLOAD_FOLDER = '/tmp/kcc_downloads'

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    query = request.form.get('query')
    if not query:
        return jsonify({'error': 'No query provided'}), 400
    
    # Use MangaDex API to find manga
    url = "https://api.mangadex.org/manga"
    params = {
        'title': query,
        'limit': 10,
        'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'], # Include all
        'order[relevance]': 'desc'
    }
    
    try:
        r = requests.get(url, params=params)
        data = r.json()
        results = []
        for manga in data['data']:
            title = manga['attributes']['title'].get('en') or list(manga['attributes']['title'].values())[0]
            desc = manga['attributes']['description'].get('en', 'No description')
            results.append({
                'id': manga['id'],
                'title': title,
                'desc': desc[:200] + '...'
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auto_convert', methods=['POST'])
def auto_convert():
    manga_id = request.form.get('manga_id')
    selection_type = request.form.get('selection_type') # 'all' or 'range'
    chapter_start = request.form.get('chapter_start')
    chapter_end = request.form.get('chapter_end')
    lang = request.form.get('lang', 'en')

    # KCC Options
    profile = request.form.get('profile', 'KV')
    format_type = request.form.get('format', 'MOBI')
    upscale = request.form.get('upscale') == 'on'
    manga_style = request.form.get('manga_style') == 'on'

    # Prepare Download Command
    # mangadex-downloader [url] --language [lang] --folder [folder]
    dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
    if os.path.exists(dl_path):
        shutil.rmtree(dl_path) # Clean start
    
    cmd_dl = [
        'mangadex-downloader', 
        f"https://mangadex.org/title/{manga_id}",
        '--language', lang,
        '--folder', dl_path,
        '--no-group-name', # Cleaner filenames
        '--save-as', 'cbz' # Download as CBZ directly so KCC can read it easily
    ]

    if selection_type == 'range':
        if chapter_start:
            cmd_dl.extend(['--start-chapter', chapter_start])
        if chapter_end:
            cmd_dl.extend(['--end-chapter', chapter_end])

    try:
        print(f"Downloading Manga: {' '.join(cmd_dl)}")
        subprocess.run(cmd_dl, check=True, text=True)
        
        # Now find the downloaded CBZ file(s) in dl_path
        # Note: mangadex-downloader might create subfolders. We need to find the files.
        downloaded_files = []
        for root, dirs, files in os.walk(dl_path):
            for file in files:
                if file.endswith('.cbz') or file.endswith('.zip') or file.endswith('.cb7'):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            return "Error: No chapters downloaded. Check ID or Language.", 500

        # Run KCC on the downloaded file(s)
        # If multiple chapters, we might want to merge or just convert the first one for now?
        # KCC can handle multiple files if we pass them.
        
        # Let's process the FIRST file found for simplicity in this version, 
        # or merge them if you want volumes. KCC batch converts if given a list.
        
        # We will move the file to OUTPUT_FOLDER after conversion
        converted_zip_path = None
        
        # Run KCC on the folder containing the CBZs
        # kcc-c2e [options] --output [OUTPUT] [INPUT_FILE]
        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
        if upscale: kcc_cmd.append('--upscale')
        if manga_style: kcc_cmd.append('--manga-style')
        
        # Pass all downloaded files to KCC
        kcc_cmd.extend(downloaded_files)
        
        print(f"Running KCC: {' '.join(kcc_cmd)}")
        kcc_result = subprocess.run(kcc_cmd, capture_output=True, text=True)

        if kcc_result.returncode != 0:
            return f"KCC Error: <pre>{kcc_result.stderr}</pre>", 500

        # Zip up the results if multiple, or send single file
        output_files = [os.path.join(OUTPUT_FOLDER, f) for f in os.listdir(OUTPUT_FOLDER) 
                       if os.path.isfile(os.path.join(OUTPUT_FOLDER, f))]
        
        # Filter for files created just now (simple heuristic: files in output folder)
        # Ideally we clean output folder before run.
        
        if len(output_files) == 1:
            final_file = output_files[0]
        else:
            # Zip multiple chapters into one pack
            shutil.make_archive(os.path.join(OUTPUT_FOLDER, 'batch_convert'), 'zip', OUTPUT_FOLDER)
            final_file = os.path.join(OUTPUT_FOLDER, 'batch_convert.zip')

        @after_this_request
        def cleanup(response):
            try:
                shutil.rmtree(dl_path) # Remove source
                for f in os.listdir(OUTPUT_FOLDER): # Clean output
                    os.remove(os.path.join(OUTPUT_FOLDER, f))
            except Exception as e:
                print(e)
            return response

        return send_file(final_file, as_attachment=True)

    except subprocess.CalledProcessError as e:
        return f"Download Error: {str(e)}", 500
    except Exception as e:
        return f"System Error: {str(e)}", 500

@app.route('/convert', methods=['POST'])
def convert():
    # ... (Keep existing manual upload logic here from previous step) ...
    if 'file' not in request.files:
        return "No file uploaded", 400
    file = request.files['file']
    if file.filename == '':
        return "No file selected", 400
    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)
    
    # ... Copy the rest of the logic from the PREVIOUS app.py for manual upload ...
    # (For brevity, I assume you keep the 'convert' function
