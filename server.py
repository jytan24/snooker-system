import importlib.metadata

try:
    import ultralytics.utils.checks

    def patched_check_requirements(requirements=(), exclude=(), install=True, cmds=""):
        """Checks requirements, prioritizing onnxruntime-directml if present."""
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
        pass

    if not hasattr(ultralytics.utils.checks, '_original_check_requirements'):
        ultralytics.utils.checks._original_check_requirements = ultralytics.utils.checks.check_requirements

    def final_patched_check(requirements=(), exclude=(), install=True, cmds=""):
        """Calls patched_check_requirements, falls back to original if needed."""
        if patched_check_requirements(requirements, exclude, install, cmds) is True:
            return True
        return ultralytics.utils.checks._original_check_requirements(requirements, exclude, install, cmds)

    ultralytics.utils.checks.check_requirements = final_patched_check
    print("Patched Ultralytics requirement checks.")
except Exception as e:
    print(f"Error patching checks: {e}")

from flask import Flask, request, jsonify, send_file, after_this_request, make_response
from ultralytics import YOLO
import cv2
import numpy as np
import base64
import os
import uuid
import time
import json
import threading
import subprocess
import shutil
import re
import onnxruntime as ort
import logging

try:
    _original_inference_session = ort.InferenceSession

    class PatchedInferenceSession(_original_inference_session):
        """Forces DirectML provider for ONNX inference if available."""
        def __init__(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kwargs):
            available = ort.get_available_providers()
            if 'DmlExecutionProvider' in available:
                if providers is None:
                    providers = ['DmlExecutionProvider']
                elif isinstance(providers, list) and 'DmlExecutionProvider' not in providers:
                    providers.insert(0, 'DmlExecutionProvider')
                print(f"Force-Enabled DirectML (AMD GPU) for ONNX Session. Providers: {providers}")
            super().__init__(path_or_bytes, sess_options=sess_options, providers=providers, provider_options=provider_options, **kwargs)

    ort.InferenceSession = PatchedInferenceSession
    print("Applied DirectML Monkey Patch for AMD GPU Support.")
except Exception as e:
    print(f"Could not apply DirectML patch: {e}")

app = Flask(__name__)

logging.getLogger('werkzeug').setLevel(logging.WARNING)  # Suppress repetitive /job_status poll logs

jobs = {}
_jobs_lock = threading.Lock()       # Protects concurrent read/write access to the jobs dict across worker threads.
_inference_lock = threading.Lock()  # Serialises model.track() calls to prevent ByteTrack internal state corruption.
_cached_ffmpeg_bin = None           # Cached ffmpeg executable path, resolved once at first use.

# When True, processed job returns the original uploaded video (no annotation overlays).
PRESERVE_OUTPUT_VIDEO_QUALITY = False

TEMP_VIDEO_DIR = os.path.abspath('temp_videos')
PROCESSED_VIDEO_DIR = os.path.abspath('processed_videos')
_processed_name_lock = threading.Lock()

def _get_ffmpeg_bin():
    """Return the ffmpeg executable path, resolving it once and caching the result."""
    global _cached_ffmpeg_bin
    if _cached_ffmpeg_bin is not None:
        return _cached_ffmpeg_bin
    ffmpeg = shutil.which('ffmpeg')
    try:
        if not ffmpeg:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    _cached_ffmpeg_bin = ffmpeg
    return _cached_ffmpeg_bin

def _cleanup_old_jobs(max_age_seconds=3600):
    """Remove completed/error/cancelled jobs older than max_age_seconds."""
    now = time.time()
    with _jobs_lock:
        expired = [jid for jid, j in jobs.items()
                   if now - j.get('created_at', now) > max_age_seconds
                   and j.get('status') in ('completed', 'error', 'cancelled')]
        for jid in expired:
            job = jobs[jid]
            # Clean up files
            for path_key in ['playback_path', 'output_path', 'input_path']:
                fpath = job.get(path_key)
                if fpath and os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception as e:
                        print(f"Warning: could not delete {fpath}: {e}")
            del jobs[jid]
            print(f"Cleaned up expired job {jid}")


def _allocate_processed_output_path():
    """Return the next output path: processed_videos/processed_vidN.mp4."""
    os.makedirs(PROCESSED_VIDEO_DIR, exist_ok=True)
    max_idx = 0
    for name in os.listdir(PROCESSED_VIDEO_DIR):
        m = re.match(r'^processed_vid(\d+)\.mp4$', name)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    next_idx = max_idx + 1
    return os.path.join(PROCESSED_VIDEO_DIR, f'processed_vid{next_idx}.mp4')


def _allocate_temp_input_path(suffix):
    """Return a temp input path inside temp_videos/ with the provided suffix."""
    os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)
    return os.path.join(TEMP_VIDEO_DIR, f"{uuid.uuid4()}_{suffix}")


def _make_playback_compatible_video(source_path, dest_path):
    """Transcode to a seek-friendly H.264 MP4 for Android/Flutter playback.

    Returns True on success, False otherwise.
    """
    try:
        if not os.path.exists(source_path) or os.path.getsize(source_path) <= 0:
            return False

        ffmpeg_bin = _get_ffmpeg_bin()

        if not ffmpeg_bin:
            print('[WARN] ffmpeg not found in PATH and bundled ffmpeg unavailable; skip playback transcode.')
            return False

        cmd = [
            ffmpeg_bin,
            '-y',
            '-i', source_path,
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '22',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-an',
            dest_path,
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )

        if proc.returncode != 0:
            print(f"[WARN] ffmpeg transcode failed ({proc.returncode}): {proc.stderr[-500:]}")
            return False

        if not os.path.exists(dest_path) or os.path.getsize(dest_path) <= 0:
            return False

        return True
    except Exception as e:
        print(f"[WARN] playback transcode error: {e}")
        return False

