import os
import subprocess
import shutil
import requests
import sys
from flask import Flask, render_template, request, send_file, jsonify, after_this_request

# STARTUP LOGGING
print("--- KCC WEB IS STARTING ---")
print(f"Python: {sys.version}")
print("Checking for KCC...", end=" ")
try:
    subprocess.run(['kcc-c2e', '--version'], check=True, capture_output=True)
    print("OK")
except Exception as e:
    print(f"FAIL: {e}")

print("Checking for KindleGen...", end=" ")
if os.path.exists("/usr/local/bin/kindlegen"):
    print("OK (Found at /usr/local/bin/kindlegen)")
else:
    print("WARNING: KindleGen not found!")

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
    
    url = "https://api.mangadex.org/manga"
    params = {
        'title': query,
        'limit': 10,
        'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'],
        'order[relevance]': 'desc'
    }
    
    try:
        r = requests.get(url, params=params)
        data = r.json()
        results = []
        for manga in data['data']:
            title = manga['attributes']['title'].get('en') or list(manga['attributes']['title'].values())[0]
            desc = manga['attributes']['description'].get('en', 'No description')
            results.append({'id': manga['id'], 'title': title, 'desc': desc[:200] + '...'})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/auto_convert', methods=['POST'])
def auto_convert():
    manga_id = request.form.get('manga_id')
    selection_type = request.form.get('selection_type')
    chapter_start = request.form.get('chapter_start')
    chapter_end = request.form.get('chapter_end')
    lang = request.form.get('lang', 'en')

    profile = request.form.get('profile', 'KV')
    format_type = request.form.get('format', 'MOBI')
    upscale = request.form.get('upscale') == 'on'
    manga_style = request.form.get('manga_style') == 'on'

    dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
    if os.path.exists(dl_path): shutil.rmtree(dl_path)
    
    # 1. Download
    cmd_dl = [
        'mangadex-downloader', 
        f"https://mangadex.org/title/{manga_id}",
        '--language', lang,
        '--folder', dl_path,
        '--no-group-name',
        '--save-as', 'cbz'
    ]
    if selection_type == 'range':
        if chapter_start: cmd_dl.extend(['--start-chapter', chapter_start])
        if chapter_end: cmd_dl.extend(['--end-chapter', chapter_end])

    try:
        print(f"Downloading: {cmd_dl}")
        subprocess.run(cmd_dl, check=True, text=True)
        
        # 2. Find Files
        downloaded_files = []
        for root, dirs, files in os.walk(dl_path):
            for file in files:
                if file.endswith(('.cbz', '.zip', '.cb7')):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            return "Error: No chapters downloaded.", 500

        # 3. Convert
        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
        if upscale: kcc_cmd.append('--upscale')
        if manga_style: kcc_cmd.append('--manga-style')
        kcc_cmd.extend(downloaded_files)
        
        print(f"Converting: {kcc_cmd}")
        kcc_result = subprocess.run(kcc_cmd, capture_output=True, text=True)
        if kcc_result.returncode != 0:
            return f"KCC Error: <pre>{kcc_result.stderr}</pre>", 500

        # 4. Bundle & Send
        output_files = [os.path.join(OUTPUT_FOLDER, f) for f in os.listdir(OUTPUT_FOLDER) 
                       if os.path.isfile(os.path.join(OUTPUT_FOLDER, f)) and not f.endswith('.zip')]
        
        # Simple Logic: If 1 file, send it. If multiple, zip them.
        final_file = output_files[0] # Default to first
        
        if len(output_files) > 1:
            shutil.make_archive(os.path.join(OUTPUT_FOLDER, 'batch_convert'), 'zip', OUTPUT_FOLDER)
            final_file = os.path.join(OUTPUT_FOLDER, 'batch_convert.zip')
        elif len(output_files) == 1:
            final_file = output_files[0]
        else:
             return f"Conversion finished but no output found. Log: {kcc_result.stdout}", 500

        @after_this_request
        def cleanup(response):
            try:
                shutil.rmtree(dl_path)
                for f in os.listdir(OUTPUT_FOLDER):
                    os.remove(os.path.join(OUTPUT_FOLDER, f))
            except Exception as e: print(e)
            return response

        return send_file(final_file, as_attachment=True)

    except Exception as e:
        return f"System Error: {str(e)}", 500

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files: return "No file", 400
    file = request.files['file']
    if file.filename == '': return "No file", 400

    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    profile = request.form.get('profile', 'KV')
    format_type = request.form.get('format', 'MOBI')
    upscale = request.form.get('upscale') == 'on'
    manga_style = request.form.get('manga_style') == 'on'

    cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
    if upscale: cmd.append('--upscale')
    if manga_style: cmd.append('--manga-style')
    cmd.append(input_path)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0: return f"Error: {result.stderr}", 500

    # Find result
    base_name = os.path.splitext(file.filename)[0]
    # Rough search for the file
    converted_file = None
    for f in os.listdir(OUTPUT_FOLDER):
        if base_name in f and (f.endswith('.mobi') or f.endswith('.epub') or f.endswith('.azw3')):
             converted_file = os.path.join(OUTPUT_FOLDER, f)
             break
    
    if converted_file:
        @after_this_request
        def cleanup(response):
            os.remove(input_path)
            os.remove(converted_file)
            return response
        return send_file(converted_file, as_attachment=True)
    return f"Error: Output not found. Log: {result.stdout}", 500

if __name__ == '__main__':
    # Host 0.0.0.0 is MANDATORY for Docker
    app.run(host='0.0.0.0', port=5000)
