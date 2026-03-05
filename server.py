import sys
import importlib.metadata

# --- PATCH ULTRALYTICS BEFORE IMPORTING IT ---
# Prevents it from auto-installing standard 'onnxruntime' over our 'onnxruntime-directml'
try:
    import ultralytics.utils.checks
    
    def patched_check_requirements(requirements=(), exclude=(), install=True, cmds=""):
        reqs_list = []
        if isinstance(requirements, str):
            reqs_list = [requirements]
        elif isinstance(requirements, (list, tuple)):
            reqs_list = requirements
            
        if any('onnxruntime' in req for req in reqs_list):
            try:
                importlib.metadata.version('onnxruntime-directml')
                print("DirectML runtime detected. Skipping standard onnxruntime check/install.")
                return True 
            except importlib.metadata.PackageNotFoundError:
                pass
        
        # Original logic (we can't easily call original if we haven't saved references, but let's assume standard behavior)
        # Actually safer to call original via module since we modify module variable
        # But we must save original FIRST.
        pass

    if not hasattr(ultralytics.utils.checks, '_original_check_requirements'):
        ultralytics.utils.checks._original_check_requirements = ultralytics.utils.checks.check_requirements
        
    def final_patched_check(requirements=(), exclude=(), install=True, cmds=""):
        if patched_check_requirements(requirements, exclude, install, cmds) is True:
            return True
        return ultralytics.utils.checks._original_check_requirements(requirements, exclude, install, cmds)

    ultralytics.utils.checks.check_requirements = final_patched_check
    print("Patched Ultralytics requirement checks.")

except Exception as e:
    print(f"Error patching checks: {e}")

from flask import Flask, request, jsonify, send_file, render_template_string, Response, after_this_request, make_response
from ultralytics import YOLO # Import YOLO after patching
import io
import cv2
import numpy as np
import base64
import os
import uuid
import time
import json
import threading
import onnxruntime as ort
from collections import deque, Counter

# Remove Duplicate Patch Block if present later...
# Check if prev block exists and delete it.


# --- MONKEY PATCH TO FORCE AMDS DIRECTML (GPU) ---
# Ultralytics doesn't auto-select DirectML, so we force it here before loading the model.
try:
    _original_inference_session = ort.InferenceSession

    class PatchedInferenceSession(_original_inference_session):
        def __init__(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kwargs):
            available = ort.get_available_providers()
            if 'DmlExecutionProvider' in available:
                if providers is None:
                    providers = ['DmlExecutionProvider']
                elif isinstance(providers, list) and 'DmlExecutionProvider' not in providers:
                    providers.insert(0, 'DmlExecutionProvider')
                
                print(f"Force-Enabled DirectML (AMD GPU) for ONNX Session. Providers: {providers}")
            
            # Call original constructor
            super().__init__(path_or_bytes, sess_options=sess_options, providers=providers, provider_options=provider_options, **kwargs)

    # Apply Patch
    ort.InferenceSession = PatchedInferenceSession
    print("Applied DirectML Monkey Patch for AMD GPU Support.")
except Exception as e:
    print(f"Could not apply DirectML patch: {e}")
# -------------------------------------------------

app = Flask(__name__)

# Global Job Store
jobs = {}