class SnookerGameTracker:
    """Finite-state machine that tracks scores, turns, fouls, and game phase for a snooker match."""

    def __init__(self, fps=30):
        self.fps = fps
        self.player_scores = {1: 0, 2: 0}
        self.current_player = 1
        self.current_break = 0

        # FSM states:
        # AwaitingBreak    - start / after foul / after turn switch
        # RedBallActive    - reds on table, player must pot a red next
        # ColourNomination - player just potted red, must now pot any colour
        # ClearancePhase   - no reds left; colours must go in strict order
        self.state = 'AwaitingBreak'

        self.points_map = {
            'red-ball': 1, 'yellow-ball': 2, 'green-ball': 3,
            'brown-ball': 4, 'blue-ball': 5, 'pink-ball': 6,
            'black-ball': 7
        }
        self.color_order = ['yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball']
        self.next_clearance_target = 0

        self.potted_counts = {
            1: {k: 0 for k in self.points_map.keys()},
            2: {k: 0 for k in self.points_map.keys()}
        }

        self.last_stable_counts = {}
        self.is_calibrated = True
        self.max_points_remaining = 0
        self.action_log = []
        
        self.shot_potted_buffer = []

        self.shot_id = 0
        self.last_red_shot_id = -1
        self.frames_since_motion = 0
        self.shot_active_debounced = False

        # Auto turn-switch tracking.
        # prev_shot_active_debounced lets us detect the True→False transition
        # (shot fully settled) without requiring the caller to track it.
        # pot_happened_this_shot is set True as soon as any ball is potted or a
        # foul fires, preventing the auto-switch from double-switching the turn.
        self.prev_shot_active_debounced = False
        self.pot_happened_this_shot = False

    def get_stable_counts(self):
        """Return a snapshot of the most recent ball counts used for reporting."""
        return dict(self.last_stable_counts)

    def switch_turn(self):
        """Swap the active player and reset the current break to zero."""
        self.current_player = 2 if self.current_player == 1 else 1
        self.current_break = 0
        msg = f"[INFO] Turn Switched: Now Player {self.current_player}"
        self.action_log.append({"type": "info", "msg": msg})
        print(msg)

    def manual_miss(self):
        """Handle a manually signalled miss by switching turn.

        If no reds remain (clearance phase), the opponent continues from the
        same next_clearance_target position rather than reverting to red-phase logic.
        """
        self.switch_turn()
        reds_remaining = self.last_stable_counts.get('red-ball', 0)
        if self.state == 'ClearancePhase' or reds_remaining == 0:
            self.state = 'ClearancePhase'
            print(f"Manual miss: turn switched, staying in ClearancePhase (next: {self.color_order[self.next_clearance_target] if self.next_clearance_target < len(self.color_order) else 'done'})")
        else:
            self.state = 'AwaitingBreak'
            print("Manual miss: turn switched, reset to AwaitingBreak.")

    def _set_clearance_target_from_counts(self, counts):
        """Reset the clearance target to Yellow (index 0) regardless of current YOLO counts.

        The clearance phase always starts at Yellow in professional snooker.
        We cannot trust YOLO counts here because the referee may still be
        retrieving the final potted colour from the pocket, making it
        temporarily invisible and skewing the count.
        """
        self.next_clearance_target = 0

    def _handle_foul(self, foul_ball_key_or_value, counts, reason=""):
        """Award foul points (minimum 4) to the opponent, switch turn, and set next state.

        After a foul the next state depends on whether reds are still on the table:
          - Reds remain   → AwaitingBreak (opponent must pot a red next)
          - No reds left  → ClearancePhase (opponent continues the colour sequence
                            from the current next_clearance_target position)
        This prevents an infinite-foul loop when a foul occurs during ColourNomination
        on the last red, or mid-way through ClearancePhase.
        """
        opponent = 2 if self.current_player == 1 else 1
        if isinstance(foul_ball_key_or_value, (int, float)):
            foul_value = max(4, int(foul_ball_key_or_value))
            foul_desc = f"value={foul_value}"
        else:
            foul_value = max(4, self.points_map.get(foul_ball_key_or_value, 4))
            foul_desc = str(foul_ball_key_or_value).replace('-ball', '').capitalize()
            if not reason:
                if foul_ball_key_or_value == 'white-ball':
                    reason = "potted White ball"
                else:
                    reason = f"illegal event involving {foul_desc}"

        self.player_scores[opponent] += foul_value
        msg = f"[FOUL] P{self.current_player} {reason} -> +{foul_value} to P{opponent}"
        self.action_log.append({"type": "foul", "msg": msg})
        print(msg)

        self.switch_turn()

        # Determine the correct post-foul state based on whether reds remain.
        reds_remaining = (counts or self.last_stable_counts).get('red-ball', 0)
        if self.state == 'ClearancePhase' or reds_remaining == 0:
            self.state = 'ClearancePhase'
            print(f"[FSM] Post-foul: no reds on table, staying in ClearancePhase (next target: {self.color_order[self.next_clearance_target] if self.next_clearance_target < len(self.color_order) else 'done'})")
        else:
            self.state = 'AwaitingBreak'

    def get_max_points_remaining(self):
        """Calculate the theoretical maximum points still available on the table."""
        reds = self.last_stable_counts.get('red-ball', 0)
        if reds > 0:
            return (reds * 8) + 27
        score = 0
        for color in self.color_order:
            if self.last_stable_counts.get(color, 0) > 0:
                score += self.points_map[color]
        return score

    def _result(self):
        """Build and return the current game-state snapshot dict."""
        return {
            'player1_score': self.player_scores[1],
            'player2_score': self.player_scores[2],
            'current_player': self.current_player,
            'break': self.current_break,
            'phase': self.state,
            'potted_1': self.potted_counts[1],
            'potted_2': self.potted_counts[2],
            'points_remaining': self.get_max_points_remaining(),
            'history': self.action_log[-50:]  # Send last 50 events
        }

    def get_banned_colors(self):
        """
        Return a list of colors that should be strictly ignored by the tracking
        system. In ClearancePhase, any color before the current target has 
        already been legally potted and must not physically exist on the table.
        """
        if self.state != 'ClearancePhase':
            return []
        if self.next_clearance_target >= len(self.color_order):
            return self.color_order[:]
        return self.color_order[:self.next_clearance_target]

    def update(self, current_frame_counts, potted_balls=None, is_shot_active=False, any_moving=False):
        """Advance the game FSM given current ball counts, potted events, and motion state."""
        counts = (current_frame_counts or {}).copy()
        self.last_stable_counts = counts
        self.max_points_remaining = self.get_max_points_remaining()

        # 2-second debounce: ignore tracking flicker between physical cue strokes.
        if any_moving:
            self.frames_since_motion = 0
            if not self.shot_active_debounced:
                self.shot_id += 1
                self.shot_active_debounced = True
                print(f"[SHOT] New physical stroke detected! Shot ID: {self.shot_id}")
        else:
            self.frames_since_motion += 1
            if self.frames_since_motion > int(self.fps * 2.0):
                self.shot_active_debounced = False

        # Detect shot lifecycle transitions for auto turn-switch logic.
        # shot_just_started: first frame of a new stroke → reset the pot flag.
        # shot_just_settled: debounce expired after last motion → evaluate outcome.
        shot_just_started  = not self.prev_shot_active_debounced and self.shot_active_debounced
        shot_just_settled  = self.prev_shot_active_debounced and not self.shot_active_debounced
        self.prev_shot_active_debounced = self.shot_active_debounced

        if shot_just_started:
            self.pot_happened_this_shot = False

        if potted_balls is None:
            potted_balls = []

        if is_shot_active:
            if potted_balls:
                self.shot_potted_buffer.extend(potted_balls)
            return self._result()

        potted_balls = self.shot_potted_buffer + potted_balls
        self.shot_potted_buffer = []

        # Mark that something was potted this shot before any early returns,
        # so the auto turn-switch at the end of this method does not fire.
        if potted_balls:
            self.pot_happened_this_shot = True

        if any(ball == 'white-ball' for ball in potted_balls):
            self._handle_foul('white-ball', counts, reason="potted White ball")
            return self._result()

        if potted_balls:
            if self.state in ['AwaitingBreak', 'RedBallActive']:
                valid = [b for b in potted_balls if b in self.points_map]
                reds_potted = valid.count('red-ball')
                colors_in_events = [b for b in valid if b != 'red-ball']

                if reds_potted > 0:
                    self.last_red_shot_id = self.shot_id
                    self.player_scores[self.current_player] += reds_potted
                    self.current_break += reds_potted
                    self.potted_counts[self.current_player]['red-ball'] += reds_potted
                    self.action_log.append({"type": "pot", "msg": f"[P{self.current_player}] Potted Red (+{reds_potted})"})
                    if colors_in_events:
                        # Red and colour in the same shot = foul.
                        self._handle_foul(colors_in_events[0], counts, reason="potted Red and Colour in same shot")
                    else:
                        self.state = 'ColourNomination'
                elif colors_in_events:
                    # Colour potted before a red = foul.
                    self._handle_foul(colors_in_events[0], counts, reason="potted Colour before Red")

            elif self.state == 'ColourNomination':
                potted_colors = [b for b in potted_balls
                                 if b not in ('red-ball', 'white-ball') and b in self.points_map]
                reds_potted = [b for b in potted_balls if b == 'red-ball']
                
                # Check for multi-reds from the same physical shot arriving late.
                if reds_potted and self.shot_id == self.last_red_shot_id:
                    count = len(reds_potted)
                    self.player_scores[self.current_player] += count
                    self.current_break += count
                    self.potted_counts[self.current_player]['red-ball'] += count
                    self.action_log.append({"type": "pot", "msg": f"[P{self.current_player}] Potted Red (+{count}) (Multi-Red)"})
                    reds_potted = [] # Safely credited, remove from foul logic below
                
                if reds_potted or len(potted_colors) > 1:
                    # Red in colour phase, or multiple colours at once = foul.
                    foul_val = max((self.points_map.get(b, 4) for b in potted_balls
                                    if b in self.points_map), default=4)
                    reason = "potted Red during Colour phase" if reds_potted else "potted multiple colours at once"
                    self._handle_foul(foul_val, counts, reason=reason)
                elif potted_colors:
                    ball = potted_colors[0]
                    p = self.points_map[ball]
                    self.player_scores[self.current_player] += p
                    self.current_break += p
                    self.potted_counts[self.current_player][ball] += 1
                    ball_name = ball.replace('-ball', '').capitalize()
                    self.action_log.append({"type": "pot", "msg": f"[P{self.current_player}] Potted {ball_name} (+{p})"})
                    # Auto-assume referee respotted the colour.
                    # If reds remain → back to RedBallActive; else → ClearancePhase.
                    if counts.get('red-ball', 0) > 0:
                        self.state = 'RedBallActive'
                    else:
                        self._set_clearance_target_from_counts(counts)
                        self.state = 'ClearancePhase'
                    print(f"Colour potted: {ball_name} +{p}. Next state: {self.state}")

            elif self.state == 'ClearancePhase':
                for ball in potted_balls:
                    if ball not in self.points_map:
                        continue
                    if self.next_clearance_target < len(self.color_order):
                        expected = self.color_order[self.next_clearance_target]
                        if ball == expected:
                            p = self.points_map[ball]
                            self.player_scores[self.current_player] += p
                            self.current_break += p
                            self.potted_counts[self.current_player][ball] += 1
                            self.next_clearance_target += 1
                            ball_name = ball.replace('-ball', '').capitalize()
                            self.action_log.append({"type": "pot", "msg": f"[P{self.current_player}] Potted {ball_name} (+{p})"})
                            print(f"Clearance: {ball_name} +{p}. Next: {self.color_order[self.next_clearance_target] if self.next_clearance_target < len(self.color_order) else 'done'}")
                        else:
                            self._handle_foul(ball, counts, reason="potted wrong clearance colour")
                            break

        # Auto turn-switch: shot fully settled (2-s debounce expired) with no pot and no foul.
        # Covers both missed shots and deliberate safety shots — both end the turn in snooker.
        if shot_just_settled and not self.pot_happened_this_shot and self.shot_id > 0:
            self.switch_turn()
            msg = f"[AUTO] No pot detected — turn passed to P{self.current_player}"
            self.action_log.append({"type": "info", "msg": msg})
            print(msg)

        return self._result()

