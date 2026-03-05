import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'package:video_player/video_player.dart';
import 'package:chewie/chewie.dart';
import 'package:path_provider/path_provider.dart';
import 'package:device_preview/device_preview.dart';
import 'package:camera/camera.dart';
import 'dart:async';


List<CameraDescription> cameras = [];

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    cameras = await availableCameras();
  } catch (e) {
    print('Error in fetching cameras: $e');
  }
  runApp(
    DevicePreview(
      enabled: false, // Set to false to disable preview for better full-screen video support
      builder: (context) => const MyApp(),
    ),
  );
} 

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      useInheritedMediaQuery: true,
      locale: DevicePreview.locale(context),
      builder: DevicePreview.appBuilder,
      title: 'Snooker Intelligent Analyzer',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.green),
        useMaterial3: true,
      ),
      home: const MyHomePage(title: 'Snooker Intelligent Analyzer'),
    );
  }
}

class MyHomePage extends StatefulWidget {
  const MyHomePage({super.key, required this.title});

  final String title;

  @override
  State<MyHomePage> createState() => _MyHomePageState();
}

class _MyHomePageState extends State<MyHomePage> {
  // Image State
  File? _image;
  Image? _resultImage;
  Map<String, dynamic>? _counts;

  // Video State
  File? _videoFile;
  File? _processedVideoFile;
  VideoPlayerController? _videoController;
  ChewieController? _chewieController;
  List<Map<String, dynamic>>? _statsTimeline;
  String? _currentJobId; // Track current job for cancellation

  // Player Names
  String player1Name = "Player 1";
  String player2Name = "Player 2";

  bool _isLoading = false;
  double _progressValue = 0.0; // 0.0 to 1.0
  int? _initialPoints;
  int _selectedIndex = 0; // 0: Video, 1: Image, 2: Live

  // Live State
  CameraController? _cameraController;
  bool _isLiveTracking = false;
  bool _isInitializingCamera = false;
  String? _liveSessionId;
  Image? _cameraFrameResult;
  Timer? _liveTimer;
  Map<String, dynamic>? _liveStats;
  bool _isFrameProcessing = false;


  final ImagePicker _picker = ImagePicker();

  
  @override
  void initState() {
    super.initState();
    // Do NOT auto-initialize camera on app startup
    // _initCamera();
  }

  Future<void> _initCamera() async {
    setState(() {
      _isInitializingCamera = true;
    });

    if (cameras.isEmpty) {
        try {
            cameras = await availableCameras();
        } catch(e) {
            print("Failed to fetch cameras dynamically: $e");
        }
    }
    
    if (cameras.isNotEmpty) {
      // Use ResolutionPreset.medium (720p) for faster FPS/lower lag. High (1080p) is too slow for live tracking.
      _cameraController = CameraController(cameras.first, ResolutionPreset.medium, enableAudio: false);
      try {
        await _cameraController!.initialize();
      } catch (e) {
        print("Camera initialization error: $e");
      }
    }

    if (mounted) {
        setState(() {
            _isInitializingCamera = false;
        });
    }
  }

  String get serverUrl {
    // If you are running on a real Android device, change this IP to your PC's IP address!
    // Example: return "http://192.168.1.10:5000";
    if (Platform.isAndroid) {
       return "http://10.0.2.2:5000"; // Default for Android Emulator
    }
    // For Windows, macOS, Linux
    return "http://127.0.0.1:5000";
  }

  @override
  void dispose() {
    _videoController?.dispose();
    _chewieController?.dispose();
    _cameraController?.dispose();
    _liveTimer?.cancel();
    super.dispose();
  }

  Future<void> _pickImage(ImageSource source) async {
    final XFile? pickedFile = await _picker.pickImage(source: source);

    if (pickedFile != null) {
      setState(() {
        _image = File(pickedFile.path);
        _resultImage = null; // Clear previous result
        _counts = null;
      });
      _uploadImage(_image!);
    }
  }

