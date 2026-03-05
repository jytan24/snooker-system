
import os

new_code = r'''
def process_video_job(job_id, temp_input, temp_output, color_mapping, color_overrides):
    try:
        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['progress'] = 0

        cap = cv2.VideoCapture(temp_input)
        if not cap.isOpened():
             print(f"Error: Could not open video at {temp_input}")
             jobs[job_id]['status'] = 'error'
             jobs[job_id]['error'] = f'Could not open video file at {temp_input}'
             return

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames > 0:
             jobs[job_id]['total_frames'] = total_frames
        else:
             jobs[job_id]['total_frames'] = 0 # unknown

        # PRE-PROCESSING: Extract Prototype Colors from Frame 0
        prototypes = []
        if color_overrides:
            ret_pre, frame_pre = cap.read()
            if ret_pre:
                print("Extracting color prototypes from frame 0...")
                target_w = 640
                p_h, p_w = frame_pre.shape[:2]
                
                if p_w > target_w:
                    aspect = p_h / p_w
                    new_w = target_w
                    new_h = int(target_w * aspect)
                    frame_pre = cv2.resize(frame_pre, (new_w, new_h))
                
                hsv_frame = cv2.cvtColor(frame_pre, cv2.COLOR_BGR2HSV)

                for override in color_overrides:
                    try:
                        if 'rect' not in override: continue
                        
                        x1, y1, x2, y2 = override['rect']
                        label = override.get('label')
                        
                        cx1, cy1 = max(0, int(x1)), max(0, int(y1))
                        cx2, cy2 = min(new_w, int(x2)), min(new_h, int(y2))
                        
                        cx1, cy1 = max(0, cx1), max(0, cy1)
                        cx2, cy2 = min(new_w, cx2), min(new_h, cy2)
                        
                        if cx2 > cx1 and cy2 > cy1:
                            roi = hsv_frame[cy1:cy2, cx1:cx2]
                            if roi.size > 0:
                                avg_hsv = cv2.mean(roi)[:3]
                                prototypes.append({
                                    'label': label,
                                    'hsv': avg_hsv
                                })
                                print(f" - Prototype [{label}]: HSV={avg_hsv}")
                            
                    except Exception as e:
                        print(f"Error extracting prototype: {e}")
            
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0) # Reset to start

        target_width = 640
        new_width = target_width
        new_height = int(height * (target_width / width)) if width > 0 else height

        if fps == 0 or np.isnan(fps): fps = 30.0

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(temp_output, fourcc, fps, (new_width, new_height))
        
        timeline_stats = []
        frame_count = 0
        last_stats = {}
        
        # Mapping Score
        points = {
            'red-ball': 1, 'yellow-ball': 2, 'green-ball': 3,
            'brown-ball': 4, 'blue-ball': 5, 'pink-ball': 6,
            'black-ball': 7, 'white-ball': 0
        }
        
        limits = {
            'red-ball': 15, 'yellow-ball': 1, 'green-ball': 1,
            'brown-ball': 1, 'blue-ball': 1, 'pink-ball': 1,
            'black-ball': 1, 'white-ball': 1
        }
        
        # Instantiate Tracker
        tracker = SnookerGameTracker()

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            
            # Resize frame
            if width > target_width:
                frame = cv2.resize(frame, (new_width, new_height))

            # Run YOLO inference
            results = model(frame, conf=0.25, agnostic_nms=True) 
            
            annotated_frame = frame
            
            if results and len(results) > 0:
                result = results[0]
                names = result.names
                has_balls = False
                
                detections = []
                
                hsv_current_frame = None
                if prototypes:
                     hsv_current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                
                for i, box in enumerate(result.boxes):
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])
                    cls_name = names[cls_id]
                    key = cls_name.lower()
                    
                    override_key = None
                    if prototypes and hsv_current_frame is not None:
                        bx1, by1, bx2, by2 = map(int, box.xyxy[0])
                        bw, bh = bx2 - bx1, by2 - by1
                        cx1, cy1 = max(0, bx1 + int(bw * 0.25)), max(0, by1 + int(bh * 0.25))
                        cx2, cy2 = min(frame.shape[1], bx2 - int(bw * 0.25)), min(frame.shape[0], by2 - int(bh * 0.25))
                        
                        if cx2 > cx1 and cy2 > cy1:
                            roi = hsv_current_frame[cy1:cy2, cx1:cx2]
                        if roi.size > 0:
                            curr_hsv = cv2.mean(roi)[:3] 
                            best_match_key = None
                            best_dist = 999
                            for p in prototypes:
                                p_hsv = p['hsv']
                                dh = abs(curr_hsv[0] - p_hsv[0])
                                if dh > 90: dh = 180 - dh
                                ds = abs(curr_hsv[1] - p_hsv[1])
                                dv = abs(curr_hsv[2] - p_hsv[2])
                                dist = (dh * 2.0) + (ds * 0.5) + (dv * 0.2)
                                if dist < best_dist:
                                    best_dist = dist
                                    best_match_key = p['label']

                            if best_dist < 45:
                                override_key = best_match_key

                    if override_key:
                        key = override_key
                    elif key in color_mapping:
                        key = color_mapping[key]
                    
                    detections.append({'key': key, 'conf': conf, 'index': i})
                
                detections.sort(key=lambda x: x['conf'], reverse=True)
                
                current_stats = {
                    'red-ball': 0, 'yellow-ball': 0, 'green-ball': 0,
                    'brown-ball': 0, 'blue-ball': 0, 'pink-ball': 0,
                    'black-ball': 0, 'white-ball': 0, 'colored-ball': 0,
                    'total_score': 0
                }
                
                accepted_indices = []
                
                for d in detections:
                    key = d['key']
                    idx = d['index']
                    
                    added = False
                    if key in current_stats:
                        limit = limits.get(key, 999)
                        if current_stats[key] < limit:
                            current_stats[key] += 1
                            has_balls = True
                            added = True
                    elif 'ball' in key: 
                        current_stats['colored-ball'] += 1
                        has_balls = True
                        added = True
                        
                    if added:
                        accepted_indices.append(idx)
                
                if accepted_indices:
                     result.boxes = result.boxes[accepted_indices]
                     annotated_frame = result.plot()
                else:
                     annotated_frame = frame

                if has_balls:
                    vis = 0
                    for key, count in current_stats.items():
                        if key in points:
                            vis += count * points[key]
                    
                    pot = vis 
                    if current_stats.get('red-ball', 0) > 0:
                        pot = (current_stats['red-ball'] * 8) + 27
                    
                    track_res = tracker.update(current_stats)
                    current_stats['potted_score'] = track_res['game_score']
                    current_stats['current_break'] = track_res['break']
                    current_stats['game_phase'] = track_res['phase']

                    current_stats['visible_score'] = vis
                    current_stats['potential_score'] = pot
                    current_stats['timestamp'] = frame_count / fps if fps > 0 else 0
                    last_stats = current_stats.copy()
                    
                    if frame_count % int(fps) == 0:
                        timeline_stats.append(current_stats)
            
            out.write(annotated_frame)

            frame_count += 1
            if total_frames > 0:
                jobs[job_id]['progress'] = int((frame_count / total_frames) * 100)
            
            if frame_count % 30 == 0:
                print(f"Job {job_id}: Processed {frame_count}/{total_frames} frames ({jobs[job_id]['progress']}%)")

        cap.release()
        out.release()
        
        visible_value = 0
        for key, count in last_stats.items():
            if key in points:
                 visible_value += count * points[key]
        
        if last_stats.get('red-ball', 0) > 0:
             potential = (last_stats['red-ball'] * 8) + 27
        else:
             potential = visible_value

        last_stats['visible_score'] = visible_value
        last_stats['potential_score'] = potential
        
        response_data = {
            'summary': last_stats,
            'timeline': timeline_stats
        }

        jobs[job_id]['stats'] = response_data
        jobs[job_id]['status'] = 'completed'
        print(f"Job {job_id} completed successfully.")

    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        if os.path.exists(temp_input):
            try: os.remove(temp_input)
            except: pass
        if os.path.exists(temp_output):
             try: os.remove(temp_output)
             except: pass

@app.route('/start_video_predict', methods=['POST'])
def start_video_predict():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    unique_filename = str(uuid.uuid4())
    temp_input = f"{unique_filename}_input.mp4"
    temp_output = f"{unique_filename}_output.mp4"
    
    try:
        file.save(temp_input)
    except Exception as e:
         return jsonify({'error': f'Failed to save file: {str(e)}'}), 500
         
    color_mapping = {}
    try:
        mapping_str = request.form.get('color_mapping')
        if mapping_str: color_mapping = json.loads(mapping_str)
    except: pass

    color_overrides = []
    try:
        overrides_str = request.form.get('color_overrides')
        if overrides_str: color_overrides = json.loads(overrides_str)
    except: pass

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'pending',
        'progress': 0,
        'input_path': temp_input,
        'output_path': temp_output,
        'created_at': time.time()
    }
    
    thread = threading.Thread(target=process_video_job, args=(job_id, temp_input, temp_output, color_mapping, color_overrides))
    thread.start()
    
    return jsonify({'job_id': job_id, 'status': 'started'})

@app.route('/job_status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    return jsonify({
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'error': job.get('error')
    })

@app.route('/job_result/<job_id>', methods=['GET'])
def get_job_result(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Job not completed'}), 400
        
    temp_output = job['output_path']
    input_path = job['input_path']
    
    @after_this_request
    def remove_files(response):
        time.sleep(1)
        try:
            if os.path.exists(temp_output): os.remove(temp_output)
            if os.path.exists(input_path): os.remove(input_path) 
        except Exception as e:
            print(f"Error removing files: {e}")
        return response
    
    response = make_response(send_file(temp_output, mimetype='video/mp4'))
    response.headers['X-Snooker-Stats'] = json.dumps(job['stats'])
    return response

# Legacy alias
@app.route('/video_predict', methods=['POST'])
def video_predict():
    return start_video_predict()

'''

with open('server.py', 'r', encoding='utf-8') as f:
    original_lines = f.readlines()

start_index = -1
end_index = -1

for i, line in enumerate(original_lines):
    if "@app.route('/video_predict', methods=['POST'])" in line:
        start_index = i
    if "@app.route('/stats', methods=['POST'])" in line:
        end_index = i
        break

if start_index != -1 and end_index != -1:
    print(f"Replacing lines {start_index} to {end_index}")
    new_lines = original_lines[:start_index] + [new_code] + original_lines[end_index:]
    with open('server.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("server.py updated successfully")
else:
    print("Could not find start/end markers")