class BallCoordinateTracker:
    """Track snooker balls across frames with unique IDs and motion state.

    Pot rule:
    - A ball is considered POTTED when:
        1. Its last detected position was inside a 70 px pocket zone, AND
        2. It has been missing (not detected) for >= pot_frames (1.5 s).
    - Balls disappearing outside every pocket zone are silently deleted.
    - Once potted, a track is never re-matched to prevent double-pot events.
    """

    POCKET_ZONE_RADIUS = 100.0   # px in video pixel space (matches calibration UI)

    def __init__(
        self,
        motion_threshold_px=5.0,
        max_match_distance_px=60.0,
        fps=30.0,
        patience_frames=5,
        max_missed_frames=None,
        default_radius_px=10.0,
    ):
        self.motion_threshold_px = float(motion_threshold_px)
        self.max_match_distance_px = float(max_match_distance_px)
        self.fps = float(fps)
        self.patience_frames = int(patience_frames)
        # A ball must be missing for 0.8 s while its last position was in a pocket zone.
        self.pot_frames = max(2, int(round(fps * 0.8)))  # ~0.8 s
        self.max_missed_frames = int(max_missed_frames) if max_missed_frames is not None else self.pot_frames + max(5, int(round(fps * 0.5)))
        self.default_radius_px = float(default_radius_px)

        self.pocket_points_2d = []  # list of 6 (x,y) tuples; empty = uncalibrated

        self.next_id = 1
        # id -> track
        # track = {
        #   'id': int, 'label': str|None, 'x': float, 'y': float,
        #   'radius': float, 'status': 'Moving'|'Stationary',
        #   'missed': int, 'potted': bool
        # }
        self.tracks = {}
        self.last_collisions = []
        self.last_potted_ids = []
        self.last_potted_balls = []

    def _distance(self, x1, y1, x2, y2):
        """Return the Euclidean distance between two 2-D points."""
        return float(np.hypot(x2 - x1, y2 - y1))

    def _create_track(self, det):
        """Initialise a new ball track from a raw detection dict."""
        tid = self.next_id
        self.next_id += 1
        x = float(det['x'])
        y = float(det['y'])
        self.tracks[tid] = {
            'id': tid,
            'label': det.get('label'),
            'x': x,
            'y': y,
            'radius': float(det.get('radius', self.default_radius_px)),
            'status': 'Stationary',
            'missed': 0,
            'potted': False,
            'recent_points': [(x, y)],
            'label_history': [det.get('label')] if det.get('label') else [],
            # Initialise in_zone correctly so a ball first detected inside
            # the pocket zone is already flagged from frame 1.
            'in_zone': self._in_pocket_zone(x, y),
        }

    def _extract_radius(self, det):
        """Derive a ball radius in pixels from a detection's bounding-box fields."""
        if 'radius' in det and det['radius'] is not None:
            return float(det['radius'])

        if 'w' in det and 'h' in det:
            w = max(1.0, float(det['w']))
            h = max(1.0, float(det['h']))
            return min(w, h) / 2.0

        if 'x1' in det and 'y1' in det and 'x2' in det and 'y2' in det:
            w = max(1.0, float(det['x2']) - float(det['x1']))
            h = max(1.0, float(det['y2']) - float(det['y1']))
            return min(w, h) / 2.0

        return self.default_radius_px

    def _compute_collisions(self):
        """Return a list of pairwise ball-overlap events among currently visible tracks."""
        active = [tr for tr in self.tracks.values() if tr.get('missed', 0) == 0]
        collisions = []
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                a = active[i]
                b = active[j]
                dist = self._distance(a['x'], a['y'], b['x'], b['y'])
                threshold = 2.5 * ((float(a.get('radius', self.default_radius_px)) + float(b.get('radius', self.default_radius_px))) / 2.0)
                if dist < threshold:
                    collisions.append({
                        'id1': a['id'],
                        'id2': b['id'],
                        'distance': dist,
                        'threshold': threshold
                    })
        return collisions

    def _in_pocket_zone(self, raw_x, raw_y):
        """Return True if (raw_x, raw_y) is within POCKET_ZONE_RADIUS px of any calibrated pocket.
        If no pocket calibration has been set, returns True for all positions
        (any disappearance counts as a pot)."""
        if not self.pocket_points_2d:
            return True  # No calibration — accept all disappearances.
        for px, py in self.pocket_points_2d:
            if self._distance(raw_x, raw_y, px, py) <= self.POCKET_ZONE_RADIUS:
                return True
        return False

    def get_collisions(self):
        """Return the collision list from the most recent update."""
        return list(self.last_collisions)

    def get_potted_ids(self):
        """Return track IDs of balls potted in the most recent update."""
        return list(self.last_potted_ids)

    def get_potted_balls(self):
        """Return label strings of balls potted in the most recent update."""
        return list(self.last_potted_balls)

    def update(self, detections):
        """Update tracker using current-frame detections.

        Args:
            detections: list of dicts. Required keys: 'x', 'y'. Optional: 'label'.

        Returns:
            list of active tracks with fields: id, label, x, y, status.
        """
        if detections is None:
            detections = []

        dets = []
        for d in detections:
            if 'x' in d and 'y' in d:
                dets.append({
                    'x': float(d['x']),
                    'y': float(d['y']),
                    'label': d.get('label'),
                    'radius': self._extract_radius(d)
                })

        unmatched_track_ids = set(self.tracks.keys())
        matched_det_idx = set()

        # Greedy nearest matching (same label preferred when available).
        # IMPORTANT: already-potted tracks are excluded from matching so that a
        # brief re-appearance at the pocket lip cannot reset the potted flag and
        # cause a spurious second pot event (double-pot bug fix).
        for di, det in enumerate(dets):
            best_tid = None
            best_dist = float('inf')

            # Pass 1: same-label matching when both labels exist
            if det.get('label') is not None:
                for tid in list(unmatched_track_ids):
                    tr = self.tracks[tid]
                    if tr.get('potted', False):
                        continue  # never re-match a potted track
                    if tr.get('label') != det.get('label'):
                        continue
                    
                    # Relax matching distance linearly if the ball is disappearing into a pocket!
                    allowed_dist = self.max_match_distance_px
                    if tr.get('in_zone', False) and tr.get('missed', 0) > 0:
                        allowed_dist = 150.0  # Big enough to absorb shadow flickers anywhere in the hole
                        
                    dist = self._distance(tr['x'], tr['y'], det['x'], det['y'])
                    if dist < best_dist and dist <= allowed_dist:
                        best_dist = dist
                        best_tid = tid

            # Pass 2: fallback to any-label matching
            if best_tid is None:
                for tid in list(unmatched_track_ids):
                    tr = self.tracks[tid]
                    if tr.get('potted', False):
                        continue  # never re-match a potted track
                    
                    # Relax matching distance linearly if the ball is disappearing into a pocket!
                    allowed_dist = self.max_match_distance_px
                    if tr.get('in_zone', False) and tr.get('missed', 0) > 0:
                        allowed_dist = 150.0
                        
                    dist = self._distance(tr['x'], tr['y'], det['x'], det['y'])
                    if dist < best_dist and dist <= allowed_dist:
                        best_dist = dist
                        best_tid = tid

            if best_tid is not None:
                tr = self.tracks[best_tid]
                displacement = best_dist
                is_moving = displacement > self.motion_threshold_px
                tr['status'] = 'Moving' if is_moving else 'Stationary'
                tr['x'] = det['x']
                tr['y'] = det['y']
                
                # Keep up to 5 recent points to calculate velocity for high-speed tracking drops
                tr.setdefault('recent_points', []).append((det['x'], det['y']))
                tr['recent_points'] = tr['recent_points'][-5:]
                
                tr['radius'] = float(det.get('radius', tr.get('radius', self.default_radius_px)))
                
                # Check pocket zone *first* so we know whether to color-lock
                now_in_zone = self._in_pocket_zone(tr['x'], tr['y'])
                
                if det.get('label') is not None:
                    # Pocket-zone Color Locking feature
                    # Ignore YOLO label flicker when ball is sinking into darkness
                    if not now_in_zone:
                        # Motion Blur Color Locking & Temporal Smoothing feature
                        if not is_moving:
                            # Only accept new YOLO colors when Stationary to avoid motion blur smearing
                            tr.setdefault('label_history', []).append(det.get('label'))
                            # Keep last 15 stationary frames (~0.5 seconds at 30fps)
                            tr['label_history'] = tr['label_history'][-15:]
                            
                            # Majority Vote evaluation
                            valid_labels = [l for l in tr['label_history'] if l is not None]
                            if valid_labels:
                                counts = {}
                                for l in valid_labels:
                                    counts[l] = counts.get(l, 0) + 1
                                majority_label = max(counts, key=counts.get)
                                tr['label'] = majority_label
                        
                tr['missed'] = 0
                # Update in_zone flag live so we always know the last position status.
                tr['in_zone'] = now_in_zone
                # NOTE: do NOT reset tr['potted'] here — the double-pot fix above
                # means potted tracks can never reach this code path.

                unmatched_track_ids.remove(best_tid)
                matched_det_idx.add(di)

        for di, det in enumerate(dets):
            if di not in matched_det_idx:
                self._create_track(det)

        self.last_potted_ids = []
        self.last_potted_balls = []
        remove_ids = []
        for tid in unmatched_track_ids:
            tr = self.tracks[tid]
            
            # Physics Extrapolation: If a ball disappears completely outside the pocket 
            # while moving at high speed, mathematically project its trajectory forward!
            if tr['missed'] == 0 and not tr.get('in_zone', False):
                pts = tr.get('recent_points', [])
                if len(pts) >= 3:
                    dx = pts[-1][0] - pts[0][0]
                    dy = pts[-1][1] - pts[0][1]
                    frames_diff = len(pts) - 1
                    avg_dx = dx / frames_diff
                    avg_dy = dy / frames_diff
                    
                    speed = (avg_dx**2 + avg_dy**2)**0.5
                    # If moving fast (e.g., > 10 pixels per frame blur)
                    if speed > 10.0:
                        # Project forward in time by 4 frames
                        proj_x = tr['x'] + (avg_dx * 4.0)
                        proj_y = tr['y'] + (avg_dy * 4.0)
                        
                        # See if the projected ball would have hit the pocket
                        if self._in_pocket_zone(proj_x, proj_y):
                            tr['in_zone'] = True
                            print(f"[PHYSICS] Fast ball {tid} vanished outside pocket. Extrapolating {speed:.1f}px/f -> In Zone: True")
            
            # Now increment missed counter
            tr['missed'] += 1

            # Single pot rule:
            #   1. Ball was last detected inside a 70px pocket zone (in_zone == True)
            #   2. Ball has been missing for >= 1.5 s (pot_frames)
            if (not tr.get('potted', False)
                    and tr.get('in_zone', False)
                    and tr['missed'] >= self.pot_frames):
                tr['potted'] = True
                self.last_potted_ids.append(tid)
                potted_label = tr.get('label')
                if potted_label:
                    self.last_potted_balls.append(str(potted_label))
                    print(f"[POTTED] track {tid} label={potted_label} "
                          f"pos=({tr['x']:.0f},{tr['y']:.0f}) missed={tr['missed']}")

            if tr['missed'] > self.max_missed_frames:
                remove_ids.append(tid)

        for tid in remove_ids:
            del self.tracks[tid]

        self.last_collisions = self._compute_collisions()

        out = []
        for tr in self.tracks.values():
            if tr.get('missed', 0) > self.patience_frames:
                continue
            out.append({
                'id': tr['id'],
                'label': tr.get('label'),
                'x': tr['x'],
                'y': tr['y'],
                'radius': tr.get('radius', self.default_radius_px),
                'status': tr['status']
            })
        return out

