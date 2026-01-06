import os
import subprocess
import shutil
from flask import Flask, render_template, request, send_file, after_this_request

app = Flask(__name__)
UPLOAD_FOLDER = '/tmp/kcc_uploads'
OUTPUT_FOLDER = '/tmp/kcc_output'

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return "No file uploaded", 400
    
    file = request.files['file']
    if file.filename == '':
        return "No file selected", 400

    # Save uploaded file
    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    # Get form options
    profile = request.form.get('profile', 'KV')  # Default to Kindle Voyage/Paperwhite
    format_type = request.form.get('format', 'MOBI')
    upscale = request.form.get('upscale') == 'on'
    manga_style = request.form.get('manga_style') == 'on'

    # Build KCC command
    # kcc-c2e [options] input_file
    cmd = ['kcc-c2e', '-p', profile, '-f', format_type, '--output', OUTPUT_FOLDER]
    
    if upscale:
        cmd.append('--upscale')
    if manga_style:
        cmd.append('--manga-style')
    
    cmd.append(input_path)

    try:
        # Run KCC
        print(f"Running command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return f"Error converting: <pre>{result.stderr}</pre>", 500

        # Find the output file
        # KCC creates the file with the new extension in the output folder
        base_name = os.path.splitext(file.filename)[0]
        # We need to find the file because KCC might rename it slightly (e.g. adding _kcc)
        output_files = os.listdir(OUTPUT_FOLDER)
        converted_file = None
        
        for f in output_files:
            # Simple heuristic: matches base name and has correct extension
            if f.startswith(base_name):
                converted_file = os.path.join(OUTPUT_FOLDER, f)
                break
        
        if not converted_file:
            # Fallback: check if standard naming was used
            expected_ext = 'mobi' if format_type == 'MOBI' else 'epub' if format_type == 'EPUB' else 'cbz'
            converted_file = os.path.join(OUTPUT_FOLDER, f"{base_name}.{expected_ext}")

        if os.path.exists(converted_file):
            # Schedule cleanup after sending
            @after_this_request
            def cleanup(response):
                try:
                    os.remove(input_path)
                    os.remove(converted_file)
                except Exception as e:
                    print(f"Error cleaning up: {e}")
                return response

            return send_file(converted_file, as_attachment=True)
        else:
            return f"Conversion failed. Output file not found. Log: <pre>{result.stdout}</pre>", 500

    except Exception as e:
        return f"System Error: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
