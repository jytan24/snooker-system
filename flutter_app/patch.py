import re
import os

filepath = R"C:\Users\tanji\OneDrive\Desktop\fyp_code\flutter_app\lib\main.dart"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Imports
if "import 'package:camera/camera.dart';" not in content:
    content = content.replace("import 'package:device_preview/device_preview.dart';", "import 'package:device_preview/device_preview.dart';\nimport 'package:camera/camera.dart';\nimport 'dart:async';")

# 2. Main func
if "List<CameraDescription> cameras = [];" not in content:
    content = content.replace("void main() {", """List<CameraDescription> cameras = [];

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    cameras = await availableCameras();
  } catch (e) {
    print('Error in fetching cameras: $e');
  }""")

# 3. Add live fields to State
if "CameraController? _cameraController;" not in content:
    state_injection = """
  // Live State
  CameraController? _cameraController;
  bool _isLiveTracking = false;
  String? _liveSessionId;
  Image? _cameraFrameResult;
  Timer? _liveTimer;
  Map<String, dynamic>? _liveStats;
  bool _isFrameProcessing = false;
"""
    content = content.replace("int _selectedIndex = 0; // 0 for Image, 1 for Video", "int _selectedIndex = 0; // 0: Video, 1: Image, 2: Live\n" + state_injection)

# 4. Init State & Dispose
if "void initState() {" not in content:
    init_state_injection = """
  @override
  void initState() {
    super.initState();
    if (cameras.isNotEmpty) {
      _cameraController = CameraController(cameras.first, ResolutionPreset.high, enableAudio: false);
      _cameraController!.initialize().then((_) {
        if (!mounted) return;
        setState(() {});
      });
    }
  }

"""
    content = content.replace("String get serverUrl {", init_state_injection + "  String get serverUrl {")
    
if "_cameraController?.dispose();" not in content:
    content = content.replace("_chewieController?.dispose();", "_chewieController?.dispose();\n    _cameraController?.dispose();\n    _liveTimer?.cancel();")

# 5. Bottom Nav Bar
new_nav = """        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.videocam), label: 'Video'),
          BottomNavigationBarItem(icon: Icon(Icons.image), label: 'Image'),
          BottomNavigationBarItem(icon: Icon(Icons.camera), label: 'Live Tab'),
        ],
      ),
      body: _selectedIndex == 0 
          ? _buildVideoBody() 
          : _selectedIndex == 1 
              ? _buildImageBody() 
              : _buildLiveBody(),"""
old_nav = """        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.videocam), label: 'Video'),
          BottomNavigationBarItem(icon: Icon(Icons.image), label: 'Image'),
        ],
      ),
      body: _selectedIndex == 0 ? _buildVideoBody() : _buildImageBody(),"""
content = content.replace(old_nav, new_nav)

# 6. Add _buildLiveBody function literally at the bottom inside state or before last brace
if "Widget _buildLiveBody(" not in content:
    live_body = """
  Future<void> _startLiveTracking() async {
     try {
       var req = await http.post(Uri.parse('$serverUrl/live/start'));
       if (req.statusCode == 200) {
          var data = jsonDecode(req.body);
          setState(() {
             _liveSessionId = data['session_id'];
             _isLiveTracking = true;
             _liveStats = null;
             _cameraFrameResult = null;
          });
          
          _liveTimer = Timer.periodic(const Duration(milliseconds: 500), (timer) async {
             if (!_isLiveTracking || !mounted) {
                timer.cancel();
                return;
             }
             if (_isFrameProcessing) return;
             if (_cameraController == null || !_cameraController!.value.isInitialized) return;
             
             _isFrameProcessing = true;
             try {
                XFile file = await _cameraController!.takePicture();
                var request = http.MultipartRequest('POST', Uri.parse('$serverUrl/live/frame'));
                request.fields['session_id'] = _liveSessionId!;
                request.files.add(await http.MultipartFile.fromPath('file', file.path));
                
                var resp = await request.send();
                if (resp.statusCode == 200) {
                   String respStr = await resp.stream.bytesToString();
                   var body = jsonDecode(respStr);
                   if (body['image'] != null && mounted) {
                       var bytes = base64Decode(body['image']);
                       setState(() {
                           _cameraFrameResult = Image.memory(bytes, gaplessPlayback: true, fit: BoxFit.contain);
                           _liveStats = body['stats'];
                       });
                   }
                }
             } catch(e) {
                print("Live frame error: $e");
             } finally {
                _isFrameProcessing = false;
             }
          });
       }
     } catch (e) {
       print("Live tracking init error: $e");
     }
  }
  
  void _stopLiveTracking() {
      setState(() {
         _isLiveTracking = false;
         _liveTimer?.cancel();
         _liveTimer = null;
      });
  }

  Widget _buildLiveBody() {
      bool isCamReady = _cameraController != null && _cameraController!.value.isInitialized;
      
      return SingleChildScrollView(
         child: Column(
            children: [
                Card(
                  margin: const EdgeInsets.all(10),
                  elevation: 6,
                  clipBehavior: Clip.hardEdge,
                  child: Container(
                      height: 400,
                      width: double.infinity,
                      color: Colors.black,
                      child: Stack(
                         alignment: Alignment.center,
                         children: [
                            if (isCamReady && !_isLiveTracking && _cameraFrameResult == null)
                                CameraPreview(_cameraController!),
                                
                            if (_cameraFrameResult != null)
                                SizedBox(width: double.infinity, height: double.infinity, child: _cameraFrameResult!),
                                
                            if (!isCamReady)
                                const Center(child: CircularProgressIndicator()),
                                
                            Positioned(
                               bottom: 20,
                               child: _isLiveTracking
                                  ? ElevatedButton.icon(
                                      onPressed: _stopLiveTracking,
                                      icon: const Icon(Icons.stop),
                                      label: const Text("Stop Live Tracking"),
                                      style: ElevatedButton.styleFrom(backgroundColor: Colors.red, foregroundColor: Colors.white),
                                    )
                                  : ElevatedButton.icon(
                                      onPressed: isCamReady ? _startLiveTracking : null,
                                      icon: const Icon(Icons.play_arrow),
                                      label: const Text("Start Live Tracking"),
                                      style: ElevatedButton.styleFrom(backgroundColor: Colors.green, foregroundColor: Colors.white),
                                    ),
                            ),
                         ],
                      )
                  )
                ),
                
                if (_liveStats != null) ...[
                     _buildStats(_ensureScoreKeys(_liveStats!)),
                     const SizedBox(height: 20),
                ]
            ]
         )
      );
  }
}
"""
    # Remove the last closing brace of the file to append, then re-add
    content = content.rstrip()
    if content.endswith("}"):
        content = content[:-1] + live_body

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied to flutter app")