  Future<void> _uploadImage(File imageFile) async {
    setState(() {
      _isLoading = true;
    });

    try {
      print("Attempting to connect to: $serverUrl/predict");
      // 1. Get Annotated Image
      var request = http.MultipartRequest('POST', Uri.parse('$serverUrl/predict'));
      request.files.add(await http.MultipartFile.fromPath('file', imageFile.path));
      var response = await request.send();


      if (response.statusCode == 200) {
        var bytes = await response.stream.toBytes();
        
        // 2. Get Stats
        var countRequest = http.MultipartRequest('POST', Uri.parse('$serverUrl/stats'));
        countRequest.files.add(await http.MultipartFile.fromPath('file', imageFile.path));
        
        var countResponse = await countRequest.send();
        
        Map<String, dynamic>? counts;
        if (countResponse.statusCode == 200) {
           var countBytes = await countResponse.stream.toBytes();
           counts = jsonDecode(utf8.decode(countBytes));
        }

        setState(() {
          _resultImage = Image.memory(bytes);
          _counts = counts;
          _isLoading = false;
        });
      } else {
        print('Server Error: ${response.statusCode}');
        setState(() {
          _isLoading = false;
        });
        _showErrorDialog('Server returned ${response.statusCode}');
      }
    } catch (e) {
      print('Connection Error: $e');
      setState(() {
        _isLoading = false;
      });
      _showErrorDialog(e.toString());
    }
  }

  Future<void> _pickVideo(ImageSource source) async {
    final XFile? pickedFile = await _picker.pickVideo(source: source);

    if (pickedFile != null) {
      File file = File(pickedFile.path);
      
      // Initialize player with ORIGINAL video immediately, muted while processing
      await _initializePlayer(file, isMuted: true);
      
      setState(() {
        _videoFile = file;
        _processedVideoFile = null;
        _isLoading = true;
      });
      
      // Directly start processing without annotation
      _processVideo(file);
    }
  }

  Future<void> _processVideo(File videoFile) async {
    // Note: Do NOT set _isLoading=true here again if already set, but ensure progress is reset
    setState(() {
      _processedVideoFile = null;
      _progressValue = 0.0;
      _initialPoints = null;
    });

    try {
      print("Starting video processing job...");
      var request = http.MultipartRequest('POST', Uri.parse('$serverUrl/start_video_predict'));
      request.files.add(await http.MultipartFile.fromPath('file', videoFile.path));
      
      // Removed custom mapping and overrides logic
      
      var response = await request.send();

      if (response.statusCode == 200) {
        // 1. Get Job ID
        String respStr = await response.stream.bytesToString();
        var jobData = jsonDecode(respStr);
        String jobId = jobData['job_id'];
        
        if (mounted) {
           setState(() {
               _currentJobId = jobId; 
           });
        }
        print("Job started: $jobId");
        
        // 2. Poll for Status
        bool completed = false;
        while (!completed) {
           // Check if cancelled locally
           if (!mounted || _currentJobId == null) return;

           await Future.delayed(const Duration(seconds: 1));
           
           var statusResp = await http.get(Uri.parse('$serverUrl/job_status/$jobId'));
           if (statusResp.statusCode == 200) {
               var statusData = jsonDecode(statusResp.body);
               String status = statusData['status'];
               
               if (status == 'cancelled') return; // Server says cancelled

               // Update Progress
               if (statusData['progress'] != null) {
                   if (mounted) {
                       setState(() {
                           _progressValue = (statusData['progress'] as int) / 100.0;
                             if (statusData['initial_points'] != null) {
                                 _initialPoints = statusData['initial_points'] as int;
                             }                         });
                     }
                 }

                 if (status == 'completed') {                   completed = true;
               } else if (status == 'error') {
                   throw Exception("Server Error: ${statusData['error']}");
               }
           } else {
               print("Error checking status: ${statusResp.statusCode}");
           }
        }
        
        // 3. Download Result
        print("Job completed. Downloading result...");
        var resultReq = http.Request('GET', Uri.parse('$serverUrl/job_result/$jobId'));
        var resultResp = await resultReq.send();
        
        if (resultResp.statusCode == 200) {
             Map<String, dynamic>? counts;
             // Check headers (case-insensitive search)
             resultResp.headers.forEach((k, v) {
                if (k.toLowerCase() == 'x-snooker-stats') {
                    try { counts = jsonDecode(v); } catch(e) { print(e); }
                }
             });
             
             var bytes = await resultResp.stream.toBytes();

             final directory = await getTemporaryDirectory();
             final uniqueId = DateTime.now().millisecondsSinceEpoch.toString(); 
             final processedPath = '${directory.path}/processed_video_$uniqueId.mp4';
             final processedFile = File(processedPath);
             await processedFile.writeAsBytes(bytes);
             
             // Re-initialize player with PROCESSED video
             await _initializePlayer(processedFile);

             if (mounted) {
                setState(() {
                  _processedVideoFile = processedFile;
                  _isLoading = false;
                  
                  if (counts != null && counts!.containsKey('summary')) {
                      _counts = counts!['summary'];
                      if (counts!.containsKey('timeline')) {
                           _statsTimeline = List<Map<String, dynamic>>.from(counts!['timeline']);
                      }
                  } else {
                      _counts = counts;
                  }
                });
             }
        } else {
            throw Exception("Failed to download result: ${resultResp.statusCode}");
        }

      } else {
        String errorBody = await response.stream.bytesToString();
        setState(() { _isLoading = false; });
        _showErrorDialog('Start Job Failed: ${response.statusCode}\n$errorBody');
      }
    } catch (e) {
      print('Connection Error: $e');
      setState(() {
        _isLoading = false;
      });
      _showErrorDialog(e.toString());
    }
  }