print("Loading model...")
model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.onnx'
if not os.path.exists(model_path):
    print(f"ONNX model not found at {model_path}, trying PT...")
    model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.pt'

try:
    model = YOLO(model_path, task='detect')
    print(f"Successfully loaded model from: {model_path}")
    print(f"Model classes: {model.names}")


except Exception as e:
    print(f"Error loading model: {e}")
    exit(1)

BALL_DRAW_COLORS = {
    'red-ball': (0, 0, 255),
    'yellow-ball': (0, 255, 255),
    'green-ball': (0, 180, 0),
    'brown-ball': (42, 42, 165),
    'blue-ball': (255, 0, 0),
    'pink-ball': (203, 192, 255),
    'black-ball': (30, 30, 30),
    'white-ball': (230, 230, 230)
}

SINGLE_BALL_IOU_THRESH = 0.45


def _iou_xyxy(a, b):
    """Compute Intersection-over-Union for two axis-aligned bounding boxes in (x1,y1,x2,y2) format."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    iw = max(0.0, inter_x2 - inter_x1)
    ih = max(0.0, inter_y2 - inter_y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


def suppress_multi_color_same_ball(detections, iou_thresh=SINGLE_BALL_IOU_THRESH):
    """Keep one label per physical ball by class-agnostic NMS (highest conf wins)."""
    items = sorted(detections, key=lambda d: float(d.get('conf', 0.0)), reverse=True)
    kept = []
    while items:
        best = items.pop(0)
        kept.append(best)
        remain = []
        for other in items:
            iou = _iou_xyxy(best.get('box_xyxy', [0, 0, 0, 0]), other.get('box_xyxy', [0, 0, 0, 0]))
            if iou < iou_thresh:
                remain.append(other)
        items = remain
    return kept

def get_box_center_hsv(frame_bgr, xyxy, predicted_label=None, hsv_frame=None):
    """Return median HSV from a class-conditioned, glare/background-masked patch."""
    try:
        x1, y1, x2, y2 = map(int, xyxy)
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            return None

        # Sample a tiny centered patch: 10% width x 10% height at (x=0.5, y=0.5).
        sample_w = max(1, int(round(bw * 0.10)))
        sample_h = max(1, int(round(bh * 0.10)))
        center_x = x1 + int(round(bw * 0.50))
        center_y = y1 + int(round(bh * 0.50))

        x_start = max(0, center_x - sample_w // 2)
        y_start = max(0, center_y - sample_h // 2)
        x_end = min(frame_bgr.shape[1], x_start + sample_w)
        y_end = min(frame_bgr.shape[0], y_start + sample_h)

        if x_end <= x_start or y_end <= y_start:
            return None

        # Reuse precomputed HSV frame when available to avoid repeated cvtColor.
        hsv = hsv_frame if hsv_frame is not None else cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        roi = hsv[y_start:y_end, x_start:x_end]
        if roi.size == 0:
            return None

        # 1) Glare filter: ignore very bright specular highlights.
        valid_mask = roi[:, :, 2] <= 225

        # 2) Class-conditioned background filter:
        # For non-green predictions, suppress green cloth hues (OpenCV Hue 40..85).
        # Green-ball is explicitly exempt so true green pixels are retained.
        if predicted_label != 'green-ball':
            hue = roi[:, :, 0]
            non_green_mask = (hue < 40) | (hue > 85)
            valid_mask = valid_mask & non_green_mask

        # 3) Failsafe: if all pixels were filtered out, use center-most single pixel.
        if not np.any(valid_mask):
            cpx = max(0, min(frame_bgr.shape[1] - 1, x1 + bw // 2))
            cpy = max(0, min(frame_bgr.shape[0] - 1, y1 + bh // 2))
            center_pixel = hsv[cpy, cpx]
            return (float(center_pixel[0]), float(center_pixel[1]), float(center_pixel[2]))

        valid_pixels = roi[valid_mask]  # shape: [N, 3]
        med_h = float(np.median(valid_pixels[:, 0]))
        med_s = float(np.median(valid_pixels[:, 1]))
        med_v = float(np.median(valid_pixels[:, 2]))
        return (med_h, med_s, med_v)
    except Exception:
        return None

def correct_label_by_hsv(pred_key, hsv_avg, pred_conf=None):
    """Color sanity check to avoid common red/blue swaps on motion blur."""
    if hsv_avg is None:
        return pred_key

    h, s, v = hsv_avg
    conf = float(pred_conf) if pred_conf is not None else 1.0

    # Gate all HSV overrides behind a confidence threshold so that strong
    # YOLO predictions are never flipped by ambiguous colour evidence.
    weak_prediction = conf < 0.70
    
    # Force Blue (only if model confidence is weak)
    if weak_prediction and 90 <= h <= 130 and s > 80 and v > 50:
        return 'blue-ball'
    # Force Green
    if weak_prediction and 40 <= h <= 85 and s > 60 and v > 50:
        return 'green-ball'
    # Force Brown
    if weak_prediction and 10 <= h <= 30 and s > 50 and 30 <= v <= 150:
        return 'brown-ball'

    # Very low saturation -> likely achromatic ball under glare/shadow.
    # Classify by value: brighter => white ball, darker => black ball.
    if s < 30:
        return 'white-ball' if v >= 90 else 'black-ball'

    # Ignore weak color evidence
    if s < 45 or v < 35:
        return pred_key

    # OpenCV hue range: [0, 179]
    is_red_hue = (h <= 12) or (h >= 165)
    is_blue_hue = 90 <= h <= 140

    # Be conservative: only override red/blue when model confidence is not strong.
    # This avoids flipping true reds to blue due to ROI drift over table cloth during motion.
    # Use the same 0.70 threshold defined above for consistency.
    if pred_key == 'red-ball':
        if weak_prediction and is_blue_hue and s >= 80:
            return 'blue-ball'
        return 'red-ball'

    if pred_key == 'blue-ball':
        if weak_prediction and is_red_hue and s >= 80:
            return 'red-ball'
        return 'blue-ball'

    return pred_key


@app.route('/frame_detections', methods=['POST'])
def get_frame_detections():
    """Accept a single video frame and return YOLO detections with a base64-encoded image."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'})

    file = request.files['file']
    temp_input = _allocate_temp_input_path('frame.mp4')
    
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
        
        yolo_frame = frame
        detections = []
        inference_warning = None

        try:
            results = model(yolo_frame, verbose=False)
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
        except Exception as infer_err:
            # Keep this endpoint resilient: pocket calibration can proceed
            # even if detector inference fails for frame 0.
            inference_warning = str(infer_err)
            print(f"[WARN] /frame_detections inference failed: {infer_err}")

        ok, buffer = cv2.imencode(
            '.jpg',
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 80]
        )
        if not ok:
            return jsonify({'error': 'Failed to encode frame image'}), 500
        img_str = base64.b64encode(buffer).decode('utf-8')

        payload = {
            'image_base64': img_str,
            'detections': detections,
            'width': width,
            'height': height
        }
        if inference_warning:
            payload['warning'] = 'Frame returned without detections'

        return jsonify(payload)

    except Exception as e:
        if os.path.exists(temp_input):
            try:
                os.remove(temp_input)
            except:
                pass
        return jsonify({'error': str(e)}), 500


