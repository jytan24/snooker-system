import cv2
import numpy as np
from ultralytics import YOLO
import math
import os

# ==========================================
# Configuration Constants
# ==========================================
MODEL_PATH = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.pt'
WIDTH, HEIGHT = 600, 1200
POCKETS = [(0,0), (600,0), (0,600), (600,600), (0,1200), (600,1200)]
BALL_RADIUS_WARPED = 10.0 
COLLISION_THRESHOLD = BALL_RADIUS_WARPED * 1.4 

MAX_DIST = math.sqrt(WIDTH**2 + HEIGHT**2)
MAX_ANGLE = 85.0
THEORETICAL_MAX_SCORE = (MAX_DIST * 1.0) + ((MAX_ANGLE**2) * 0.05) + (MAX_DIST * 0.5)

print("Loading YOLO Model...")
model = YOLO(MODEL_PATH)

def calculate_distance(p1, p2):
    return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

def point_to_line_dist(pt, v1, v2):
    a, b, p = np.array(v1), np.array(v2), np.array(pt)
    ab, ap = b - a, p - a
    dot_ab = np.dot(ab, ab)
    if dot_ab == 0: return np.linalg.norm(p - a)
    t = max(0.0, min(1.0, np.dot(ap, ab) / dot_ab))
    return np.linalg.norm(p - (a + t * ab))

def is_path_blocked(start_pt, end_pt, all_balls, exclude_balls):
    for ball in all_balls:
        if ball in exclude_balls: continue
        if point_to_line_dist(ball, start_pt, end_pt) < COLLISION_THRESHOLD: return True 
    return False

def find_best_pocket_and_score(target_pos, white_pos, all_balls_warped):
    best_score = float('inf')
    best_pocket = None
    dist_white_to_red = calculate_distance(white_pos, target_pos)
    for pocket in POCKETS:
        dist_red_to_pocket = calculate_distance(target_pos, pocket)
        vec_w2r = np.array([target_pos[0]-white_pos[0], target_pos[1]-white_pos[1]])
        vec_r2p = np.array([pocket[0]-target_pos[0], pocket[1]-target_pos[1]])
        norm_w2r, norm_r2p = np.linalg.norm(vec_w2r), np.linalg.norm(vec_r2p)
        if norm_w2r == 0 or norm_r2p == 0: continue
        cos_a = max(-1.0, min(1.0, np.dot(vec_w2r, vec_r2p) / (norm_w2r * norm_r2p))) 
        angle = math.degrees(math.acos(cos_a))
        if angle > MAX_ANGLE: continue 
        unit_p2r = vec_r2p / norm_r2p
        gh_warped = (target_pos[0] - BALL_RADIUS_WARPED*2*unit_p2r[0], target_pos[1] - BALL_RADIUS_WARPED*2*unit_p2r[1])
        if is_path_blocked(white_pos, gh_warped, all_balls_warped, [white_pos, target_pos]): continue
        if is_path_blocked(target_pos, pocket, all_balls_warped, [white_pos, target_pos]): continue
        current_score = (dist_white_to_red * 1.0) + (angle**2 * 0.05) + (dist_red_to_pocket * 0.5)
        if current_score < best_score:
            best_score, best_pocket = current_score, pocket
    return best_pocket, best_score

def draw_dashed_line(img, pt1, pt2, color, thickness=1, dash_length=8):
    dist = calculate_distance(pt1, pt2)
    if dist <= 0: return
    for i in np.arange(0, dist, dash_length):
        r = i / dist
        p_start = (int(pt1[0]*(1-r) + pt2[0]*r), int(pt1[1]*(1-r) + pt2[1]*r))
        r2 = min(1.0, (i + dash_length/2) / dist)
        p_end = (int(pt1[0]*(1-r2) + pt2[0]*r2), int(pt1[1]*(1-r2) + pt2[1]*r2))
        cv2.line(img, p_start, p_end, color, thickness)