  Future<void> _cancelProcessing() async {
      String? id = _currentJobId;
      print("User cancelling job $id...");
      
      // Stop UI immediately
      if (mounted) {
          setState(() {
             _isLoading = false;
             _progressValue = 0.0;
             _currentJobId = null; 
          });
      }
      
      if (id != null) {
          try {
             await http.post(Uri.parse('$serverUrl/cancel_job/$id'));
          } catch(e) {
             print("Error notifying server of cancellation: $e");
          }
      }
  }

  void _disposeVideoControllers() {
    _videoController?.removeListener(_onVideoPositionChanged);
    _videoController?.dispose();
    _chewieController?.dispose();
    _cameraController?.dispose();
    _liveTimer?.cancel();
    _videoController = null;
    _chewieController = null;
    // Do NOT clear _statsTimeline here immediately if we want to retain it during swap, 
    // but in this flow we are starting new video, so it is safer to clear.
    _statsTimeline = null;
  }
  
  void _onVideoPositionChanged() {
    // Check various null states
    if (_videoController == null || !_videoController!.value.isInitialized) return;
    
    // Convert to seconds
    final double currentSeconds = _videoController!.value.position.inMilliseconds / 1000.0;
    
    if (_statsTimeline == null || _statsTimeline!.isEmpty) return;
     
     Map<String, dynamic>? currentStats;
     
     // Find the stats closest to current timestamp 
     // The timeline is sorted by timestamp usually.
     for (var stat in _statsTimeline!) {
         double t = 0.0;
         if (stat.containsKey('timestamp')) {
              var val = stat['timestamp'];
              if (val is int) {
                t = val.toDouble();
              } else if (val is double) t = val;
         }

         // Keep taking stats as long as they are <= currentSeconds
         // This assumes timeline is sorted.
         if (t <= currentSeconds) {
             currentStats = stat;
         } else {
             // Stop once we surpass the current time
             break; 
         }
     }
     
     if (currentStats != null) {
         // Optimization: Only setState if reference changed (or content)
         // Assuming new map reference per frame from server
         if (currentStats != _counts) {
             setState(() {
                _counts = _ensureScoreKeys(currentStats!);
             });
         }
     }
  }

  // Helper to ensure keys exist even if server didn't send them (e.g. older cached result)
  Map<String, dynamic> _ensureScoreKeys(Map<String, dynamic> input) {
      if (!input.containsKey('player1_score')) input['player1_score'] = 0;
      if (!input.containsKey('player2_score')) input['player2_score'] = 0;
      if (!input.containsKey('current_player')) input['current_player'] = 1;
      return input;
  }