def process_video_job(job_id, temp_input, temp_output, color_mapping, color_overrides, pocket_points=None):
    """Background worker: run YOLO detection, ball tracking, and game scoring on a full video."""
    try:
        print(f"Job {job_id}: starting processing worker...")
        jobs[job_id]['status'] = 'processing'
        jobs[job_id]['progress'] = 0
        jobs[job_id]['processed_frames'] = 0
        jobs[job_id]['total_frames'] = 0

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

        last_heartbeat_ts = time.time()

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        prototypes = []
        if color_overrides:
            ret_pre, frame_pre = cap.read()
            if ret_pre:
                print("Extracting color prototypes from frame 0...")
                target_w = 1280
                p_h, p_w = frame_pre.shape[:2]
                proc_w, proc_h = p_w, p_h
                
                if p_w > target_w:
                    aspect = p_h / p_w
                    new_w = target_w
                    new_h = int(target_w * aspect)
                    frame_pre = cv2.resize(frame_pre, (new_w, new_h))
                    proc_w, proc_h = new_w, new_h

                hsv_frame = cv2.cvtColor(frame_pre, cv2.COLOR_BGR2HSV)

                for override in color_overrides:
                    try:
                        if 'rect' not in override: continue
                        
                        x1, y1, x2, y2 = override['rect']
                        label = override.get('label')
                        
                        cx1, cy1 = max(0, int(x1)), max(0, int(y1))
                        cx2, cy2 = min(proc_w, int(x2)), min(proc_h, int(y2))
                        
                        cx1, cy1 = max(0, cx1), max(0, cy1)
                        cx2, cy2 = min(proc_w, cx2), min(proc_h, cy2)
                        
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

        new_width = width
        new_height = height

        if fps == 0 or np.isnan(fps): fps = 30.0

        print(
            f"Job {job_id}: video opened "
            f"({width}x{height}, fps={fps:.2f}, total_frames={total_frames if total_frames > 0 else 'unknown'})"
        )

        out = None
        if not PRESERVE_OUTPUT_VIDEO_QUALITY:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(temp_output, fourcc, fps, (new_width, new_height))
            if out is None or not out.isOpened():
                raise RuntimeError(
                    f"Failed to open VideoWriter for output: {temp_output} "
                    f"(fps={fps}, size={new_width}x{new_height})"
                )
        
        timeline_stats = []
        frame_count = 0
        last_stats = {}
        last_logged_action_idx = 0
        # Keep timeline dense enough for smooth UI updates, but cap points for long videos.
        timeline_sample_fps = min(10.0, fps) if fps > 0 else 10.0
        base_timeline_stride = max(1, int(round(fps / timeline_sample_fps))) if fps > 0 else 1
        max_timeline_points = 4000
        if total_frames > 0:
            capped_stride = max(1, int(np.ceil(total_frames / float(max_timeline_points))))
        else:
            capped_stride = 1
        timeline_stride = max(base_timeline_stride, capped_stride)
        
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
        
        tracker = SnookerGameTracker(fps=fps)
        coord_tracker = BallCoordinateTracker(
            motion_threshold_px=5.0,
            fps=fps,
            patience_frames=5,
        )
        # Apply pocket calibration if provided by the client.
        # The calibration image shown to the user is resized to max 1280px wide,
        # so tapped coordinates are in that 1280-wide space.
        # The video is processed at its original resolution, so we must scale
        # pocket points up to match the ball tracking coordinate space.
        if pocket_points and len(pocket_points) == 6:
            CALIB_WIDTH = 1280.0  # max width used by /frame_detections
            scale = width / CALIB_WIDTH if width > CALIB_WIDTH else 1.0
            coord_tracker.pocket_points_2d = [
                (float(p['x']) * scale, float(p['y']) * scale)
                for p in pocket_points
            ]
            print(f"Pocket calibration set (scale={scale:.3f}): {coord_tracker.pocket_points_2d}")

        hold_frames = max(2, int(round(fps * 0.35)))  # ~350ms detection hold for fast motion
        singleton_keys = ['yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball', 'white-ball']
        recent_singletons = {k: {'miss': hold_frames + 1, 'box_xyxy': None, 'conf': 0.0} for k in singleton_keys}
        recent_red_count = 0
        red_missing_frames = hold_frames + 1
        singleton_match_radius = max(18, int(min(new_width, new_height) * 0.06))
        singleton_lock_conf_max = 0.65
        last_singleton_positions = {k: None for k in singleton_keys}

        while cap.isOpened():
            if jobs[job_id].get('status') == 'cancelled':
                print(f"Job {job_id} cancelled by user.")
                break

            ret, frame = cap.read()
            if not ret:
                break

            try:
                key = cv2.waitKey(1) & 0xFF
                if key == ord('m'):
                    tracker.manual_miss()
            except Exception:
                pass
            
            yolo_frame = frame

            # Serialise inference to prevent concurrent threads corrupting the shared
            # ByteTrack internal state. For a single-user deployment this lock is
            # sufficient; multi-user deployments would need per-instance model objects.
            with _inference_lock:
                results = model.track(
                    yolo_frame,
                    tracker='bytetrack.yaml',
                    persist=(frame_count > 0),
                    conf=0.15,
                    verbose=False
                )
            
            annotated_frame = frame
            
            if results and len(results) > 0:
                result = results[0]
                names = result.names
                has_balls = False
                
                detections = []
                
                # Single HSV conversion per frame; reused by both prototype override and color sanity checks.
                hsv_current_frame = cv2.cvtColor(yolo_frame, cv2.COLOR_BGR2HSV)
                
                for box in result.boxes:
                    cls_id = int(box.cls[0])
                    conf = float(box.conf[0])
                    cls_name = names[cls_id]
                    predicted_label = cls_name.lower()
                    key = predicted_label
                    box_xyxy = box.xyxy[0]

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
                                    if dh > 90:
                                        dh = 180 - dh
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

                    hsv_avg = get_box_center_hsv(
                        yolo_frame,
                        box_xyxy,
                        predicted_label=predicted_label,
                        hsv_frame=hsv_current_frame
                    )
                    key = correct_label_by_hsv(key, hsv_avg, conf)
                    
                    bx1, by1, bx2, by2 = map(int, box.xyxy[0])
                    center = ((bx1 + bx2) / 2.0, (by1 + by2) / 2.0)
                    detections.append({
                        'key': key,
                        'conf': conf,
                        'box_xyxy': [bx1, by1, bx2, by2],
                        'center': center
                    })

                # Ensure one physical ball contributes only one color label.
                detections = suppress_multi_color_same_ball(detections)
                detections.sort(key=lambda x: x['conf'], reverse=True)

                # Singleton identity lock by position to reduce red/blue/other color flips during motion.
                frame_taken_singletons = set()
                for d in detections:
                    key = d['key']
                    if key not in singleton_keys:
                        continue

                    cx, cy = d['center']
                    best_label = None
                    best_dist = float('inf')

                    for label, pos in last_singleton_positions.items():
                        if pos is None or label in frame_taken_singletons:
                            continue
                        dx = cx - pos[0]
                        dy = cy - pos[1]
                        dist = (dx * dx + dy * dy) ** 0.5
                        if dist < best_dist and dist <= singleton_match_radius:
                            best_dist = dist
                            best_label = label

                    if best_label is not None and best_label != key and d['conf'] <= singleton_lock_conf_max:
                        d['key'] = best_label

                    if d['key'] in singleton_keys and d['key'] not in frame_taken_singletons:
                        frame_taken_singletons.add(d['key'])
                
                current_stats = {
                    'red-ball': 0, 'yellow-ball': 0, 'green-ball': 0,
                    'brown-ball': 0, 'blue-ball': 0, 'pink-ball': 0,
                    'black-ball': 0, 'white-ball': 0, 'colored-ball': 0,
                    'total_score': 0
                }

                for key in singleton_keys:
                    recent_singletons[key]['miss'] += 1
                red_missing_frames += 1
                
                accepted_detections = []
                banned_colors = tracker.get_banned_colors()
                
                for d in detections:
                    key = d['key']
                    
                    if key in banned_colors:
                        # Drop hallucinatory detections of already-potted colors.
                        continue

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
                        accepted_detections.append({
                            'key': key,
                            'conf': d['conf'],
                            'box_xyxy': d['box_xyxy'],
                            'persisted': False
                        })

                        if key in recent_singletons:
                            recent_singletons[key]['miss'] = 0
                            recent_singletons[key]['box_xyxy'] = d['box_xyxy']
                            recent_singletons[key]['conf'] = float(d['conf'])
                            bx1, by1, bx2, by2 = d['box_xyxy']
                            last_singleton_positions[key] = ((bx1 + bx2) / 2.0, (by1 + by2) / 2.0)

                # Hold singleton colors briefly when moving blur causes 1-2 missed frames.
                for key in singleton_keys:
                    if key in banned_colors:
                        continue  # Avoid respawning held ghosts of banned colors

                    if current_stats.get(key, 0) == 0:
                        rs = recent_singletons[key]
                        if rs['box_xyxy'] is not None and rs['miss'] <= hold_frames:
                            current_stats[key] = 1
                            has_balls = True
                            accepted_detections.append({
                                'key': key,
                                'conf': rs['conf'],
                                'box_xyxy': rs['box_xyxy'],
                                'persisted': True
                            })

                # Hold red count briefly only when it drops to zero for a moment.
                if current_stats.get('red-ball', 0) > 0:
                    recent_red_count = current_stats['red-ball']
                    red_missing_frames = 0
                elif recent_red_count > 0 and red_missing_frames <= hold_frames:
                    current_stats['red-ball'] = recent_red_count
                    has_balls = True
                
                if accepted_detections:
                    # Annotate on the original frame (not the inference copy) to keep output sharp.
                    annotated_frame = frame.copy()
                    for d in accepted_detections:
                        x1, y1, x2, y2 = map(int, d['box_xyxy'])
                        label = d['key']
                        conf_val = float(d['conf'])
                        color = BALL_DRAW_COLORS.get(label, (0, 255, 255))

                        cv2.rectangle(
                            annotated_frame,
                            (x1, y1),
                            (x2, y2),
                            color,
                            2,
                            cv2.LINE_AA
                        )

                        text = f"{label} {conf_val:.2f}"
                        text_y = y1 - 8 if y1 > 18 else y1 + 18
                        cv2.putText(
                            annotated_frame,
                            text,
                            (x1 + 1, text_y + 1),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            (0, 0, 0),
                            2,
                            cv2.LINE_AA
                        )
                        cv2.putText(
                            annotated_frame,
                            text,
                            (x1, text_y),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            color,
                            1,
                             cv2.LINE_AA
                         )
                else:
                     annotated_frame = frame

                # Feed coord_tracker only FRESH detections (not held/persisted).
                # Held detections keep resetting the missed counter,
                # which prevents the pot_frames_threshold from ever being reached.
                tracker_dets = []
                for d in accepted_detections:
                    if d.get('persisted', False):
                        continue  # skip held detections
                    x1, y1, x2, y2 = map(float, d['box_xyxy'])
                    cx = (x1 + x2) / 2.0
                    cy = (y1 + y2) / 2.0
                    radius = max(1.0, min(x2 - x1, y2 - y1) / 2.0)
                    tracker_dets.append({
                        'x': cx,
                        'y': cy,
                        'label': d.get('key'),
                        'radius': radius
                    })

                coordinate_mapping = coord_tracker.update(tracker_dets)
                collisions = coord_tracker.get_collisions()
                potted_ids = coord_tracker.get_potted_ids()
                potted_balls = coord_tracker.get_potted_balls()

                any_moving = any(t.get('status') == 'Moving' for t in coordinate_mapping)
                any_missing_in_zone = any(
                    tr.get('in_zone', False) and 0 < tr.get('missed', 0) < coord_tracker.pot_frames
                    for tr in coord_tracker.tracks.values()
                )
                is_shot_active = any_moving or any_missing_in_zone
                
                # Pass a snapshot of the detection counts to the game tracker so that
                # the tracker's read of input values and the subsequent accumulation
                # into current_stats do not interfere with each other.
                detection_counts = dict(current_stats)
                track_res = tracker.update(detection_counts, potted_balls, is_shot_active=is_shot_active, any_moving=any_moving)

                # Print every newly generated game action exactly once.
                if len(tracker.action_log) > last_logged_action_idx:
                    for evt in tracker.action_log[last_logged_action_idx:]:
                        if isinstance(evt, dict):
                            evt_type = evt.get('type', 'info')
                            evt_msg = evt.get('msg', '')
                            print(f"[JOB {job_id}][ACTION:{evt_type}] {evt_msg}")
                    last_logged_action_idx = len(tracker.action_log)

                timeline_appended_this_frame = False

                if has_balls or potted_balls or track_res.get('history'):
                    # Use smoothed counts for exported video stats (reduces motion-blur flicker)
                    stable_counts = tracker.get_stable_counts()
                    # Build output counts from current_stats (accumulation dict, not mutated input)
                    stats_counts = current_stats.copy()
                    for ball_key in points.keys():
                        stats_counts[ball_key] = stable_counts.get(ball_key, current_stats.get(ball_key, 0))

                    vis = 0
                    for key, count in stats_counts.items():
                        if key in points:
                            vis += count * points[key]
                    
                    pot = vis 
                    if stats_counts.get('red-ball', 0) > 0:
                        pot = (stats_counts['red-ball'] * 8) + 27

                    current_stats.update(stats_counts)
                    current_stats['player1_score'] = track_res['player1_score']
                    current_stats['player2_score'] = track_res['player2_score']
                    current_stats['current_player'] = track_res['current_player']
                    current_stats['current_break'] = track_res['break']
                    current_stats['game_phase'] = track_res['phase']
                    current_stats['potted_score'] = track_res['player1_score'] + track_res['player2_score']
                    
                    if 'potted_1' in track_res:
                         for k, v in track_res['potted_1'].items():
                             current_stats[f'p1_potted_{k}'] = v
                    if 'potted_2' in track_res:
                         for k, v in track_res['potted_2'].items():
                             current_stats[f'p2_potted_{k}'] = v
                    
                    current_stats['visible_score'] = vis
                    # Use accurate max potential score based on game state (not just currently visible balls)
                    current_stats['potential_score'] = track_res.get('points_remaining', pot)
                    current_stats['coordinate_mapping'] = [{
                        'id': int(t['id']),
                        'label': t.get('label'),
                        'x': round(float(t['x']), 2),
                        'y': round(float(t['y']), 2),
                        'status': t.get('status')
                    } for t in coordinate_mapping]
                    current_stats['collisions'] = [{
                        'id1': int(c['id1']),
                        'id2': int(c['id2']),
                        'distance': round(float(c['distance']), 3),
                        'threshold': round(float(c['threshold']), 3)
                    } for c in collisions]
                    current_stats['potted_ids'] = [int(v) for v in potted_ids]
                    current_stats['potted_balls'] = list(potted_balls)
                    current_stats['history'] = track_res.get('history', [])
                    current_stats['timestamp'] = frame_count / fps if fps > 0 else 0
                    last_stats = current_stats.copy()
                    
                    if frame_count % timeline_stride == 0:
                        # Keep timeline lightweight for client seek/rewind sync.
                        timeline_entry = {
                            'timestamp': current_stats.get('timestamp', 0),
                            'player1_score': current_stats.get('player1_score', 0),
                            'player2_score': current_stats.get('player2_score', 0),
                            'current_player': current_stats.get('current_player', 1),
                            'potential_score': current_stats.get('potential_score', 0),
                            'potted_balls': list(current_stats.get('potted_balls', [])),
                        }

                        for ball_key in points.keys():
                            timeline_entry[f'p1_potted_{ball_key}'] = current_stats.get(f'p1_potted_{ball_key}', 0)
                            timeline_entry[f'p2_potted_{ball_key}'] = current_stats.get(f'p2_potted_{ball_key}', 0)

                        timeline_stats.append(timeline_entry)
                        timeline_appended_this_frame = True

                if frame_count % timeline_stride == 0 and not timeline_appended_this_frame:
                    # Dense fallback sampling so rewind still updates even during temporary detection gaps.
                    ts = frame_count / fps if fps > 0 else 0
                    source = last_stats if isinstance(last_stats, dict) and len(last_stats) > 0 else {}

                    timeline_entry = {
                        'timestamp': ts,
                        'player1_score': int(source.get('player1_score', track_res.get('player1_score', tracker.player_scores.get(1, 0)))),
                        'player2_score': int(source.get('player2_score', track_res.get('player2_score', tracker.player_scores.get(2, 0)))),
                        'current_player': int(source.get('current_player', track_res.get('current_player', tracker.current_player))),
                        'potential_score': int(source.get('potential_score', track_res.get('points_remaining', tracker.get_max_points_remaining()))),
                        'potted_balls': list(source.get('potted_balls', [])),
                    }

                    for ball_key in points.keys():
                        p1_key = f'p1_potted_{ball_key}'
                        p2_key = f'p2_potted_{ball_key}'
                        timeline_entry[p1_key] = int(source.get(p1_key, track_res.get('potted_1', {}).get(ball_key, 0)))
                        timeline_entry[p2_key] = int(source.get(p2_key, track_res.get('potted_2', {}).get(ball_key, 0)))

                    timeline_stats.append(timeline_entry)

            if out is not None:
                out.write(annotated_frame)

            frame_count += 1
            with _jobs_lock:  # Thread-safe progress update visible to polling clients.
                jobs[job_id]['processed_frames'] = frame_count
                if total_frames > 0:
                    jobs[job_id]['progress'] = int((frame_count / total_frames) * 100)

            # Deterministic progress log cadence for long jobs.
            if frame_count % 500 == 0:
                if total_frames > 0:
                    print(
                        f"Job {job_id}: frame {frame_count}/{total_frames} "
                        f"({jobs[job_id]['progress']}%) [500-frame checkpoint]"
                    )
                else:
                    print(f"Job {job_id}: frame {frame_count} [500-frame checkpoint]")

            now_ts = time.time()
            if now_ts - last_heartbeat_ts >= 5.0:
                if total_frames > 0:
                    print(f"Job {job_id}: processing frame {frame_count}/{total_frames} ({jobs[job_id]['progress']}%)")
                else:
                    print(f"Job {job_id}: processing frame {frame_count} (total unknown)")
                last_heartbeat_ts = now_ts
                
            if tracker.is_calibrated and 'initial_points' not in jobs[job_id]:
                jobs[job_id]['initial_points'] = tracker.max_points_remaining

        cap.release()
        if out is not None:
            out.release()
        
        if jobs[job_id].get('status') == 'cancelled':
            playback_path = jobs[job_id].get('playback_path')
            if playback_path and os.path.exists(playback_path):
                try: os.remove(playback_path)
                except: pass
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
        
        # Keep timeline payload bounded for long videos so clients can fetch it
        # reliably and use it for seek/rewind synchronization.
        MAX_TIMELINE_POINTS = 4000
        if len(timeline_stats) > MAX_TIMELINE_POINTS:
            step = int(np.ceil(len(timeline_stats) / float(MAX_TIMELINE_POINTS)))
            timeline_compact = timeline_stats[::step]
            if timeline_compact and timeline_compact[-1] != timeline_stats[-1]:
                timeline_compact.append(timeline_stats[-1])
            print(
                f"Job {job_id}: compacted timeline "
                f"{len(timeline_stats)} -> {len(timeline_compact)} points (step={step})"
            )
            timeline_stats = timeline_compact

        response_data = {
            'summary': last_stats,
            'timeline': timeline_stats
        }

        playback_path = None
        if not PRESERVE_OUTPUT_VIDEO_QUALITY and os.path.exists(temp_output):
            base, ext = os.path.splitext(temp_output)
            candidate = f"{base}_playback{ext}"
            if _make_playback_compatible_video(temp_output, candidate):
                playback_path = candidate
                print(f"Job {job_id}: playback-compatible video ready -> {playback_path}")
            else:
                print(f"Job {job_id}: using raw annotated output (no playback transcode).")

        jobs[job_id]['stats'] = response_data
        jobs[job_id]['playback_path'] = playback_path
        jobs[job_id]['status'] = 'completed'
        print(f"Job {job_id} completed successfully.")

    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        playback_path = jobs[job_id].get('playback_path')
        if playback_path and os.path.exists(playback_path):
            try: os.remove(playback_path)
            except: pass
        if os.path.exists(temp_input):
            try: os.remove(temp_input)
            except: pass
        if os.path.exists(temp_output):
             try: os.remove(temp_output)
             except: pass