class SnookerGameTracker:
    def __init__(self, fps=30):
        self.fps = fps
        self.player_scores = {1: 0, 2: 0} # Player 1 and 2
        self.current_player = 1
        self.current_break = 0
        self.history = deque(maxlen=max(int(fps), 5)) # Set minimum size constraints for stability overrides
        self.last_stable_counts = {}
        
        # State Machine States: 'AwaitingBreak', 'RedBallActive', 'ColourNomination', 'AwaitingRespot', 'ClearancePhase', 'FoulHandler'
        self.state = 'RedBallActive' 
        self.pending_respot_color = None # The color we are waiting to be respotted
        
        self.points_map = {
             'red-ball': 1, 'yellow-ball': 2, 'green-ball': 3,
             'brown-ball': 4, 'blue-ball': 5, 'pink-ball': 6,
             'black-ball': 7
        }
        # Potted counts per player
        self.potted_counts = {
            1: {k: 0 for k in self.points_map.keys()},
            2: {k: 0 for k in self.points_map.keys()}
        }
        
        # For Clearance Phase
        self.clearance_order = ['yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball']
        self.next_clearance_target = 0 # Index in clearance_order

        # Debouncing / Persistence Logic
        self.pending_changes = {}  # Stores {key: {'target': val, 'frames': count}}
        
        # DYNAMIC THRESHOLDS to prevent Flicker Fouls
        # User requested: 1.5 seconds for a ball missing to be considered potted.
        self.THRESHOLDS = {
            'red-ball': int(fps * 1.5),  # 1.5 seconds confirmation for reds
            'default': int(fps * 1.5)    # 1.5 seconds confirmation for colors
        }
        
        # Start-up Calibration
        self.calibration_frames = 0
        self.max_calibration_frames = int(fps * 1.5) # 1.5 seconds of stability
        self.is_calibrated = False

    def get_stable_counts(self):
        if not self.history:
            return {}
        
        # Get the most frequent count for each ball type in history
        stable = {}
        # Union of all keys seen in history
        all_keys = set().union(*self.history)
        
        for key in all_keys:
            counts = [frame.get(key, 0) for frame in self.history]
            if counts:
                # Require a stronger consensus (at least 50% of frames must agree)
                # This prevents a flickering "0" from becoming the mode if "1" is present in 40% of frames
                counter = Counter(counts)
                mode, freq = counter.most_common(1)[0]
                
                # If history is full, ensure at least 50% consistency
                if len(self.history) >= (self.fps // 2) and (freq / len(self.history) < 0.5):
                     # Not stable enough, keep previous if possible or default to max (assume ball exists)
                     # For safety in limited visibility, we bias towards "Ball Exists" (higher count) to prevent false pots
                     stable[key] = max(counts) 
                else:
                     stable[key] = mode
            
        return stable

    def switch_turn(self):
        self.current_player = 2 if self.current_player == 1 else 1
        self.current_break = 0
        print(f"Turn Switched. Now Player {self.current_player}")

    def get_max_points_remaining(self):
        if not self.is_calibrated:
            return 0
        
        reds = self.last_stable_counts.get('red-ball', 0)
        if reds > 0:
            return (reds * 8) + 27
        else:
            # Sum of remaining colors based on what's still visible
            score = 0
            for color in self.clearance_order:
                if self.last_stable_counts.get(color, 0) > 0:
                     score += self.points_map[color]
            return score

    def update(self, current_frame_counts):
        # 1. Add to history buffer
        # IMPORTANT: Store a COPY so that subsequent modifications to current_frame_counts (like adding strings) don't pollute history
        self.history.append(current_frame_counts.copy())
        
        # Need full buffer to filter noise (skip if fps is extremely low like live tracking)
        if self.fps >= 5 and len(self.history) < 5:
            return {
                'player1_score': self.player_scores[1],
                'player2_score': self.player_scores[2],
                'current_player': self.current_player,
                'break': self.current_break, 
                'phase': self.state, 
                'potted_1': self.potted_counts[1], # Return explicitly
                'potted_2': self.potted_counts[2], # Return explicitly
                'points_remaining': self.get_max_points_remaining()
            }

        # 2. Get Stable State form History (Short-term smoothing)
        current_stable = self.get_stable_counts()
        
        # --- CALIBRATION PHASE ---
        if not self.is_calibrated:
            if not self.last_stable_counts:
                self.last_stable_counts = current_stable
            else:
                 # Check if stable matches last stable
                 match = True
                 relevant_keys = ['red-ball', 'yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball', 'white-ball']
                 for key in relevant_keys:
                     if self.last_stable_counts.get(key, 0) != current_stable.get(key, 0):
                         match = False
                         break
                 
                 if match:
                     self.calibration_frames += 1
                 else:
                     self.calibration_frames = 0
                     self.last_stable_counts = current_stable
            
            if self.calibration_frames >= self.max_calibration_frames:
                self.is_calibrated = True
                
                # Calculate maximum possible points remaining on table
                red_count = self.last_stable_counts.get('red-ball', 0)
                # Max points: For each red (1 point), you can pot a black (7 points) = 8 points per red.
                # Plus the colors: 2 (yellow) + 3 (green) + 4 (brown) + 5 (blue) + 6 (pink) + 7 (black) = 27 points.
                self.max_points_remaining = (red_count * 8) + 27
                self.initial_points_on_table = self.max_points_remaining
                
                print(f"CALIBRATION COMPLETE. Initial State: {self.last_stable_counts}")
                print(f"Reds detected: {red_count}")
                print(f"Maximum possible points on table: {self.max_points_remaining}")

            return {
                'player1_score': self.player_scores[1],
                'player2_score': self.player_scores[2],
                'current_player': self.current_player,
                'break': self.current_break, 
                'phase': "Calibrating...", 
                'potted_1': self.potted_counts[1], # Return explicitly
                'potted_2': self.potted_counts[2], # Return explicitly
                'points_remaining': self.get_max_points_remaining()
            }

        # Be sure to track all relevant keys
        relevant_keys = ['red-ball', 'yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball', 'white-ball']

        # 3. Check for Persisted Changes (Long-term stability)
        for key in relevant_keys:
            target_val = current_stable.get(key, 0)
            current_val = self.last_stable_counts.get(key, 0)
            
            if target_val == current_val:
                # No change pending
                if key in self.pending_changes:
                    del self.pending_changes[key]
                continue
            
            # Change detected: Check if it's new or existing
            if key in self.pending_changes and self.pending_changes[key]['target'] == target_val:
                self.pending_changes[key]['frames'] += 1
            else:
                # Start verify timer
                self.pending_changes[key] = {'target': target_val, 'frames': 1}
            
            # Check Threshold (Dynamic)
            limit = self.THRESHOLDS.get(key, self.THRESHOLDS['default'])
            
            # If target is HIGHER (Ball Appeared/Respot), confirm faster
            if target_val > current_val:
                limit = 20 # 0.6s to recognize a ball reappeared
             
            if self.pending_changes[key]['frames'] >= limit:
                # CONFIRMED CHANGE!
                print(f"CONFIRMED CHANGE for {key}: {current_val} -> {target_val}")
                
                # --- Execute Logic Based on Change ---
                if key == 'red-ball':
                     if target_val < current_val:
                        # Pot Red
                        count_diff = current_val - target_val
                        points = count_diff * 1
                        
                        self.player_scores[self.current_player] += points
                        self.current_break += points
                        self.potted_counts[self.current_player]['red-ball'] += count_diff 
                        
                        # Transition Logic
                        self.state = 'ColourNomination'
                        
                        print(f"POT: {count_diff} Red(s). P{self.current_player}: {self.player_scores[self.current_player]}. State: {self.state}")
                     
                     elif target_val > current_val:
                        # Red(s) appeared (Respot/Error correction)
                        print(f"Red(s) Appeared (+{target_val - current_val})")

                elif key == 'white-ball':
                     if target_val < current_val:
                        # White Ball Potted -> Foul Check
                        # User Rule: Must be gone for 2 seconds (handled by POT_THRESHOLD=80).
                        print(f"FOUL: White Ball Potted (Count {current_val}->{target_val}). Penalty: -4.")
                        self.player_scores[self.current_player] -= 4
                        self.switch_turn()
                        self.state = 'AwaitingRespot'
                        self.pending_respot_color = 'white-ball'

                else:
                     # Colors
                     if target_val < current_val:
                         # Pot Color
                         valid_pot = False
                         is_foul = False
                         color = key
                         
                         if self.state == 'ColourNomination':
                             valid_pot = True
                             self.state = 'AwaitingRespot'
                             self.pending_respot_color = color
                             print(f"POT: {color}. Waiting for Respot...")
                         
                         elif self.state == 'RedBallActive':
                              # FOUL: Potted a color when a Red was expected
                              print(f"FOUL: Potted {color} when Red expected. Penalty: -4.")
                              self.player_scores[self.current_player] -= 4
                              self.switch_turn()
                              self.state = 'AwaitingRespot'
                              self.pending_respot_color = color
                              is_foul = True

                         elif self.state == 'ClearancePhase':
                             # In Clearance Phase, colors must be potted in strict order:
                             # Yellow(2) -> Green(3) -> Brown(4) -> Blue(5) -> Pink(6) -> Black(7)
                             if self.next_clearance_target < len(self.clearance_order):
                                 expected_color = self.clearance_order[self.next_clearance_target]
                                 
                                 if color == expected_color:
                                     valid_pot = True
                                     print(f"CLEARANCE POT: {color} (Correct Sequence).")
                                     if self.next_clearance_target < len(self.clearance_order) - 1:
                                         self.next_clearance_target += 1
                                     else:
                                         print("Frame Cleared! All colors potted.")
                                 else:
                                     print(f"FOUL: Expected {expected_color}, Potted {color}")
                                     self.player_scores[self.current_player] -= 4 
                                     self.switch_turn()
                                     is_foul = True
                         
                         # Award Valid Pot
                         if valid_pot and not is_foul:
                             p_val = self.points_map.get(key, 0)
                             self.player_scores[self.current_player] += p_val
                             self.current_break += p_val
                             self.potted_counts[self.current_player][key] += 1
                             print(f"Score Update: P{self.current_player}={self.player_scores[self.current_player]}. State: {self.state}")

                     elif target_val > current_val:
                         # Respot Detected (Color Reappeared)
                         # Specifically handle if we were waiting for THIS color
                         if self.state == 'AwaitingRespot' and self.pending_respot_color == key:
                             curr_reds_on_table = self.last_stable_counts.get('red-ball', 0)
                             
                             if curr_reds_on_table > 0:
                                 self.state = 'RedBallActive'
                                 print(f"RESPOT: {key}. Reds Left -> RedBallActive.")
                             else:
                                 self.state = 'ClearancePhase'
                                 print(f"RESPOT: {key}. No Reds -> ClearancePhase.")
                             
                             self.pending_respot_color = None
                         else:
                             print(f"Color {key} appeared unexpectedly (or general respot).")

                # Update Official State
                self.last_stable_counts[key] = target_val
                
                # Clear pending
                del self.pending_changes[key]

        return {
            'player1_score': self.player_scores[1],
            'player2_score': self.player_scores[2],
            'current_player': self.current_player,
            'break': self.current_break, 
            'phase': self.state, 
            'potted_1': self.potted_counts[1], # Return explicitly
            'potted_2': self.potted_counts[2], # Return explicitly
            'points_remaining': self.get_max_points_remaining()
        }

# Check backend
try:
    print(f"ONNX Providers: {ort.get_available_providers()}")
except:
    pass

# HTML Template for the Web Interface
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Snooker Ball Detector</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; text-align: center; }
        .container { max-width: 800px; margin: 0 auto; }
        .upload-box { border: 2px dashed #ccc; padding: 30px; margin: 20px 0; border-radius: 10px; }
        button { padding: 10px 20px; background-color: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background-color: #218838; }
        img { max-width: 100%; border: 1px solid #ddd; border-radius: 5px; margin-top: 20px; }
        .stats { background: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 20px; text-align: left; display: inline-block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎱 Snooker Ball Detection</h1>
        
        <div class="upload-box">
            <form action="/web_predict" method="post" enctype="multipart/form-data">
                <input type="file" name="file" accept="image/*" required>
                <br><br>
                <button type="submit">Upload & Detect</button>
            </form>
        </div>

        {% if result_image %}
            <h2>Detection Result</h2>
            <img src="data:image/jpeg;base64,{{ result_image }}" alt="Detected Image">
            
            <div class="stats">
                <h3>Ball Counts:</h3>
                <ul>
                    <li>🔴 Red Balls: <strong>{{ stats['red-ball'] }}</strong></li>
                    <li>🎨 Colored Balls: <strong>{{ stats['colored-ball'] }}</strong></li>
                    <li>⚪ Cue Ball: <strong>{{ stats['white-ball'] }}</strong></li>
                </ul>
            </div>
        {% endif %}
    </div>
</body>
</html>
"""

# Load the model once
print("Loading model...")
# Prioritize ONNX for AMD/DirectML support
model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.onnx'
if not os.path.exists(model_path):
    print(f"ONNX model not found at {model_path}, trying PT...")
    model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.pt'

try:
    model = YOLO(model_path, task='detect')
    print(f"Successfully loaded model from: {model_path}")
    print(f"Model classes: {model.names}")

    # --- DEVICE & GPU CHECK ---
    print("-" * 30)
    device = model.device
    print(f"Current Model Device: {device}")
    
    # 1. Check NVIDIA (CUDA)
    import torch
    if torch.cuda.is_available():
        print(f"NVIDIA GPU Detected: {torch.cuda.get_device_name(0)}")
        if str(device) == 'cpu':
             print("WARNING: CUDA is available but model is on CPU. Try: model.to('cuda')")
    else:
        print("No NVIDIA CUDA GPU detected.")

    # 2. Check AMD (DirectML) via ONNX
    try:
        providers = ort.get_available_providers()
        print(f"Available ONNX Providers: {providers}")
        
        has_directml = 'DmlExecutionProvider' in providers
        if has_directml:
            print("SUCCESS: AMD GPU Support Detected (DirectML)!")
            if model_path.endswith('.pt'):
                print("NOTICE: You are using a PyTorch (.pt) model which runs on CPU on Windows with AMD.")
                print("ACTION REQUIRED: To use your AMD GPU, export the model to ONNX:")
                print("Run this python code once: model.export(format='onnx')")
                print("Then restart server.py. It will auto-load the .onnx file if .pt is missing or if logic prefers it.")
    except Exception as e:
        print(f"Could not check ONNX providers: {e}")
    print("-" * 30)

except Exception as e:
    print(f"Error loading model: {e}")
    # Fallback/Exit
    exit(1)

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/web_predict', methods=['POST'])
def web_predict():
    if 'file' not in request.files:
        return "No file uploaded", 400
    
    file = request.files['file']
    if file.filename == '':
        return "No file selected", 400

    # Read image
    img_bytes = file.read()
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    # Run inference
    # agnostic_nms=True prevents multiple class detections for the same object (e.g. Red and Pink on same ball)
    results = model(img, conf=0.25, agnostic_nms=True)
    result = results[0] # First image results

    # Get Counts
    counts = {'red-ball': 0, 'colored-ball': 0, 'white-ball': 0}
    names = result.names
    for box in result.boxes:
        cls_id = int(box.cls[0])
        cls_name = names[cls_id]
        if 'red' in cls_name.lower(): counts['red-ball'] += 1
        elif 'white' in cls_name.lower(): counts['white-ball'] += 1
        else: counts['colored-ball'] += 1

    # Annotate image
    annotated_img = result.plot(conf=False, line_width=1, font_size=1)
    
    # Convert to base64 for HTML display
    _, buffer = cv2.imencode('.jpg', annotated_img)
    img_b64 = base64.b64encode(buffer).decode('utf-8')

    return render_template_string(HTML_TEMPLATE, result_image=img_b64, stats=counts)

@app.route('/predict', methods=['POST'])
def predict_api():
    print("Received prediction request")
    if 'file' not in request.files:
        print("No file part in request")
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    print(f"Processing image: {file.filename}")
    img_bytes = file.read()
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    # Use high resolution for static image prediction too, but match ONNX imgsz
    results = model(img, conf=0.25, agnostic_nms=True, imgsz=1280)
    result = results[0]
    
    # Filter Limits
    limits = {
        'red-ball': 15, 'yellow-ball': 1, 'green-ball': 1,
        'brown-ball': 1, 'blue-ball': 1, 'pink-ball': 1,
        'black-ball': 1, 'white-ball': 1
    }
    
    counts = {}
    keep_indices = []
    
    # Sort by confidence
    boxes_list = []
    for i, box in enumerate(result.boxes):
        boxes_list.append({
            'index': i,
            'conf': float(box.conf),
            'cls': int(box.cls),
            'label': result.names[int(box.cls)].lower()
        })
    
    boxes_list.sort(key=lambda x: x['conf'], reverse=True)
    
    for item in boxes_list:
        lbl = item['label']
        limit = limits.get(lbl, 999)
        current = counts.get(lbl, 0)
        
        if current < limit:
            counts[lbl] = current + 1
            keep_indices.append(item['index'])
             
    if keep_indices:
        result.boxes = result.boxes[keep_indices]
        annotated_img = result.plot(conf=False, line_width=1, font_size=1)
    else:
        annotated_img = img

    _, buffer = cv2.imencode('.jpg', annotated_img)
    return Response(buffer.tobytes(), mimetype='image/jpeg')


@app.route('/frame_detections', methods=['POST'])
def get_frame_detections():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'})

    file = request.files['file']
    unique_id = str(uuid.uuid4())
    temp_input = os.path.abspath(f"temp_frame_{unique_id}.mp4")
    
    try:
        if not file.filename:
            return jsonify({'error': 'No file selected'})

        file.save(temp_input)
        
        cap = cv2.VideoCapture(temp_input)
        ret, frame = cap.read()
        cap.release()
        try:
           os.remove(temp_input) # Clean up immediately
        except:
           pass
        
        if not ret:
            return jsonify({'error': 'Could not read video frame'}), 500

        # Resize for consistency with processing
        width = frame.shape[1]
        height = frame.shape[0]
        target_width = 1280

        if width > target_width:
             new_width = target_width
             aspect_ratio = height / width
             new_height = int(target_width * aspect_ratio)
             frame = cv2.resize(frame, (new_width, new_height))
             width = new_width
             height = new_height
        
        # Run Detection
        results = model(frame, conf=0.25, agnostic_nms=True)
        detections = []
        
        if results:
            result = results[0]
            names = result.names
            for one_box in result.boxes:
                x1, y1, x2, y2 = one_box.xyxy[0].tolist()
                cls_id = int(one_box.cls[0])
                conf = float(one_box.conf[0])
                label = names[cls_id]
                
                detections.append({
                    'rect': [x1, y1, x2, y2],
                    'label': label,
                    'conf': conf
                })
        
        # Encode Frame to Base64
        _, buffer = cv2.imencode('.jpg', frame)
        img_str = base64.b64encode(buffer).decode('utf-8')
        
        return jsonify({
            'image_base64': img_str,
            'detections': detections,
            'width': width,
            'height': height
        })

    except Exception as e:
        if os.path.exists(temp_input):
            try:
                os.remove(temp_input)
            except:
                pass
        return jsonify({'error': str(e)}), 500


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
                target_w = 1280
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

        # Set target width to original width to maintain quality
        target_width = width 
        new_width = width
        new_height = height

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
        tracker = SnookerGameTracker(fps=fps)

        while cap.isOpened():
            # Check for cancellation
            if jobs[job_id].get('status') == 'cancelled':
                print(f"Job {job_id} cancelled by user.")
                break

            ret, frame = cap.read()
            if not ret:
                break
            
            # Resize frame
            if width > target_width:
                frame = cv2.resize(frame, (new_width, new_height))

            # Run YOLO inference
            # Reverted back to the original stable parameters:
            results = model(frame, conf=0.25, agnostic_nms=True, imgsz=1280) 
            
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
                     annotated_frame = result.plot(conf=False, line_width=1, font_size=1)
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
                    current_stats['player1_score'] = track_res['player1_score']
                    current_stats['player2_score'] = track_res['player2_score']
                    current_stats['current_player'] = track_res['current_player']
                    current_stats['current_break'] = track_res['break']
                    current_stats['game_phase'] = track_res['phase']
                    current_stats['potted_score'] = track_res['player1_score'] + track_res['player2_score'] # Approximate total potted sum
                    
                    if 'potted_1' in track_res:
                         for k, v in track_res['potted_1'].items():
                             current_stats[f'p1_potted_{k}'] = v
                    if 'potted_2' in track_res:
                         for k, v in track_res['potted_2'].items():
                             current_stats[f'p2_potted_{k}'] = v
                    
                    current_stats['visible_score'] = vis
                    # Use accurate max potential score based on game state (not just currently visible balls)
                    current_stats['potential_score'] = track_res.get('points_remaining', pot)
                    current_stats['timestamp'] = frame_count / fps if fps > 0 else 0
                    last_stats = current_stats.copy()
                    
                    if frame_count % int(fps) == 0:
                        timeline_stats.append(current_stats)
            
            out.write(annotated_frame)

            frame_count += 1
            if total_frames > 0:
                jobs[job_id]['progress'] = int((frame_count / total_frames) * 100)
                
            if tracker.is_calibrated and 'initial_points' not in jobs[job_id]:
                jobs[job_id]['initial_points'] = tracker.max_points_remaining

        cap.release()
        out.release()
        
        if jobs[job_id].get('status') == 'cancelled':
            if os.path.exists(temp_output):
                try: os.remove(temp_output)
                except: pass
            if os.path.exists(temp_input):
                try: os.remove(temp_input)
                except: pass
            print(f"Job {job_id} cleanup complete.")
            return

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

@app.route('/cancel_job/<job_id>', methods=['POST'])
def cancel_job(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    # Only cancel if it's running or pending
    current_status = jobs[job_id].get('status', '')
    if current_status in ['pending', 'processing']:
        jobs[job_id]['status'] = 'cancelled'
        print(f"Cancellation requested for Job {job_id}")
        return jsonify({'status': 'cancelled'})
    
    return jsonify({'error': f'Job cannot be cancelled (Status: {current_status})'}), 400

@app.route('/job_status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    return jsonify({
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'initial_points': job.get('initial_points'),
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

import uuid
import base64

live_sessions = {}

@app.route('/live/start', methods=['POST'])
def live_start():
    session_id = str(uuid.uuid4())
    # Set to 10 FPS (100ms) - stable for HTTP-based live streaming without lag
    live_sessions[session_id] = SnookerGameTracker(fps=10.0)
    return jsonify({'session_id': session_id})

@app.route('/live/frame', methods=['POST'])
def live_frame():
    session_id = request.form.get('session_id')
    if not session_id or session_id not in live_sessions:
        return jsonify({'error': 'Invalid or expired session'}), 400
        
    tracker = live_sessions[session_id]
    
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file'}), 400
        
    img_bytes = file.read()
    nparr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    # Run YOLO inference
    results = model(frame, conf=0.25, agnostic_nms=True, imgsz=1280)
    result = results[0]
    names = result.names
    
    # Get current counts
    counts = {
        'red-ball': 0, 'yellow-ball': 0, 'green-ball': 0,
        'brown-ball': 0, 'blue-ball': 0, 'pink-ball': 0,
        'black-ball': 0, 'white-ball': 0
    }
    
    for box in result.boxes:
        cls_id = int(box.cls[0])
        key = names[cls_id].lower()
        if key in counts:
             counts[key] += 1
             
    track_res = tracker.update(counts)
    
    # Update track_res with ball counts directly for UI rendering in flutter
    for key in counts:
        track_res[key] = counts[key]
        
    track_res['game_phase'] = track_res.get('phase', 'Unknown')
    track_res['current_break'] = track_res.get('break', 0)
    
    # Flatten potted counts for UI (p1_potted_red-ball, etc) matching video_job logic
    if 'potted_1' in track_res:
         for k, v in track_res['potted_1'].items():
             track_res[f'p1_potted_{k}'] = v
    if 'potted_2' in track_res:
         for k, v in track_res['potted_2'].items():
             track_res[f'p2_potted_{k}'] = v

    # Add total potted score
    track_res['potted_score'] = track_res.get('player1_score', 0) + track_res.get('player2_score', 0)
    # Add potential_score if not present (tracker.update provides points_remaining)
    if 'points_remaining' in track_res:
        track_res['potential_score'] = track_res['points_remaining']

    points_map = {
        'red-ball': 1, 'yellow-ball': 2, 'green-ball': 3,
        'brown-ball': 4, 'blue-ball': 5, 'pink-ball': 6,
        'black-ball': 7, 'white-ball': 0
    }
    vis = 0
    for key, count in counts.items():
        if key in points_map:
            vis += count * points_map[key]
            
    track_res['visible_score'] = vis
    
    # Encode annotated frame to base64
    annotated_frame = result.plot(conf=False, line_width=2, font_size=2)
    _, buffer = cv2.imencode('.jpg', annotated_frame)
    b64_img = base64.b64encode(buffer).decode('utf-8')
    
    return jsonify({
        'stats': track_res,
        'image': b64_img
    })

@app.route('/stats', methods=['POST'])
def stats():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    # Check mapping
    color_mapping = {}
    try:
        mapping_str = request.form.get('color_mapping')
        if mapping_str:
             color_mapping = json.loads(mapping_str)
             print(f"Applied Color Mapping (Stats): {color_mapping}")
    except Exception as e:
        print(f"Error parsing color mapping: {e}")

    file = request.files['file']
    img_bytes = file.read()
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    results = model(img, conf=0.25, agnostic_nms=True)
    result = results[0]
    names = result.names
    
    # Initialize counts for all specific balls
    counts = {
        'red-ball': 0,
        'yellow-ball': 0,
        'green-ball': 0,
        'brown-ball': 0,
        'blue-ball': 0,
        'pink-ball': 0,
        'black-ball': 0,
        'white-ball': 0,
        'total_score': 0
    }
    
    # Snooker Point Values & Limits
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

    names = result.names
    detections = []
    
    # Gather Detections
    for box in result.boxes:
        cls_id = int(box.cls[0])
        cls_name = names[cls_id]
        key = cls_name.lower()
        conf = float(box.conf[0])
        
        # Apply Mapping
        if key in color_mapping:
            key = color_mapping[key]
            
        detections.append({'key': key, 'conf': conf})
    
    # Sort by confidence
    detections.sort(key=lambda x: x['conf'], reverse=True)
    
    # Count with Limits
    temp_counts = {}
    for d in detections:
        key = d['key']
        limit = limits.get(key, 999)
        current_count = temp_counts.get(key, 0)
        
        if current_count < limit:
            temp_counts[key] = current_count + 1
            if key in counts:
                counts[key] += 1
            elif 'ball' in key:
                counts['colored-ball'] = counts.get('colored-ball', 0) + 1
            
    # Calculate Score
    # 1. 'Visible Value': Sum of points of all balls currently seen
    visible_value = 0
    for key, count in counts.items():
        if key != 'total_score' and key in points:
             visible_value += count * points[key]
    
    # 2. 'Potential Remaining': Estimate max points left
    # Assumption: For every Red, you can take a Black (7). Plus all colors (27) at the end.
    # If no reds, just sum the visible colors.
    if counts['red-ball'] > 0:
         potential = (counts['red-ball'] * 8) + 27
    else:
         # If no reds, potential is just the sum of visible colors
         potential = visible_value

    counts['visible_score'] = visible_value
    counts['potential_score'] = potential
    
    return jsonify(counts)

if __name__ == '__main__':
    # host='0.0.0.0' allows access from other devices on the network
    app.run(host='0.0.0.0', port=5000, debug=True)
