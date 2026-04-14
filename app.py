import os, json, uuid, requests, base64
import gc
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, jsonify
from snooker_engine import analyze_snooker_image
from datetime import datetime

app = Flask(__name__)

# ============================================================================
# Configuration
# ============================================================================
# 1. Fill in your actual ImgBB API Key
IMGBB_API_KEY = "db288aa13d920fd7b1603e7bc2866159" 
# 2. Ensure 'firebase-key.json' is in the same directory as this script
try:
    cred = credentials.Certificate("firebase-key.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Firebase Initialization Error: {e}")

def upload_to_imgbb(image_path):
    """Uploads a local image file to ImgBB and returns the hosted cloud URL."""
    try:
        with open(image_path, "rb") as file:
            url = "https://api.imgbb.com/1/upload"
            payload = {
                "key": IMGBB_API_KEY,
                "image": base64.b64encode(file.read()),
            }
            res = requests.post(url, data=payload)
            if res.status_code == 200:
                return res.json()['data']['url']
            else:
                print(f"ImgBB Upload Failed: {res.text}")
                return None
    except Exception as e:
        print(f"ImgBB Upload Error: {e}")
        return None

@app.route('/analyze_shot', methods=['POST'])
def analyze_shot():
    # Clean up static folder before processing new request
    request_id = uuid.uuid4().hex
    
    # Temporary path for the incoming raw input image
    in_path = f"temp_in_{request_id}.jpg"
    
    # Store original cloud URL
    original_cloud_url = None

    try:
        file = request.files['image']
        corners = json.loads(request.form.get('corners'))
        user_uid = request.form.get('uid', 'anonymous') # Receive UID from Flutter
        file.save(in_path)
        
        # --- PHASE 1: Upload Original Raw Image to Cloud ---
        # So we can display the before/after in history
        original_cloud_url = upload_to_imgbb(in_path)

        # --- PHASE 2: Run Snooker AI Engine ---
        # The engine saves result images temporarily to local disk
        # with prefix f"tmp_{request_id}_out"
        result = analyze_snooker_image(in_path, corners, f"tmp_{request_id}_out")
        
        if result["status"] == "success":
            
            # Internal helper: Bulk upload images from a shot list and return URLs, difficulties, and ACCURACIES
            def process_and_cloud_sync(shots):
                urls = []
                difficulties = []
                accuracies = [] 
                
                # Pre-process: Extract all valid float difficulties for comparison
                diff_values = []
                for s in shots:
                    try:
                        diff_values.append(float(str(s.get('difficulty', '')).replace('%', '')))
                    except ValueError:
                        diff_values.append(999.0) # Assign extremely high difficulty for invalid values

                for i, s in enumerate(shots):
                    local_img = s['image_path']
                    cloud_url = upload_to_imgbb(local_img)
                    if cloud_url:
                        s['image_url'] = cloud_url 
                        urls.append(cloud_url)
                        
                        diff_str = str(s.get('difficulty', 'N/A'))
                        difficulties.append(diff_str)

                        # ============================================================================
                        # [AI Decision Confidence / Accuracy Model]
                        # Principle: Relative Advantage Model
                        # If the current rank's difficulty is much lower than the next rank's,
                        # the advantage is clear, meaning high AI decision confidence.
                        # ============================================================================
                        if diff_str != "N/A" and "%" in diff_str and i < len(diff_values):
                            current_diff = diff_values[i]
                            
                            # [BUG FIX]: Properly handle the boundary condition for the last item.
                            # If it's the last rank, there is no "next rank" to compare against,
                            # so we set next_diff equal to current_diff to avoid an artificial 99% boost.
                            if i + 1 < len(diff_values):
                                next_diff = diff_values[i + 1]
                            else:
                                next_diff = current_diff
                            
                            # Base confidence level set to 85.0%
                            base_confidence = 85.0 
                            
                            # Calculate advantage delta
                            delta = next_diff - current_diff
                            
                            # Calculate bonus based on delta (max 14.0% bonus)
                            bonus = min(14.0, max(0.0, delta * 0.7)) 
                            
                            ai_accuracy = base_confidence + bonus
                            
                            # Slight accuracy bump for the #1 recommended shot
                            if i == 0:
                                ai_accuracy += 2.0
                                
                            # Cap the maximum accuracy at 99.8%
                            ai_accuracy = min(99.8, ai_accuracy)
                            s['accuracy'] = f"{ai_accuracy:.1f}%"
                        else:
                            s['accuracy'] = "N/A"
                            
                        accuracies.append(s.get('accuracy', 'N/A'))
                    
                    # Clean up local tactical image
                    if os.path.exists(local_img): os.remove(local_img)
                
                return shots, urls, difficulties, accuracies 

            # Process Red and Color tactical routes
            red_res, red_urls, red_diffs, red_accs = process_and_cloud_sync(result["red_shots"])
            color_res, color_urls, color_diffs, color_accs = process_and_cloud_sync(result["color_shots"])

            # --- PHASE 3: Build & Save COMPLETE Tactical History to Firebase Firestore ---
            history_record = {
                "uid": user_uid,
                "timestamp": datetime.now(),
                "original_url": original_cloud_url, 
                "red_urls": red_urls,               
                "color_urls": color_urls,           
                "red_difficulties": red_diffs,     
                "color_difficulties": color_diffs, 
                "red_accuracies": red_accs,
                "color_accuracies": color_accs,
                "note": "AI Tactical Analysis Complete"
              }
            
            db.collection("histories").add(history_record)

            return jsonify({
                "status": "success",
                "red_shots": red_res,
                "color_shots": color_res
            })
            
        return jsonify(result), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # Clean up the initial raw uploaded image
        if os.path.exists(in_path): os.remove(in_path)
        # Force garbage collection to free RAM
        gc.collect()

if __name__ == '__main__':
    # Local production suggested threaded=False for stability against CUDA crashes
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=False)