@app.route('/start_video_predict', methods=['POST'])
def start_video_predict():
    """Accept a video upload, create a background processing job, and return the job ID."""
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    temp_input = _allocate_temp_input_path('input.mp4')
    with _processed_name_lock:
        temp_output = _allocate_processed_output_path()
    
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

    pocket_points = []
    try:
        pocket_str = request.form.get('pocket_points')
        if pocket_str:
            pocket_points = json.loads(pocket_str)
            # Validate structure and numeric types before passing to the tracker.
            if isinstance(pocket_points, list) and len(pocket_points) == 6:
                # Validate each pocket point before passing to the tracker to
                # prevent cryptic errors deep inside the processing pipeline.
                for p in pocket_points:
                    if not isinstance(p, dict) or 'x' not in p or 'y' not in p:
                        raise ValueError(f"Invalid pocket point structure: {p}")
                    try:
                        float(p['x'])
                        float(p['y'])
                    except (TypeError, ValueError):
                        raise ValueError(f"Pocket coordinates must be numeric: {p}")
                print(f"Received {len(pocket_points)} pocket calibration points.")
            else:
                print(f"Warning: pocket_points has wrong length or type: {len(pocket_points)}")
                pocket_points = []
    except Exception as e:
        print(f"Warning: Failed to parse pocket_points: {e}")
        pocket_points = []

    job_id = str(uuid.uuid4())
    with _jobs_lock:  # Thread-safe job registration before the worker thread starts.
        jobs[job_id] = {
            'status': 'pending',
            'progress': 0,
            'input_path': temp_input,
            'output_path': temp_output,
            'playback_path': None,
            'created_at': time.time()
        }
    
    thread = threading.Thread(target=process_video_job, args=(job_id, temp_input, temp_output, color_mapping, color_overrides, pocket_points))
    thread.daemon = False
    thread.start()
    
    # Probabilistic cleanup: approximately 1 in 10 job submissions triggers a
    # background sweep to remove expired jobs and their associated files.
    import random
    if random.random() < 0.1:
        threading.Thread(target=_cleanup_old_jobs, daemon=True).start()
    
    return jsonify({'job_id': job_id, 'status': 'started'})