def analyze_snooker_image(image_path, corner_points, output_prefix="res"):
    img_orig = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img_orig is None: return {"status": "error", "message": "Decode failed"}

    # Compression for speed and bandwidth
    MAX_DIM = 800
    h, w = img_orig.shape[:2]
    if max(h, w) > MAX_DIM:
        s = MAX_DIM / max(h, w)
        img_orig = cv2.resize(img_orig, (int(w*s), int(h*s)))
        h, w = img_orig.shape[:2]

    pts1 = np.float32([[p[0]*w, p[1]*h] for p in corner_points])
    pts2 = np.float32([[0,0], [WIDTH,0], [0,HEIGHT], [WIDTH,HEIGHT]])
    M = cv2.getPerspectiveTransform(pts1, pts2)
    M_INV = np.linalg.inv(M)

    results = model.predict(source=img_orig, conf=0.3, verbose=False)[0]
    white_warp, white_orig = None, None
    best_white_conf = -1.0 # Track highest confidence white ball to avoid false detections from glare
    reds, colors = [], []
    all_balls_warp = []
    COLOR_BALL_CLASSES = ['yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball']

    for box in results.boxes:
        cx, cy = float(box.xywh[0][0]), float(box.xywh[0][1])
        cls = results.names[int(box.cls[0])]
        conf = float(box.conf[0]) # Confidence score for this detection
        
        pw = cv2.perspectiveTransform(np.array([[[cx, cy]]], dtype=np.float32), M)[0][0]
        wx, wy = float(pw[0]), float(pw[1])
        all_balls_warp.append((wx, wy))
        
        if cls == 'white-ball': 
            # Only update white ball position if this detection has higher confidence (prevents glare override)
            if conf > best_white_conf:
                white_orig, white_warp = (int(cx), int(cy)), (wx, wy)
                best_white_conf = conf
        elif cls == 'red-ball': 
            reds.append({'orig': (int(cx), int(cy)), 'warp': (wx, wy)})
        elif cls in COLOR_BALL_CLASSES: 
            colors.append({'orig': (int(cx), int(cy)), 'warp': (wx, wy)})

    del results
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # ERROR: No Cue Ball
    if not white_orig: return {"status": "error", "message": "Cue ball missing! System cannot find the white ball."}

    def process_group(targets, t_type):
        # ERROR: No target balls found on the table
        if not targets:
            return [] # Returning empty triggers the Flutter "No Balls Detected" UI

        valid = []
        for i, t in enumerate(targets):
            pkt, score = find_best_pocket_and_score(t['warp'], white_warp, all_balls_warp)
            if pkt:
                pct = min(100.0, (score / THEORETICAL_MAX_SCORE) * 100.0)
                valid.append({'score': score, 'pct': pct, 'target': t, 'pocket': pkt})
        
        # ====================================================================
        # SNOOKERED FALLBACK LOGIC (All attacking paths are blocked)
        # ====================================================================
        if not valid and len(targets) > 0:
            # 1. Find the closest ball of this type to the cue ball
            closest_target = None
            min_distance = float('inf')
            
            for t in targets:
                dist = calculate_distance(white_warp, t['warp'])
                if dist < min_distance:
                    min_distance = dist
                    closest_target = t
            
            # 2. Draw a safety line directly to this closest ball
            img_draw = img_orig.copy()
            # Draw an Orange dashed line indicating a defensive shot
            draw_dashed_line(img_draw, white_orig, closest_target['orig'], (0, 165, 255), 2)
            # Highlight the target ball
            cv2.circle(img_draw, closest_target['orig'], int(BALL_RADIUS_WARPED * 1.5), (0, 0, 255), 2)
            
            path = f"{output_prefix}_{t_type}_safety.jpg"
            cv2.imwrite(path, img_draw, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
            
            # 3. Return a special response with 'N/A' difficulty and a 'snookered' message
            # Flutter will see 'N/A' and trigger the Orange Dashboard!
            return [{'rank': 1, 'difficulty': 'N/A', 'message': 'all_paths_blocked', 'image_path': path}]

        # ====================================================================
        # NORMAL ATTACKING SHOT LOGIC
        # ====================================================================
        valid.sort(key=lambda x: x['score'])
        res_list = []
        for rank, shot in enumerate(valid[:3]):
            img_draw = img_orig.copy()
            
            v_p2r = np.array([shot['target']['warp'][0]-shot['pocket'][0], shot['target']['warp'][1]-shot['pocket'][1]])
            u_p2r = v_p2r / np.linalg.norm(v_p2r)
            gh_w = (shot['target']['warp'][0] + BALL_RADIUS_WARPED*2*u_p2r[0], shot['target']['warp'][1] + BALL_RADIUS_WARPED*2*u_p2r[1])
            
            gh_o = cv2.perspectiveTransform(np.array([[[gh_w[0], gh_w[1]]]], dtype=np.float32), M_INV)[0][0]
            pkt_o = cv2.perspectiveTransform(np.array([[[shot['pocket'][0], shot['pocket'][1]]]], dtype=np.float32), M_INV)[0][0]
            
            gh_orig_pt = (int(gh_o[0]), int(gh_o[1]))
            target_orig_pt = shot['target']['orig']
            pkt_orig_pt = (int(pkt_o[0]), int(pkt_o[1]))
            
            draw_dashed_line(img_draw, white_orig, gh_orig_pt, (255,255,255), 2)
            draw_dashed_line(img_draw, target_orig_pt, pkt_orig_pt, (0,255,255), 2)
            cv2.circle(img_draw, gh_orig_pt, int(BALL_RADIUS_WARPED * 1.5), (0, 0, 255), 2)
            
            path = f"{output_prefix}_{t_type}_{rank+1}.jpg"
            cv2.imwrite(path, img_draw, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
            res_list.append({'rank': rank+1, 'difficulty': f"{shot['pct']:.1f}%", 'message': 'clear_path', 'image_path': path})
        return res_list

    return {
        "status": "success",
        "red_shots": process_group(reds, "red"),
        "color_shots": process_group(colors, "color")
    }