  Future<void> _initializePlayer(File file, {bool isMuted = false}) async {
    // Dispose previous if any
    _disposeVideoControllers();
    
    _videoController = VideoPlayerController.file(file);
    await _videoController!.initialize();
    
    // Add listener for real-time score updates
    _videoController!.addListener(_onVideoPositionChanged);
    
    if (isMuted) {
      await _videoController!.setVolume(0.0);
    }

    _chewieController = ChewieController(
      videoPlayerController: _videoController!,
      autoPlay: true,
      looping: isMuted, // Loop original video while processing
      aspectRatio: _videoController!.value.aspectRatio,
      allowFullScreen: true,
      allowPlaybackSpeedChanging: true,
      deviceOrientationsOnEnterFullScreen: [
        DeviceOrientation.landscapeLeft,
        DeviceOrientation.landscapeRight,
      ],
      deviceOrientationsAfterFullScreen: [DeviceOrientation.portraitUp],
    );
     // Update UI
    if (mounted) setState(() {});
  }

  void _showErrorDialog(String message) {
    if (!mounted) return;
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Error'),
        content: Text("Failed to connect to server ($serverUrl).\n\n"
            "Ensure:\n"
            "1. Server.py is running on PC.\n"
            "2. If on Android, PC and Phone are on same WiFi.\n"
            "3. If on Windows, server is on 127.0.0.1.\n\n"
            "Error Details: $message"),
        actions: <Widget>[
          TextButton(
            child: const Text('Okay'),
            onPressed: () {
              Navigator.of(ctx).pop();
            },
          )
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.title),
        backgroundColor: Theme.of(context).colorScheme.inversePrimary,
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _selectedIndex,
        onTap: (index) {
          setState(() {
            _selectedIndex = index;
          });
        },
        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.videocam), label: 'Video'),
          BottomNavigationBarItem(icon: Icon(Icons.image), label: 'Image'),
          BottomNavigationBarItem(icon: Icon(Icons.camera), label: 'Live Tab'),
        ],
      ),
      body: _selectedIndex == 0 
          ? _buildVideoBody() 
          : _selectedIndex == 1 
              ? _buildImageBody() 
              : _buildLiveBody(),
      // Removed FloatingActionButton for cleaner UI on both tabs
    );
  }

  Widget _buildImageBody() {
    if (_isLoading) {
      return const Center(child: CircularProgressIndicator());
    }

    bool hasImage = _image != null;

    return SingleChildScrollView(
      child: Column(
        children: <Widget>[
          const SizedBox(height: 10),
          // Image Widget Container
          Card(
            margin: const EdgeInsets.symmetric(horizontal: 10.0, vertical: 5.0),
            elevation: 6,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            clipBehavior: Clip.hardEdge,
            child: Container(
              constraints: const BoxConstraints(minHeight: 250, maxHeight: 400),
              width: double.infinity,
              alignment: Alignment.center,
              color: Colors.black, // Dark background for media
              child: hasImage
                  ? (_resultImage ?? Image.file(_image!))
                  : Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(Icons.image_outlined, size: 64, color: Colors.white24),
                        const SizedBox(height: 20),
                        ElevatedButton.icon(
                          onPressed: () => _pickImage(ImageSource.gallery),
                          icon: const Icon(Icons.add_a_photo),
                          label: const Text("Select Image to Scan"),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: Colors.white,
                            foregroundColor: Colors.black,
                          ),
                        ),
                      ],
                    ),
            ),
          ),
          
          const SizedBox(height: 10),

          // Stats at the Bottom (Always show, empty if null)
          _buildImageStats(hasImage && _counts != null ? _counts! : {}),
          // _buildManualControls(), // Hide manual controls for image mode as it's static

          const SizedBox(height: 20),
          
          if (hasImage)
            ElevatedButton.icon(
              onPressed: () => _pickImage(ImageSource.gallery),
              icon: const Icon(Icons.image_search),
              label: const Text("Select New Image"),
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
              ),
            ),
            
          const SizedBox(height: 20),
        ],
      ),
    );
  }

  Widget _buildImageStats(Map<String, dynamic> counts) {
    return Card(
      margin: const EdgeInsets.all(12),
      elevation: 4,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            const Text("Image Analysis", style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const Divider(),
            
            // Score Summary
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: Colors.blue.shade50,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.blue.shade200),
              ),
              child: Column(
                children: [
                   if (counts.containsKey('visible_score'))
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text("Visible Points:", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                      Text("${counts['visible_score']}", style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 20, color: Colors.indigo)),
                    ], 
                  ),
                  const SizedBox(height: 5),
                  if (counts.containsKey('potential_score'))
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text("Points Remaining:", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                      Text("${counts['potential_score']}", style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 20, color: Colors.blue)),
                    ], 
                  ),
                ],
              ),
            ),
            
            const SizedBox(height: 15),
            const Text("Detected Balls", style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const Divider(),
            
            _buildStatRow("Reds", counts['red-ball'], Colors.red),
            _buildStatRow("Yellow", counts['yellow-ball'], Colors.amber),
            _buildStatRow("Green", counts['green-ball'], Colors.green),
            _buildStatRow("Brown", counts['brown-ball'], Colors.brown),
            _buildStatRow("Blue", counts['blue-ball'], Colors.blue),
            _buildStatRow("Pink", counts['pink-ball'], Colors.pinkAccent),
            _buildStatRow("Black", counts['black-ball'], Colors.black),
            _buildStatRow("White", counts['white-ball'], Colors.grey),
          ],
        ),
      ),
    ); 
  }

  Widget _buildVideoBody() {
    // Determine what to show in the player area
    // 1. Processed video if done
    // 2. Original video if loading/processing
    // 3. Placeholder if nothing picked
    
    // We now have _chewieController active even during _isLoading if _videoFile is set
    bool hasPlayer = _chewieController != null && _chewieController!.videoPlayerController.value.isInitialized;
    
    Widget playerWidget;
    
    if (hasPlayer) {
         playerWidget = AspectRatio(
            aspectRatio: _videoController!.value.aspectRatio,
            child: Chewie(controller: _chewieController!),
         );
    } else {
         playerWidget = Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
                 const Icon(Icons.video_camera_back_outlined, size: 64, color: Colors.white24),
                 const SizedBox(height: 20),
                 ElevatedButton.icon(
                   onPressed: () => _pickVideo(ImageSource.gallery),
                   icon: const Icon(Icons.add),
                   label: const Text("Select Video to Track"),
                   style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.white,
                      foregroundColor: Colors.black,
                      minimumSize: const Size(220, 45),
                   ),
                 ),
            ],
         );
    }

    return SingleChildScrollView(
         child: Column(
           children: [
              const SizedBox(height: 10),
              // Video Widget Container with Stack for Overlay
              Card(
                margin: const EdgeInsets.symmetric(horizontal: 10.0, vertical: 5.0),
                elevation: 6,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                clipBehavior: Clip.hardEdge,
                child: Container(
                  height: 300, 
                  width: double.infinity,
                  alignment: Alignment.center,
                  color: Colors.black,
                  child: Stack(
                      alignment: Alignment.center,
                      children: [
                          playerWidget,
                          
                          // Loading Overlay
                          if (_isLoading)
                              Container(
                                  color: Colors.black54,
                                  width: double.infinity,
                                  height: double.infinity,
                                  child: Column(
                                      mainAxisAlignment: MainAxisAlignment.center,
                                      children: [
                                          const CircularProgressIndicator(color: Colors.white),
                                          const SizedBox(height: 20),
                                          SizedBox(
                                              width: 200, 
                                              child: LinearProgressIndicator(value: _progressValue, color: Colors.green, backgroundColor: Colors.white24)
                                          ),
                                          const SizedBox(height: 10),
                                          Text("Processing... ${(_progressValue * 100).toInt()}%", style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),                                            if (_initialPoints != null) ...[
                                                const SizedBox(height: 10),
                                                const Text("Detected Maximum Info:", style: TextStyle(color: Colors.yellow, fontWeight: FontWeight.bold)),
                                                Text("Maximum Initial Points on Table: $_initialPoints", style: const TextStyle(color: Colors.yellowAccent, fontSize: 16)),
                                            ],                                          const Padding(
                                            padding: EdgeInsets.all(8.0),
                                            child: Text("Calibrating table state & tracking...", style: TextStyle(color: Colors.white70, fontSize: 12)),
                                          ),
                                          const SizedBox(height: 15),
                                          ElevatedButton(
                                              onPressed: _cancelProcessing,
                                              style: ElevatedButton.styleFrom(
                                                  backgroundColor: Colors.red.withOpacity(0.8),
                                                  foregroundColor: Colors.white
                                              ),
                                              child: const Text("Cancel"),
                                          )
                                      ]
                                  ),
                              )
                      ],
                  ),
                ),
              ),
             const SizedBox(height: 10),
             
             // Always show stats board (empty if no data yet)
             _buildStats(hasPlayer && _counts != null ? _counts! : {}),
             
             _buildManualControls(),
             
             const SizedBox(height: 20),
             if (hasPlayer && !_isLoading) ...[
                 ElevatedButton.icon(
                   onPressed: () => _pickVideo(ImageSource.gallery),
                   icon: const Icon(Icons.upload_file),
                   label: const Text("Upload New Video"),
                   style: ElevatedButton.styleFrom(
                     padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
                     minimumSize: const Size(220, 45),
                   ),
                 ),
             ],
             const SizedBox(height: 20),
           ],
         ),
       );
  }



  Widget _buildStats(Map<String, dynamic> counts) {
    int p1 = counts['player1_score'] ?? 0;
    int p2 = counts['player2_score'] ?? 0;
    int currentPlayer = counts['current_player'] ?? 1;
    int currentBreak = counts['current_break'] ?? 0;

    return Card(
      margin: const EdgeInsets.all(12),
      elevation: 4,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            const Text("Snooker Scoreboard", style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const Divider(),
            
            // Player Scores
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _buildPlayerScore(1, p1, currentPlayer == 1),
                Container(width: 1, height: 50, color: Colors.grey),
                _buildPlayerScore(2, p2, currentPlayer == 2),
              ],
            ),
            const SizedBox(height: 15),

            // Score Summary
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: Colors.green.shade50,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.green.shade200),
              ),
              child: Column(
                children: [
                   if (counts['potted_score'] != null)
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text("Total Potted:", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                      Text("${counts['potted_score']}", style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 20, color: Colors.indigo)),
                    ], 
                  ),
                  const SizedBox(height: 5),
                  if (counts['potential_score'] != null)
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text("Points Remaining:", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                      Text("${counts['potential_score']}", style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 20, color: Colors.blue)),
                    ], 
                  ),
                ],
              ),
            ),
            
            const SizedBox(height: 15),
            const Text("Potted (History)", style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const Divider(),
            
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    children: [
                       Text(player1Name, style: const TextStyle(fontWeight: FontWeight.bold, decoration: TextDecoration.underline)),
                       const SizedBox(height: 5),
                       _buildStatRow("Red", counts['p1_potted_red-ball'], Colors.red),
                       _buildStatRow("Yellow", counts['p1_potted_yellow-ball'], Colors.amber),
                       _buildStatRow("Green", counts['p1_potted_green-ball'], Colors.green),
                       _buildStatRow("Brown", counts['p1_potted_brown-ball'], const Color.fromARGB(255, 95, 63, 52)),
                       _buildStatRow("Blue", counts['p1_potted_blue-ball'], Colors.blue),
                       _buildStatRow("Pink", counts['p1_potted_pink-ball'], Colors.pinkAccent),
                       _buildStatRow("Black", counts['p1_potted_black-ball'], Colors.black),
                    ],
                  ),
                ),
                Container(width: 1, height: 200, color: Colors.grey.shade300),
                Expanded(
                  child: Column(
                    children: [
                       Text(player2Name, style: const TextStyle(fontWeight: FontWeight.bold, decoration: TextDecoration.underline)),
                       const SizedBox(height: 5),
                       _buildStatRow("Red", counts['p2_potted_red-ball'], Colors.red),
                       _buildStatRow("Yellow", counts['p2_potted_yellow-ball'], Colors.amber),
                       _buildStatRow("Green", counts['p2_potted_green-ball'], Colors.green),
                       _buildStatRow("Brown", counts['p2_potted_brown-ball'], Colors.brown),
                       _buildStatRow("Blue", counts['p2_potted_blue-ball'], Colors.blue),
                       _buildStatRow("Pink", counts['p2_potted_pink-ball'], Colors.pinkAccent),
                       _buildStatRow("Black", counts['p2_potted_black-ball'], Colors.black),
                    ],
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildPlayerScore(int id, int score, bool isActive) {
      String playerName = (id == 1) ? player1Name : player2Name;
      
      return GestureDetector(
        onTap: () => _editPlayerName(id),
        child: Column(
            children: [
                Row(
                   mainAxisSize: MainAxisSize.min,
                   children: [
                       Text(playerName, style: TextStyle(
                          fontSize: 16, 
                          fontWeight: isActive ? FontWeight.bold : FontWeight.normal,
                          color: isActive ? Colors.black : Colors.grey
                         )),
                       const SizedBox(width: 4),
                       const Icon(Icons.edit, size: 12, color: Colors.grey),
                   ],
                ),
                const SizedBox(height: 5),
                Container(
                    padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
                    decoration: BoxDecoration(
                        color: isActive ? Colors.green : Colors.grey.shade300,
                        borderRadius: BorderRadius.circular(10),
                        boxShadow: isActive ? [const BoxShadow(color: Colors.green, blurRadius: 5)] : []
                    ),
                    child: Text("$score", style: TextStyle(
                        fontSize: 24, 
                        fontWeight: FontWeight.bold,
                        color: isActive ? Colors.white : Colors.black54
                    )),
                ),
                if (isActive)
                   const Padding(
                     padding: EdgeInsets.only(top: 4.0),
                     child: Text("Playing", style: TextStyle(fontSize: 10, color: Colors.green, fontWeight: FontWeight.bold)),
                   )
            ],
        ),
      );
  }

  void _editPlayerName(int id) {
    TextEditingController controller = TextEditingController(text: (id == 1) ? player1Name : player2Name);
    showDialog(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: Text("Edit Player $id Name"),
          content: TextField(
            controller: controller,
            decoration: const InputDecoration(labelText: "Name"),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text("Cancel"),
            ),
            TextButton(
              onPressed: () {
                setState(() {
                  if (id == 1) {
                    player1Name = controller.text;
                  } else {
                    player2Name = controller.text;
                  }
                });
                Navigator.pop(context);
              },
              child: const Text("Save"),
            ),
          ],
        );
      },
    );
  }

  Widget _buildStatRow(String label, dynamic count, Color color) {
    if (count == null || count == 0) return const SizedBox.shrink(); // Hide if 0
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4.0, horizontal: 8.0),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Row(
            children: [
              Container(width: 16, height: 16, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
              const SizedBox(width: 8),
              Text(label, style: const TextStyle(fontSize: 16)),
            ],
          ),
          Text("x$count", style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        ],
      ),
    );
  }

  Widget _buildManualControls() {
    if (_counts == null) return const SizedBox.shrink();

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      elevation: 3,
      child: ExpansionTile(
        title: const Text("Manual Adjustments",
            style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
        children: [
          const Padding(
            padding: EdgeInsets.all(8.0),
            child: Text("Tap '+' to add potted balls, '-' to subtract (Current Player).",
                style: TextStyle(fontSize: 12, color: Colors.grey)),
          ),
          _buildAdjustRow("Red Potted", 'red-ball', 1, Colors.red),
          _buildAdjustRow("Yellow Potted", 'yellow-ball', 2, Colors.amber),
          _buildAdjustRow("Green Potted", 'green-ball', 3, Colors.green),
          _buildAdjustRow("Brown Potted", 'brown-ball', 4, Colors.brown),
          _buildAdjustRow("Blue Potted", 'blue-ball', 5, Colors.blue),
          _buildAdjustRow("Pink Potted", 'pink-ball', 6, Colors.pinkAccent),
          _buildAdjustRow("Black Potted", 'black-ball', 7, Colors.black),
        ],
      ),
    );
  }

  Widget _buildAdjustRow(String label, String itemKey, int points, Color color) {
    int currentPlayer = _counts!['current_player'] ?? 1;
    String fullKey = 'p${currentPlayer}_potted_$itemKey';
    
    int count = _counts![fullKey] ?? 0;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16.0, vertical: 6.0),
      child: Row(
        children: [
          Container(
            width: 14,
            height: 14,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 12),
          Expanded(child: Text(label, style: const TextStyle(fontSize: 15))),
          IconButton(
            icon: const Icon(Icons.remove_circle_outline),
            color: Colors.red,
            onPressed: () => _updateManualCount(itemKey, -1, points),
          ),
          SizedBox(
            width: 30,
            child: Center(
              child: Text(
                "$count",
                style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
              ),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.add_circle_outline),
            color: Colors.green,
            onPressed: () => _updateManualCount(itemKey, 1, points),
          ),
        ],
      ),
    );
  }

  void _updateManualCount(String itemKey, int change, int pointsPerBall) {
    if (_counts == null) return;

    setState(() {
      int currentPlayer = _counts!['current_player'] ?? 1;
      String fullKey = 'p${currentPlayer}_potted_$itemKey';
      
      int current = 0;
      if (_counts![fullKey] is int) {
        current = _counts![fullKey];
      } else if (_counts![fullKey] is double) {
        current = (_counts![fullKey] as double).toInt();
      }

      int newVal = current + change;
      if (newVal < 0) newVal = 0;
      _counts![fullKey] = newVal;

      // Update Player Score directly
      String scoreKey = 'player${currentPlayer}_score';
      int currentScore = _counts![scoreKey] ?? 0;
      _counts![scoreKey] = currentScore + (change * pointsPerBall);

      // Recalculate total potted score (sum of p1 and p2 scores)
      int p1 = _counts!['player1_score'] ?? 0;
      int p2 = _counts!['player2_score'] ?? 0;
      _counts!['potted_score'] = p1 + p2;
    });
  }

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
          
          // Use 100ms (10 FPS) instead of 33ms (30 FPS).
          // 30 FPS via HTTP+Disk IO causes camera preview lag on most devices.
          // 10 FPS is smooth enough for refereeing and prevents UI freezes.
          _liveTimer = Timer.periodic(const Duration(milliseconds: 100), (timer) async {
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
                
                // IMPORTANT: Delete temporary image file to prevent disk fill-up and IO lag
                try { await File(file.path).delete(); } catch(e) { print("Error deleting temp file: $e"); }

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
      
      String cameraButtonText = "Open Device Camera";
      if (Platform.isWindows || Platform.isMacOS || Platform.isLinux) {
          cameraButtonText = "Open Webcam";
      }

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
                                _isInitializingCamera
                                    ? const Center(
                                        child: Column(
                                          mainAxisAlignment: MainAxisAlignment.center,
                                          children: [
                                            CircularProgressIndicator(),
                                            SizedBox(height: 10),
                                            Text("Loading Camera...", style: TextStyle(color: Colors.white)),
                                          ],
                                        )
                                      )
                                    : Center(
                                        child: ElevatedButton.icon(
                                            onPressed: _initCamera,
                                            icon: const Icon(Icons.camera_alt),
                                            label: Text(cameraButtonText),
                                            style: ElevatedButton.styleFrom(
                                                backgroundColor: Colors.white,
                                                foregroundColor: Colors.black,
                                                padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
                                            ),
                                        ),
                                      ),
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
                    ),
                  ),
                ),
                if (_liveStats != null) ...[
                     _buildStats(_ensureScoreKeys(_liveStats!)),
                     const SizedBox(height: 20),
                ]
            ],
         ),
      );
  }
}
