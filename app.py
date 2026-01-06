import os
import subprocess
from flask import Flask, render_template, request, send_file, after_this_request

app = Flask(__name__)

# Config
UPLOAD_FOLDER = '/app/uploads'
PROCESSED_FOLDER = '/app/processed'

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

    input_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(input_path)

    # Settings
    device = request.form.get('device', 'KPW5')
    format_type = request.form.get('format', 'MOBI')
    
    # ---------------------------------------------------------
    # CALLING KCC DIRECTLY (No path needed!)
    # ---------------------------------------------------------
    cmd = ['kcc-c2e'] 
    cmd.extend(['-p', device])
    cmd.extend(['-f', format_type])
    cmd.extend(['-o', PROCESSED_FOLDER])
    
    if 'manga_mode' in request.form:
        cmd.append('-m')
    if 'upscale' in request.form:
        cmd.append('-u')
        
    cmd.append(input_path)

    try:
        subprocess.run(cmd, check=True)
        
        # Handle Output File
        base_name = os.path.splitext(file.filename)[0]
        # KCC auto-names output. We search the folder to be safe.
        output_filename = f"{base_name}.{format_type.lower()}"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        
        # Fallback search if KCC renamed it slightly
        if not os.path.exists(output_path):
             for f in os.listdir(PROCESSED_FOLDER):
                 if f.startswith(base_name) and f.endswith(format_type.lower()):
                     output_path = os.path.join(PROCESSED_FOLDER, f)
                     break

        # Cleanup Input
        if os.path.exists(input_path):
            os.remove(input_path)

        # Serve & Cleanup Output
        @after_this_request
        def remove_file(response):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                print(f"Error cleaning up: {e}")
            return response

        return send_file(output_path, as_attachment=True)

    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
