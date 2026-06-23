import os
import json
import uuid
import subprocess
import re
import time
import threading
from flask import Flask, request, jsonify, render_template, Response, send_file
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import yt_dlp
import google.generativeai as genai

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///jobs.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Read Gemini API key from environment
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

os.makedirs('downloads', exist_ok=True)
os.makedirs('outputs', exist_ok=True)

class Job(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    url = db.Column(db.String(500))
    preset = db.Column(db.String(50))
    status = db.Column(db.String(50), default='queued')
    progress = db.Column(db.Integer, default=0)
    result = db.Column(db.Text, default='{}')
    scheduled_time = db.Column(db.String(50), nullable=True)

with app.app_context():
    db.create_all()

FFMPEG_PATH = os.path.join(os.getcwd(), 'ffmpeg')
if os.path.exists(FFMPEG_PATH):
    os.chmod(FFMPEG_PATH, 0o755)
else:
    FFMPEG_PATH = 'ffmpeg'

FFPROBE_PATH = os.path.join(os.getcwd(), 'ffprobe')
if os.path.exists(FFPROBE_PATH):
    os.chmod(FFPROBE_PATH, 0o755)
else:
    FFPROBE_PATH = 'ffprobe'

def run_ffmpeg(cmd):
    full_cmd = f"{FFMPEG_PATH} {cmd}"
    subprocess.run(full_cmd, shell=True, check=True, capture_output=True)

def process_video(job_id, url, preset):
    job = Job.query.get(job_id)
    job.status = 'Downloading video...'
    db.session.commit()

    download_path = f"downloads/{job_id}.mp4"
    try:
        ydl_opts = {'format': 'best[height<=480]', 'outtmpl': download_path, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'Video')
    except Exception as e:
        job.status = f'Download Error: {str(e)[:50]}'
        job.progress = -1
        db.session.commit()
        return

    try:
        cmd = f"{FFPROBE_PATH} -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {download_path}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        duration = float(result.stdout.strip())
        clip_duration = min(60, duration)
        
        output_file = f"outputs/{job_id}_clip_1.mp4"
        ff_cmd = f"-i {download_path} -ss 0 -t {clip_duration} -vf 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2' -c:a aac -y {output_file}"
        run_ffmpeg(ff_cmd)
    except Exception as e:
        job.status = f'Processing Error: {str(e)[:50]}'
        job.progress = -1
        db.session.commit()
        return

    job.progress = 80
    job.status = 'Generating AI Titles...'
    db.session.commit()

    try:
        prompt = f"Source title: '{title}'. Generate 5 high-CTR YouTube Shorts titles and a 150-character description with 3 hashtags. Output as JSON: {{'titles': ['t1','t2','t3','t4','t5'], 'description': 'desc'}}"
        response = model.generate_content(prompt)
        raw = response.text
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            metadata = json.loads(json_match.group())
        else:
            metadata = {"titles": ["Viral Clip!", "Watch This!", "Insane Moment"], "description": "Auto-generated."}
    except:
        metadata = {"titles": ["Clipped Video", "Short Form", "Trending"], "description": "Auto-generated."}

    job.progress = 100
    job.status = 'done'
    job.result = json.dumps({
        'clips': [f"{job_id}_clip_1.mp4"],
        'metadata': [{'title': t, 'description': metadata.get('description', '')} for t in metadata.get('titles', [])]
    })
    db.session.commit()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def start_process():
    data = request.json
    url = data.get('url')
    preset = data.get('preset', 'Shorts')
    job_id = str(uuid.uuid4())[:8]
    new_job = Job(id=job_id, url=url, preset=preset, status='queued')
    db.session.add(new_job)
    db.session.commit()
    thread = threading.Thread(target=process_video, args=(job_id, url, preset))
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/progress/<job_id>')
def progress_stream(job_id):
    def generate():
        while True:
            job = Job.query.get(job_id)
            if not job:
                break
            if job.progress >= 100:
                result_data = json.loads(job.result) if job.result else {}
                yield f"data: {json.dumps({'progress': 100, 'status': 'Done!', 'result': result_data})}\n\n"
                break
            elif job.progress < 0:
                yield f"data: {json.dumps({'progress': -1, 'status': job.status})}\n\n"
                break
            else:
                yield f"data: {json.dumps({'progress': job.progress, 'status': job.status})}\n\n"
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/schedule', methods=['POST'])
def schedule_job():
    data = request.json
    url = data.get('url')
    preset = data.get('preset')
    run_time = data.get('run_date')
    job_id = str(uuid.uuid4())[:8]
    new_job = Job(id=job_id, url=url, preset=preset, status='scheduled', scheduled_time=run_time)
    db.session.add(new_job)
    db.session.commit()
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=process_video, trigger='date', run_date=run_time, args=[job_id, url, preset])
    scheduler.start()
    return jsonify({'message': 'Scheduled!', 'job_id': job_id})

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(f"outputs/{filename}", as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
