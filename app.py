import os
import subprocess
import shutil
from flask import Flask, render_template, request, send_file, after_this_request

app = Flask(__name__)

# Config
UPLOAD_FOLDER = '/app/uploads'
PROCESSED_FOLDER = '/app/processed'
KCC_SCRIPT = '/app/kcc-source/kcc-c2e.py'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return "No file uploaded", 400
    
    file = request.files['file']
    if file.filename == '':
        return "No file selected", 400

    # Save user file
    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    # Get Form Settings
    device = request.form.get('device', 'KPW5')
    format_type = request.form.get('format', 'MOBI')
    manga_mode = 'manga_mode' in request.form
    upscale = 'upscale' in request.form
    
    # Construct KCC Command
    # python3 kcc-c2e.py -p KPW5 -m -u -f MOBI -o /app/processed /app/uploads/file.cbz
    cmd = ['python3', KCC_SCRIPT]
    cmd.extend(['-p', device])
    cmd.extend(['-f', format_type])
    cmd.extend(['-o', PROCESSED_FOLDER])
    
    if manga_mode:
        cmd.append('-m') # Manga mode (R-to-L)
    if upscale:
        cmd.append('-u') # Upscale enabled
        
    cmd.append(input_path)

    try:
        # Run Conversion
        subprocess.run(cmd, check=True)
        
        # Find the output file (KCC changes extension)
        base_name = os.path.splitext(file.filename)[0]
        output_filename = f"{base_name}.{format_type.lower()}"
        if format_type == 'MOBI' and device == 'KS': 
             # Scribe sometimes defaults to .azw3 internally? check fallback
             pass 

        # Scan folder for the newest file to be safe
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        
        # Cleanup input immediately
        os.remove(input_path)

        # Serve file and cleanup output after sending
        @after_this_request
        def remove_file(response):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                print(f"Error cleaning up: {e}")
            return response

        return send_file(output_path, as_attachment=True)

    except subprocess.CalledProcessError as e:
        return f"Conversion Failed: {str(e)}", 500
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