@app.route('/cancel_job/<job_id>', methods=['POST'])
def cancel_job(job_id):
    """Request cancellation of a running or pending job."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    current_status = jobs[job_id].get('status', '')
    if current_status in ['pending', 'processing']:
        jobs[job_id]['status'] = 'cancelled'
        print(f"Cancellation requested for Job {job_id}")
        return jsonify({'status': 'cancelled'})
    
    return jsonify({'error': f'Job cannot be cancelled (Status: {current_status})'}), 400

@app.route('/job_status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Return the current status and progress of a processing job."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    payload = {
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'processed_frames': job.get('processed_frames', 0),
        'total_frames': job.get('total_frames', 0),
        'initial_points': job.get('initial_points'),
        'error': job.get('error')
    }

    # Provide lightweight fallback stats once processing is complete.
    # IMPORTANT: for long videos, full timeline can be very large and may
    # cause polling connection drops on mobile clients.
    if job.get('status') == 'completed':
        stats = job.get('stats')
        if isinstance(stats, dict) and isinstance(stats.get('summary'), dict):
            payload['summary'] = stats.get('summary')
        include_timeline = request.args.get('include_timeline', '0').lower() in ('1', 'true', 'yes')
        if include_timeline and isinstance(stats, dict) and isinstance(stats.get('timeline'), list):
            # Optional explicit include for debugging/manual calls.
            payload['timeline'] = stats.get('timeline')

    return jsonify(payload)

@app.route('/job_result/<job_id>', methods=['GET'])
def get_job_result(job_id):
    """Serve the processed video file.

    Do not delete files immediately after first response because the Flutter
    video player performs additional range/seek requests during scrubbing.
    """
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    if job['status'] != 'completed':
        return jsonify({'error': 'Job not completed'}), 400
        
    temp_output = job['output_path']
    input_path = job['input_path']
    playback_path = job.get('playback_path')

    # Prefer processed output, but gracefully fall back to the original upload
    # when encoder/output generation fails unexpectedly.
    if playback_path and os.path.exists(playback_path) and os.path.getsize(playback_path) > 0:
        result_video_path = playback_path
    else:
        result_video_path = input_path if PRESERVE_OUTPUT_VIDEO_QUALITY else temp_output
    if not os.path.exists(result_video_path) or os.path.getsize(result_video_path) <= 0:
        if os.path.exists(input_path) and os.path.getsize(input_path) > 0:
            print(
                f"[WARN] Job {job_id}: processed output missing/empty "
                f"({result_video_path}), serving original input instead."
            )
            result_video_path = input_path
        else:
            return jsonify({'error': 'Result video file is unavailable'}), 500

    # conditional=True enables HTTP range requests for seeking/scrubbing.
    response = make_response(
        send_file(result_video_path, mimetype='video/mp4', conditional=True)
    )
    # Keep header tiny to avoid client/header-size limits.
    response.headers['X-Snooker-Stats-Ref'] = job_id
    return response

@app.route('/job_stats/<job_id>', methods=['GET'])
def get_job_stats(job_id):
    """Return the full statistics payload for a completed job."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job.get('status') != 'completed':
        return jsonify({'error': 'Job not completed'}), 400

    return jsonify(job.get('stats', {}))

@app.route('/job_timeline/<job_id>', methods=['GET'])
def get_job_timeline(job_id):
    """Return only timeline data for a completed job (lighter than full stats)."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job.get('status') != 'completed':
        return jsonify({'error': 'Job not completed'}), 400

    stats = job.get('stats', {})
    timeline = stats.get('timeline', []) if isinstance(stats, dict) else []
    summary = stats.get('summary', {}) if isinstance(stats, dict) else {}
    return jsonify({
        'job_id': job_id,
        'timeline': timeline if isinstance(timeline, list) else [],
        'summary': summary if isinstance(summary, dict) else {}
    })

@app.route('/job_timeline_chunk/<job_id>', methods=['GET'])
def get_job_timeline_chunk(job_id):
    """Return timeline data in chunks for large jobs to prevent connection drops."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = jobs[job_id]
    if job.get('status') != 'completed':
        return jsonify({'error': 'Job not completed'}), 400

    stats = job.get('stats', {})
    timeline = stats.get('timeline', []) if isinstance(stats, dict) else []
    summary = stats.get('summary', {}) if isinstance(stats, dict) else {}
    if not isinstance(timeline, list):
        timeline = []
    if not isinstance(summary, dict):
        summary = {}

    try:
        offset = int(request.args.get('offset', 0))
    except Exception:
        offset = 0
    try:
        limit = int(request.args.get('limit', 500))
    except Exception:
        limit = 500

    offset = max(0, offset)
    limit = max(1, min(limit, 1000))

    total = len(timeline)
    end = min(total, offset + limit)
    chunk = timeline[offset:end]

    payload = {
        'job_id': job_id,
        'offset': offset,
        'limit': limit,
        'total': total,
        'has_more': end < total,
        'timeline': chunk,
    }
    if offset == 0:
        payload['summary'] = summary
    return jsonify(payload)

@app.route('/video_predict', methods=['POST'])
def video_predict():
    """Legacy alias for start_video_predict."""
    return start_video_predict()

live_sessions = {}

@app.route('/live/start', methods=['POST'])
def live_start():
    """Create a new live analysis session with its own game tracker and coordinate tracker."""
    session_id = str(uuid.uuid4())
    
    pocket_points = []
    try:
        pocket_str = request.form.get('pocket_points')
        if pocket_str:
            pocket_points = json.loads(pocket_str)
            print(f"[LIVE] Received {len(pocket_points)} pocket calibration points.")
    except: pass

    tracker = SnookerGameTracker(fps=10.0)
    coord_tracker = BallCoordinateTracker(
        motion_threshold_px=2.0,
        max_match_distance_px=50.0,
        fps=10.0,
        patience_frames=5,
    )
    
    if pocket_points and len(pocket_points) == 6:
        # Calibration coordinates from the UI are already in the 1280-wide space
        # used by /frame_detections; live_frame resizes to 1280px to match.
        coord_tracker.pocket_points_2d = [
            (float(p['x']), float(p['y']))
            for p in pocket_points
        ]
        print(f"[LIVE] Pocket calibration set: {coord_tracker.pocket_points_2d}")

    # Advanced temporal tracking state for this session.
    fps = 10.0
    hold_frames = max(2, int(round(fps * 0.35)))
    singleton_keys = ['yellow-ball', 'green-ball', 'brown-ball', 'blue-ball', 'pink-ball', 'black-ball', 'white-ball']
    
    live_sessions[session_id] = {
        'game': tracker,
        'coord': coord_tracker,
        'recent_singletons': {k: {'miss': hold_frames + 1, 'box_xyxy': None, 'conf': 0.0} for k in singleton_keys},
        'red_missing_frames': hold_frames + 1,
        'recent_red_count': 0,
        'last_singleton_positions': {k: None for k in singleton_keys},
        'frame_count': 0,
        'hold_frames': hold_frames,
        'singleton_keys': singleton_keys,
        'fps': fps
    }
    return jsonify({'session_id': session_id})

