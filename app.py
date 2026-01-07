import os
import subprocess
import shutil
import requests
import sys
import time
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context

app = Flask(__name__)
UPLOAD_FOLDER = '/tmp/kcc_uploads'
OUTPUT_FOLDER = '/tmp/kcc_output'
DOWNLOAD_FOLDER = '/tmp/kcc_downloads'

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

def run_command_with_retry(cmd, max_retries=3):
    """Runs a command and retries if it fails."""
    attempt = 0
    while attempt < max_retries:
        try:
            # We use Popen to stream output real-time
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                yield line
            process.wait()
            if process.returncode == 0:
                return
            else:
                yield f"WARNING: Process failed with code {process.returncode}. Retrying ({attempt+1}/{max_retries})..."
        except Exception as e:
            yield f"ERROR: System execution failed: {str(e)}"
        attempt += 1
        time.sleep(2) # Wait 2s before retry
    yield "FAILURE: Max retries reached. Check logs."

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search_manga():
    query = request.form.get('query')
    if not query: return jsonify({'error': 'No query provided'}), 400
    
    url = "https://api.mangadex.org/manga"
    params = {'title': query, 'limit': 10, 'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'], 'order[relevance]': 'desc'}
    
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

@app.route('/stream_convert')
def stream_convert():
    def generate():
        manga_id = request.args.get('manga_id')
        manga_title = request.args.get('manga_title')
        # We now use Volume Range instead of Chapters
        vol_start = request.args.get('vol_start')
        vol_end = request.args.get('vol_end')
        
        profile = request.args.get('profile', 'KV')
        format_type = request.args.get('format', 'MOBI')
        upscale = request.args.get('upscale') == 'true'
        manga_style = request.args.get('manga_style') == 'true'

        dl_path = os.path.join(DOWNLOAD_FOLDER, manga_id)
        # Clean previous runs to prevent "batchconvert" ghosts
        if os.path.exists(dl_path): shutil.rmtree(dl_path)
        if os.path.exists(OUTPUT_FOLDER): 
            shutil.rmtree(OUTPUT_FOLDER)
            os.makedirs(OUTPUT_FOLDER)
        
        yield "data: STATUS: Initializing... \n\n"

        # 1. Download Command (Volume Mode)
        cmd_dl = [
            'mangadex-downloader', 
            f"https://mangadex.org/title/{manga_id}",
            '--language', 'en',
            '--folder', dl_path,
            '--no-group-name',
            '--save-as', 'cbz' # Keep CBZ so KCC handles it best
        ]
        
        # Download specific volumes
        if vol_start and vol_end:
             cmd_dl.extend(['--start-volume', vol_start, '--end-volume', vol_end])
        elif vol_start:
             cmd_dl.extend(['--start-volume', vol_start])

        # Stream Download Logs
        yield "data: STATUS: Downloading Volumes... \n\n"
        for line in run_command_with_retry(cmd_dl):
            yield f"data: LOG: {line.strip()}\n\n"

        # 2. Check Files
        downloaded_files = []
        for root, dirs, files in os.walk(dl_path):
            for file in files:
                if file.endswith(('.cbz', '.zip', '.cb7')):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            yield "data: ERROR: No English volumes found to download.\n\n"
            return

        yield "data: STATUS: Converting to Kindle format... \n\n"

        # 3. Convert Command
        # --nopanelview prevents the "4 corners" jumping
        # --spreadsplitter helps split double-pages correctly
        kcc_cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER, '--nopanelview', '--spreadsplitter']
        
        if upscale: kcc_cmd.append('--upscale')
        if manga_style: kcc_cmd.append('--manga-style')
        kcc_cmd.extend(downloaded_files)
        
        for line in run_command_with_retry(kcc_cmd):
             yield f"data: LOG: {line.strip()}\n\n"

        # 4. Finish & Rename
        output_files = [f for f in os.listdir(OUTPUT_FOLDER) if f.endswith('.mobi') or f.endswith('.epub') or f.endswith('.azw3')]
        
        if output_files:
             # If multiple volumes, zip them nicely. If single, just send it.
             if len(output_files) == 1:
                 final_filename = output_files[0]
                 # Rename to include Manga Title if possible (KCC sometimes strips it)
                 if manga_title and manga_title not in final_filename:
                     new_name = f"{manga_title} - {final_filename}"
                     os.rename(os.path.join(OUTPUT_FOLDER, final_filename), os.path.join(OUTPUT_FOLDER, new_name))
                     final_filename = new_name
                 
                 yield f"data: DONE: {final_filename}\n\n"
             else:
                 # Zip multiple volumes
                 zip_name = f"{manga_title} - Volumes {vol_start}-{vol_end}.zip"
                 shutil.make_archive(os.path.join(OUTPUT_FOLDER, 'bundle'), 'zip', OUTPUT_FOLDER)
                 # Move zip to output folder root for clean sending
                 final_path = os.path.join(OUTPUT_FOLDER, zip_name)
                 os.rename(os.path.join(OUTPUT_FOLDER, 'bundle.zip'), final_path)
                 yield f"data: DONE: {zip_name}\n\n"
        else:
             yield "data: ERROR: Conversion failed, no output file.\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/download_file/<filename>')
def download_file(filename):
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