@app.route('/live/frame', methods=['POST'])
def live_frame():
    """Process a single live camera frame and return annotated image with game stats."""
    session_id = request.form.get('session_id')
    if not session_id or session_id not in live_sessions:
        return jsonify({'error': 'Invalid or expired session'}), 400
        
    session_data = live_sessions[session_id]
    tracker = session_data.get('game')
    coord_tracker = session_data.get('coord')
    recent_singletons = session_data.get('recent_singletons')
    red_missing_frames = session_data.get('red_missing_frames')
    recent_red_count = session_data.get('recent_red_count')
    last_singleton_positions = session_data.get('last_singleton_positions')
    frame_count = session_data.get('frame_count')
    hold_frames = session_data.get('hold_frames')
    singleton_keys = session_data.get('singleton_keys')
    
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file'}), 400
        
    img_bytes = file.read()
    nparr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    target_width = 1280
    height, width = frame.shape[:2]
    if width > target_width:
        aspect_ratio = height / width
        new_height = int(target_width * aspect_ratio)
        frame = cv2.resize(frame, (target_width, new_height))
        height, width = frame.shape[:2]

    yolo_frame = frame
    annotated_frame = frame.copy()

    # Serialise inference to prevent concurrent threads corrupting the shared
    # ByteTrack internal state. For a single-user deployment this lock is
    # sufficient; multi-user deployments would need per-instance model objects.
    with _inference_lock:
        results = model.track(
            yolo_frame,
            tracker='bytetrack.yaml',
            persist=(frame_count > 0),
            conf=0.15,
            verbose=False
        )
    
    if not results or len(results) == 0:
        return jsonify({'error': 'No tracking results'}), 500
        
    result = results[0]
    names = result.names
    has_balls = False
    detections = []
    
    hsv_current_frame = cv2.cvtColor(yolo_frame, cv2.COLOR_BGR2HSV)
    
    for box in result.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        cls_name = names[cls_id]
        predicted_label = cls_name.lower()
        key = predicted_label
        box_xyxy = box.xyxy[0].tolist()

        hsv_avg = get_box_center_hsv(
            yolo_frame,
            box_xyxy,
            predicted_label=predicted_label,
            hsv_frame=hsv_current_frame
        )
        key = correct_label_by_hsv(key, hsv_avg, conf)
        
        bx1, by1, bx2, by2 = map(int, box_xyxy)
        center = ((bx1 + bx2) / 2.0, (by1 + by2) / 2.0)
        detections.append({
            'key': key,
            'conf': conf,
            'box_xyxy': [bx1, by1, bx2, by2],
            'center': center
        })

    # Ensure one physical ball contributes only one color label.
    detections = suppress_multi_color_same_ball(detections)
    detections.sort(key=lambda x: x['conf'], reverse=True)

    # Singleton identity lock: re-map low-conf labels to nearby known singleton positions.
    singleton_match_radius = max(18, int(min(width, height) * 0.06))
    singleton_lock_conf_max = 0.65
    frame_taken_singletons = set()
    
    for d in detections:
        key = d['key']
        if key not in singleton_keys:
            continue

        cx, cy = d['center']
        best_label = None
        best_dist = float('inf')

        for label, pos in last_singleton_positions.items():
            if pos is None or label in frame_taken_singletons:
                continue
            dx = cx - pos[0]
            dy = cy - pos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist and dist <= singleton_match_radius:
                best_dist = dist
                best_label = label

        if best_label is not None and best_label != key and d['conf'] <= singleton_lock_conf_max:
            d['key'] = best_label

        if d['key'] in singleton_keys and d['key'] not in frame_taken_singletons:
            frame_taken_singletons.add(d['key'])
            
    current_stats = {
        'red-ball': 0, 'yellow-ball': 0, 'green-ball': 0,
        'brown-ball': 0, 'blue-ball': 0, 'pink-ball': 0,
        'black-ball': 0, 'white-ball': 0, 'colored-ball': 0,
        'total_score': 0
    }
    limits = {
        'red-ball': 15, 'yellow-ball': 1, 'green-ball': 1,
        'brown-ball': 1, 'blue-ball': 1, 'pink-ball': 1,
        'black-ball': 1, 'white-ball': 1
    }

    for key in singleton_keys:
        recent_singletons[key]['miss'] += 1
    red_missing_frames += 1
    
    accepted_detections = []
    banned_colors = tracker.get_banned_colors()
    
    for d in detections:
        key = d['key']
        
        if key in banned_colors:
            continue

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
            accepted_detections.append({
                'key': key,
                'conf': d['conf'],
                'box_xyxy': d['box_xyxy'],
                'persisted': False
            })

            if key in recent_singletons:
                recent_singletons[key]['miss'] = 0
                recent_singletons[key]['box_xyxy'] = d['box_xyxy']
                recent_singletons[key]['conf'] = float(d['conf'])
                bx1, by1, bx2, by2 = d['box_xyxy']
                last_singleton_positions[key] = ((bx1 + bx2) / 2.0, (by1 + by2) / 2.0)

    # Hold singleton colors briefly when moving blur causes missed frames.
    for key in singleton_keys:
        if key in banned_colors:
            continue

        if current_stats.get(key, 0) == 0:
            rs = recent_singletons[key]
            if rs['box_xyxy'] is not None and rs['miss'] <= hold_frames:
                current_stats[key] = 1
                has_balls = True
                accepted_detections.append({
                    'key': key,
                    'conf': rs['conf'],
                    'box_xyxy': rs['box_xyxy'],
                    'persisted': True
                })

    # Hold red count briefly only when it drops to zero for a moment.
    if current_stats.get('red-ball', 0) > 0:
        recent_red_count = current_stats['red-ball']
        red_missing_frames = 0
    elif recent_red_count > 0 and red_missing_frames <= hold_frames:
        current_stats['red-ball'] = recent_red_count
        has_balls = True

    if accepted_detections:
        for d in accepted_detections:
            x1, y1, x2, y2 = map(int, d['box_xyxy'])
            label = d['key']
            conf_val = float(d['conf'])
            color = BALL_DRAW_COLORS.get(label, (0, 255, 255))

            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
            text = f"{label} {conf_val:.2f}"
            text_y = y1 - 8 if y1 > 18 else y1 + 18
            cv2.putText(annotated_frame, text, (x1 + 1, text_y + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 2, cv2.LINE_AA)
            cv2.putText(annotated_frame, text, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    tracker_dets = []
    for d in accepted_detections:
        if d.get('persisted', False):
            continue
        x1, y1, x2, y2 = map(float, d['box_xyxy'])
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        radius = max(1.0, min(x2 - x1, y2 - y1) / 2.0)
        tracker_dets.append({
            'x': cx, 'y': cy, 'label': d.get('key'), 'radius': radius
        })

    active_tracks = coord_tracker.update(tracker_dets)
    potted_balls_this_frame = coord_tracker.get_potted_balls()
    
    any_moving = any(tr.get('status') == 'Moving' for tr in active_tracks)
    any_missing_in_zone = any(
        tr.get('in_zone', False) and 0 < tr.get('missed', 0) < coord_tracker.pot_frames
        for tr in coord_tracker.tracks.values()
    )
    is_shot_active = any_moving or any_missing_in_zone

    potted_balls_this_frame = [b for b in potted_balls_this_frame if str(b) != "None"]
             
    track_res = tracker.update(current_stats, potted_balls_this_frame, is_shot_active=is_shot_active, any_moving=any_moving)
    stable_counts = tracker.get_stable_counts()
    
    for key in current_stats:
        if key in ['total_score']: continue
        track_res[key] = stable_counts.get(key, current_stats.get(key, 0))
        
    track_res['game_phase'] = track_res.get('phase', 'Unknown')
    track_res['current_break'] = track_res.get('break', 0)
    
    if 'potted_1' in track_res:
         for k, v in track_res['potted_1'].items():
             track_res[f'p1_potted_{k}'] = v
    if 'potted_2' in track_res:
         for k, v in track_res['potted_2'].items():
             track_res[f'p2_potted_{k}'] = v

    track_res['potted_score'] = track_res.get('player1_score', 0) + track_res.get('player2_score', 0)
    if 'points_remaining' in track_res:
        track_res['potential_score'] = track_res['points_remaining']

    points_map = {
        'red-ball': 1, 'yellow-ball': 2, 'green-ball': 3,
        'brown-ball': 4, 'blue-ball': 5, 'pink-ball': 6,
        'black-ball': 7, 'white-ball': 0
    }
    vis = 0
    for key, count in current_stats.items():
        if key in points_map:
            vis += count * points_map[key]
            
    track_res['visible_score'] = vis
    
    session_data['red_missing_frames'] = red_missing_frames
    session_data['recent_red_count'] = recent_red_count
    session_data['frame_count'] += 1

    _, buffer = cv2.imencode('.jpg', annotated_frame)
    b64_img = base64.b64encode(buffer).decode('utf-8')
    
    return jsonify({
        'stats': track_res,
        'image': b64_img
    })


@app.route('/live/stop', methods=['POST'])
def live_stop():
    """Delete a live analysis session from memory.

    Expects a JSON body with:
      - session_id : the session ID returned by /live/start

    Called by the Flutter client when the user stops live tracking,
    preventing session objects from accumulating indefinitely.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id') or request.form.get('session_id')
    if session_id and session_id in live_sessions:
        live_sessions.pop(session_id)
        print(f"[LIVE] Session {session_id} removed.")
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'not_found'}), 404


if __name__ == '__main__':
    # host='0.0.0.0' allows access from other devices on the network.
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
