import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'package:video_player/video_player.dart';
import 'package:chewie/chewie.dart';
import 'package:device_preview/device_preview.dart';
import 'package:camera/camera.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:async';

List<CameraDescription> cameras = [];

const String _serverUrlOverride =
  String.fromEnvironment('SERVER_URL', defaultValue: '');
const String _imageApiUrlOverride =
  String.fromEnvironment('IMAGE_API_URL', defaultValue: '');

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    cameras = await availableCameras();
  } catch (e) {
    print('Error in fetching cameras: $e');
  }
  runApp(
    DevicePreview(
      enabled:
          false, // Set to false to disable preview for better full-screen video support
      builder: (context) => const MyApp(),
    ),
  );
}

/// Root widget that configures the MaterialApp theme and DevicePreview.
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
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF0D1B4C),
        ),
        useMaterial3: true,
      ),
      home: const MyHomePage(title: 'Snooker Intelligent Analyzer'),
    );
  }
}

/// Main scaffold with a Video / Live bottom-nav layout.
class MyHomePage extends StatefulWidget {
  const MyHomePage({super.key, required this.title});

  final String title;

  @override
  State<MyHomePage> createState() => _MyHomePageState();
}

class _MyHomePageState extends State<MyHomePage> {
  Map<String, dynamic>? _counts;

  File? _videoFile;
  File? _processedVideoFile;
  VideoPlayerController? _videoController;
  ChewieController? _chewieController;
  List<Map<String, dynamic>>? _statsTimeline;
  String? _currentJobId;

  List<Offset> _pocketPoints = [];
  String player1Name = "Player 1";
  String player2Name = "Player 2";

  bool _isLoading = false;
  double _progressValue = 0.0;
  int? _initialPoints;
  int _selectedIndex = 0;

  CameraController? _cameraController;
  bool _isLiveTracking = false;
  bool _isInitializingCamera = false;
  String? _liveSessionId;
  Image? _cameraFrameResult;
  Timer? _liveTimer;
  Map<String, dynamic>? _liveStats;
  bool _isFrameProcessing = false;

  final List<dynamic> _pottedSeqP1 = [];
  final List<dynamic> _pottedSeqP2 = [];
  Map<String, dynamic>? _lastSeqStats;
  Map<String, dynamic>? _timelineStartStats;
  double _lastVideoSeconds = 0.0;
  int _lastAppliedTimelineIndex = -1;
  Duration? _lastVideoProcessDuration;

  File? _imageFile;
  Uint8List? _resultImageBytes;
  bool _isImageAnalyzing = false;
  final List<Offset> _imageTapPoints = [];
  final GlobalKey _imageAnalysisKey = GlobalKey();
  String? _shotMessage;
  String? _shotDifficulty;
  String? _shotTargetId;
  String? _shotPocket;

  final ImagePicker _picker = ImagePicker();

  @override
  void initState() {
    super.initState();
  }

  String _normalizeBaseUrl(String url) {
    final trimmed = url.trim();
    if (trimmed.isEmpty) return trimmed;
    return trimmed.endsWith('/')
        ? trimmed.substring(0, trimmed.length - 1)
        : trimmed;
  }

  /// Initialize the device camera for live tracking.
  Future<void> _initCamera() async {
    setState(() {
      _isInitializingCamera = true;
    });

    if (cameras.isEmpty) {
      try {
        cameras = await availableCameras();
      } catch (e) {
        print("Failed to fetch cameras dynamically: $e");
      }
    }

    if (cameras.isNotEmpty) {
      // 720p (medium) keeps FPS high enough for live tracking; 1080p causes too much lag.
      _cameraController = CameraController(
          cameras.first, ResolutionPreset.medium,
          enableAudio: false);
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

  /// Backend URL — uses the Android-emulator loopback on Android, localhost otherwise.
  String get serverUrl {
    final override = _normalizeBaseUrl(_serverUrlOverride);
    if (override.isNotEmpty) {
      return override;
    }
    if (Platform.isAndroid) {
      return "http://10.0.2.2:5000";
    }
    return "http://127.0.0.1:5000";
  }

  String get imageApiUrl {
    final override = _normalizeBaseUrl(_imageApiUrlOverride);
    if (override.isNotEmpty) {
      return override;
    }
    if (Platform.isAndroid) {
      return "http://10.0.2.2:5001";
    }
    return "http://127.0.0.1:5001";
  }

  @override
  void dispose() {
    _videoController?.dispose();
    _chewieController?.dispose();
    _cameraController?.dispose();
    _liveTimer?.cancel();
    super.dispose();
  }

  /// Pick a video, fetch frame 0 for pocket calibration, then submit for processing.
  Future<void> _pickVideo(ImageSource source) async {
    final XFile? pickedFile = await _picker.pickVideo(source: source);
    if (pickedFile == null) return;
    final File file = File(pickedFile.path);

    Uint8List? frame0Bytes;
    Size? frameSize;
    try {
      var req = http.MultipartRequest(
          'POST', Uri.parse('$serverUrl/frame_detections'));
      req.files.add(await http.MultipartFile.fromPath('file', file.path));
      final resp = await req.send();
      if (resp.statusCode == 200) {
        final body = jsonDecode(await resp.stream.bytesToString());
        if (body['image_base64'] != null) {
          frame0Bytes = base64Decode(body['image_base64'] as String);
          frameSize = Size(
            (body['width'] as num).toDouble(),
            (body['height'] as num).toDouble(),
          );
        }
      }
    } catch (e) {
      print('Frame 0 fetch error: $e');
    }

    if (!mounted) return;
    final List<Offset>? tappedPoints = await _showPocketCalibrationDialog(
      frame0Bytes: frame0Bytes,
      frameSize: frameSize,
    );
    if (tappedPoints == null) return;

    setState(() {
      _pocketPoints = tappedPoints;
    });

    await _initializePlayer(file: file, isMuted: true);
    setState(() {
      _videoFile = file;
      _processedVideoFile = null;
      _isLoading = true;
      _lastVideoProcessDuration = null;
    });
    _processVideo(file);
  }

  /// Shows a full-screen dialog where the user taps 6 pocket corners on Frame 0.
  /// Returns the list of 6 [Offset] points in the video's pixel coordinate space,
  /// or null if the user cancelled.
  Future<List<Offset>?> _showPocketCalibrationDialog({
    required Uint8List? frame0Bytes,
    required Size? frameSize,
  }) async {
    return showDialog<List<Offset>>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => _PocketCalibrationDialog(
        frame0Bytes: frame0Bytes,
        frameSize: frameSize,
      ),
    );
  }

  /// Upload a video to the server, poll for progress, then download the result.
  Future<void> _processVideo(File videoFile) async {
    final Stopwatch processingStopwatch = Stopwatch()..start();
    String lastRequestUrl = '$serverUrl/start_video_predict';

    setState(() {
      _processedVideoFile = null;
      _progressValue = 0.0;
      _initialPoints = null;
    });

    try {
      print("Starting video processing job...");
      var request = http.MultipartRequest(
        'POST', Uri.parse(lastRequestUrl));
      request.files
          .add(await http.MultipartFile.fromPath('file', videoFile.path));
      if (_pocketPoints.length == 6) {
        final pocketJson = jsonEncode(
            _pocketPoints.map((p) => {'x': p.dx, 'y': p.dy}).toList());
        request.fields['pocket_points'] = pocketJson;
      }
      var response = await request.send();

      if (response.statusCode == 200) {
        String respStr = await response.stream.bytesToString();
        var jobData = jsonDecode(respStr);
        String jobId = jobData['job_id'];

        if (mounted) {
          setState(() {
            _currentJobId = jobId;
          });
        }
        print("Job started: $jobId");

        bool completed = false;
        Map<String, dynamic>? latestStatusData;
        while (!completed) {
          if (!mounted || _currentJobId == null) return;

          await Future.delayed(const Duration(seconds: 1));

            lastRequestUrl = '$serverUrl/job_status/$jobId';
          var statusResp =
              await http.get(Uri.parse(lastRequestUrl));
          if (statusResp.statusCode == 200) {
            var statusData = jsonDecode(statusResp.body);
            latestStatusData = (statusData is Map<String, dynamic>)
                ? statusData
                : Map<String, dynamic>.from(statusData as Map);
            String status = statusData['status'];

            if (status == 'cancelled') return;

            if (statusData['progress'] != null) {
              if (mounted) {
                setState(() {
                  _progressValue = (statusData['progress'] as int) / 100.0;
                  if (statusData['initial_points'] != null) {
                    _initialPoints = statusData['initial_points'] as int;
                  }
                });
              }
            }

            if (status == 'completed') {
              completed = true;
            } else if (status == 'error') {
              throw Exception("Server Error: ${statusData['error']}");
            }
          } else {
            print("Error checking status: ${statusResp.statusCode}");
          }
        }

        Map<String, dynamic>? counts;
        for (int attempt = 0; attempt < 3 && counts == null; attempt++) {
          try {
            lastRequestUrl = '$serverUrl/job_stats/$jobId';
            final statsResp = await http.get(Uri.parse(lastRequestUrl));
            if (statsResp.statusCode == 200 && statsResp.body.isNotEmpty) {
              counts = jsonDecode(statsResp.body) as Map<String, dynamic>;
              break;
            }
          } catch (e) {
            print(
                "Warning: failed to fetch job stats (attempt ${attempt + 1}/3): $e");
          }

          if (counts == null) {
            await Future.delayed(const Duration(milliseconds: 400));
          }
        }

        if (counts == null) {
          try {
            lastRequestUrl = '$serverUrl/job_status/$jobId?include_timeline=1';
            final statusResp = await http.get(Uri.parse(lastRequestUrl));
            if (statusResp.statusCode == 200 && statusResp.body.isNotEmpty) {
              final statusData = jsonDecode(statusResp.body);
              if (statusData is Map) {
                final statusMap = Map<String, dynamic>.from(statusData);
                if (statusMap['summary'] is Map) {
                  counts = {
                    'summary': Map<String, dynamic>.from(statusMap['summary'] as Map),
                    'timeline': statusMap['timeline'] is List
                        ? List<dynamic>.from(statusMap['timeline'] as List)
                        : <dynamic>[],
                  };
                }
              }
            }
          } catch (e) {
            print('Warning: failed to fetch timeline fallback from job_status: $e');
          }
        }

        // Fallback: use summary/timeline included in /job_status when /job_stats is unavailable.
        if (counts == null &&
            latestStatusData != null &&
            latestStatusData['summary'] is Map) {
          counts = {
            'summary':
                Map<String, dynamic>.from(latestStatusData['summary'] as Map),
            'timeline': latestStatusData['timeline'] is List
                ? List<dynamic>.from(latestStatusData['timeline'] as List)
                : <dynamic>[],
          };
        }

        // Always refresh timeline via chunked endpoint for robust scrub-sync on large jobs.
        bool timelineLoaded = false;
        for (int attempt = 0; attempt < 3 && !timelineLoaded; attempt++) {
          try {
            final paged = await _fetchTimelineInChunks(jobId);
            if (paged != null && paged['timeline'] is List) {
              final pagedTimeline = List<dynamic>.from(paged['timeline'] as List);
              if (pagedTimeline.isNotEmpty) {
                final pagedSummary = paged['summary'] is Map
                    ? Map<String, dynamic>.from(paged['summary'] as Map)
                    : <String, dynamic>{};

                if (counts == null) {
                  counts = {
                    'summary': pagedSummary,
                    'timeline': pagedTimeline,
                  };
                } else {
                  counts['timeline'] = pagedTimeline;
                  if ((counts['summary'] is! Map ||
                          (counts['summary'] as Map).isEmpty) &&
                      pagedSummary.isNotEmpty) {
                    counts['summary'] = pagedSummary;
                  }
                }
                timelineLoaded = true;
                print('Timeline loaded via chunked endpoint: ${pagedTimeline.length} points');
                break;
              }
            }
          } catch (e) {
            print('Warning: failed to fetch chunked timeline (attempt ${attempt + 1}/3): $e');
          }

          if (!timelineLoaded) {
            await Future.delayed(const Duration(milliseconds: 250));
          }
        }

        if (!timelineLoaded) {
          try {
            lastRequestUrl = '$serverUrl/job_timeline/$jobId';
            final timelineResp = await http.get(Uri.parse(lastRequestUrl));
            if (timelineResp.statusCode == 200 && timelineResp.body.isNotEmpty) {
              final timelineData = jsonDecode(timelineResp.body);
              if (timelineData is Map) {
                final tMap = Map<String, dynamic>.from(timelineData);
                final summaryFromTimeline =
                    tMap['summary'] is Map ? Map<String, dynamic>.from(tMap['summary'] as Map) : null;
                final timelineFromTimeline =
                    tMap['timeline'] is List ? List<dynamic>.from(tMap['timeline'] as List) : <dynamic>[];

                if (counts == null) {
                  counts = {
                    'summary': summaryFromTimeline ?? <String, dynamic>{},
                    'timeline': timelineFromTimeline,
                  };
                } else {
                  counts['timeline'] = timelineFromTimeline;
                  if ((counts['summary'] is! Map ||
                          (counts['summary'] as Map).isEmpty) &&
                      summaryFromTimeline != null) {
                    counts['summary'] = summaryFromTimeline;
                  }
                }
                print('Timeline loaded via single endpoint: ${timelineFromTimeline.length} points');
              }
            }
          } catch (e) {
            print('Warning: failed to fetch dedicated job timeline: $e');
          }
        }

        print("Job completed. Loading network stream...");
        final resultUrl = '$serverUrl/job_result/$jobId';
        lastRequestUrl = resultUrl;

        try {
          await _initializePlayer(networkUrl: resultUrl);
        } catch (networkInitError) {
          print('Network video init failed, falling back to local file: $networkInitError');

          lastRequestUrl = resultUrl;
          final appDir = await getApplicationDocumentsDirectory();
          final processedDir = Directory('${appDir.path}${Platform.pathSeparator}processed_videos');
          if (!await processedDir.exists()) {
            await processedDir.create(recursive: true);
          }
          final localResultPath =
              '${processedDir.path}${Platform.pathSeparator}snooker_result_${DateTime.now().millisecondsSinceEpoch}.mp4';
          final localResultFile = File(localResultPath);

          final client = http.Client();
          try {
            final req = http.Request('GET', Uri.parse(resultUrl));
            final resp = await client.send(req);
            if (resp.statusCode != 200) {
              throw Exception(
                'Failed to load processed video. '
                'Network init error: $networkInitError; '
                'Download status: ${resp.statusCode}',
              );
            }

            final sink = localResultFile.openWrite();
            await resp.stream.pipe(sink);
            await sink.close();
          } finally {
            client.close();
          }

          if (!await localResultFile.exists() || await localResultFile.length() == 0) {
            throw Exception(
              'Failed to load processed video. '
              'Network init error: $networkInitError; '
              'Downloaded file is empty.',
            );
          }

          await _initializePlayer(file: localResultFile);
        }

        if (mounted) {
          setState(() {
            _processedVideoFile = File('');
            _isLoading = false;
            _lastVideoProcessDuration = processingStopwatch.elapsed;
            _lastAppliedTimelineIndex = -1;
            _timelineStartStats = null;

            if (counts != null && counts.containsKey('summary')) {
              final summary = Map<String, dynamic>.from(counts['summary']);
              _counts = _ensureScoreKeys(summary);

              if (counts.containsKey('timeline')) {
                final rawTimeline = List<dynamic>.from(counts['timeline']);
                _statsTimeline = rawTimeline
                    .whereType<Map>()
                    .map((e) => Map<String, dynamic>.from(e))
                    .toList()
                  ..sort((a, b) {
                    final ta = (a['timestamp'] is num)
                        ? (a['timestamp'] as num).toDouble()
                        : 0.0;
                    final tb = (b['timestamp'] is num)
                        ? (b['timestamp'] as num).toDouble()
                        : 0.0;
                    return ta.compareTo(tb);
                  });

                if (_statsTimeline!.isNotEmpty) {
                  final first = _statsTimeline!.first;
                  final start = <String, dynamic>{
                    'player1_score': 0,
                    'player2_score': 0,
                    'current_player': 1,
                  };

                  // Keep table-state style fields if available from timeline.
                  if (first['potential_score'] != null) {
                    start['potential_score'] = first['potential_score'];
                  }

                  // Initialize potted counters to zero for all keys present in timeline.
                  first.forEach((k, v) {
                    if (k.startsWith('p1_potted_') ||
                        k.startsWith('p2_potted_')) {
                      start[k] = 0;
                    }
                  });

                  _timelineStartStats = start;
                }

                if (_statsTimeline!.isNotEmpty) {
                  // Show populated/final history immediately after processing.
                  _rebuildSequencesToTime(double.infinity);
                } else {
                  _rebuildSequencesFromSummary(summary);
                }
              } else {
                _rebuildSequencesFromSummary(summary);
              }
            } else {
              _counts = counts;
            }
          });
        }
      } else {
        String errorBody = await response.stream.bytesToString();
        setState(() {
          _isLoading = false;
        });
        _showErrorDialog(
            'Start Job Failed: ${response.statusCode}\n$errorBody',
            endpoint: lastRequestUrl);
      }
    } catch (e) {
      print('Connection Error: $e');
      setState(() {
        _isLoading = false;
      });
      _showErrorDialog(e.toString(), endpoint: lastRequestUrl);
    }
  }

  /// Cancel the current video-processing job on the server.
  Future<void> _cancelProcessing() async {
    String? id = _currentJobId;
    print("User cancelling job $id...");

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
      } catch (e) {
        print("Error notifying server of cancellation: $e");
      }
    }
  }

  /// Dispose video player and chewie controllers and reset sequence state.
  void _disposeVideoControllers() {
    _videoController?.removeListener(_onVideoPositionChanged);
    _videoController?.dispose();
    _chewieController?.dispose();
    _videoController = null;
    _chewieController = null;
    _statsTimeline = null;
    _pottedSeqP1.clear();
    _pottedSeqP2.clear();
    _lastSeqStats = null;
    _timelineStartStats = null;
    _lastVideoSeconds = 0.0;
    _lastAppliedTimelineIndex = -1;
  }

  /// Safely coerce a dynamic value to int.
  int _asInt(dynamic v) {
    if (v is int) return v;
    if (v is double) return v.toInt();
    if (v is String) return int.tryParse(v) ?? 0;
    return 0;
  }

  /// Map a ball label string to its display colour.
  Color _ballToColor(String key) {
    switch (key) {
      case 'red-ball':
        return Colors.red;
      case 'yellow-ball':
        return Colors.amber;
      case 'green-ball':
        return Colors.green;
      case 'brown-ball':
        return const Color.fromARGB(255, 95, 63, 52);
      case 'blue-ball':
        return Colors.blue;
      case 'pink-ball':
        return const Color(0xFFFFB6D9);
      case 'black-ball':
        return Colors.black;
      default:
        return Colors.grey;
    }
  }

  /// Human-readable name for a ball label key.
  String _prettyBall(String key) {
    switch (key) {
      case 'red-ball':
        return 'Red';
      case 'yellow-ball':
        return 'Yellow';
      case 'green-ball':
        return 'Green';
      case 'brown-ball':
        return 'Brown';
      case 'blue-ball':
        return 'Blue';
      case 'pink-ball':
        return 'Pink';
      case 'black-ball':
        return 'Black';
      default:
        return key;
    }
  }

  /// Append a potting or foul event to the given player's sequence (capped at 40).
  void _appendForPlayer(int player, dynamic event) {
    if (player == 1) {
      _pottedSeqP1.add(event);
      if (_pottedSeqP1.length > 40) _pottedSeqP1.removeAt(0);
    } else {
      _pottedSeqP2.add(event);
      if (_pottedSeqP2.length > 40) _pottedSeqP2.removeAt(0);
    }
  }

  /// Convert display ball name from history text to canonical label key.
  String _ballNameToKey(String name) {
    switch (name.trim().toLowerCase()) {
      case 'red':
        return 'red-ball';
      case 'yellow':
        return 'yellow-ball';
      case 'green':
        return 'green-ball';
      case 'brown':
        return 'brown-ball';
      case 'blue':
        return 'blue-ball';
      case 'pink':
        return 'pink-ball';
      case 'black':
        return 'black-ball';
      default:
        return '';
    }
  }

  /// Build potted sequences from chronological history when available.
  /// Falls back to summary counters when history is unavailable.
  void _rebuildSequencesFromSummary(Map<String, dynamic> stats) {
    _pottedSeqP1.clear();
    _pottedSeqP2.clear();
    _lastSeqStats = null;

    const balls = [
      'red-ball',
      'yellow-ball',
      'green-ball',
      'brown-ball',
      'blue-ball',
      'pink-ball',
      'black-ball',
    ];

    final historyRaw = stats['history'];
    if (historyRaw is List && historyRaw.isNotEmpty) {
      for (final entryRaw in historyRaw) {
        if (entryRaw is! Map) continue;
        final entry = Map<String, dynamic>.from(entryRaw);
        final type = (entry['type'] ?? '').toString();
        final msg = (entry['msg'] ?? '').toString();

        if (type == 'pot') {
          final playerMatch = RegExp(r'\[P(\d+)\]').firstMatch(msg);
          final ballMatch = RegExp(r'Potted\s+([A-Za-z]+)').firstMatch(msg);
          if (playerMatch == null || ballMatch == null) continue;

          final player = _asInt(playerMatch.group(1));
          final ballKey = _ballNameToKey(ballMatch.group(1) ?? '');
          if (player != 1 && player != 2) continue;
          if (!balls.contains(ballKey)) continue;

          int repeat = 1;
          if (ballKey == 'red-ball') {
            final pointsMatch = RegExp(r'\(\+(\d+)\)').firstMatch(msg);
            repeat = pointsMatch == null ? 1 : _asInt(pointsMatch.group(1));
            if (repeat <= 0) repeat = 1;
          }

          for (int i = 0; i < repeat; i++) {
            _appendForPlayer(player, ballKey);
          }
        } else if (type == 'foul') {
          final offenderMatch = RegExp(r'\[FOUL\]\s*P(\d+)').firstMatch(msg);
          final penaltyMatch =
              RegExp(r'\+\s*(\d+)\s*to\s*P\d+').firstMatch(msg);
          if (offenderMatch == null || penaltyMatch == null) continue;

          final offender = _asInt(offenderMatch.group(1));
          final penalty = _asInt(penaltyMatch.group(1));
          if ((offender == 1 || offender == 2) && penalty > 0) {
            _appendForPlayer(offender, 'foul:$penalty');
          }
        }
      }

      _lastSeqStats = Map<String, dynamic>.from(_ensureScoreKeys(stats));
      return;
    }

    for (final p in [1, 2]) {
      for (final b in balls) {
        final n = _asInt(stats['p${p}_potted_$b']);
        for (int i = 0; i < n; i++) {
          _appendForPlayer(p, b);
        }
      }
    }

    _lastSeqStats = Map<String, dynamic>.from(_ensureScoreKeys(stats));
  }

  /// Whether this sequence event represents a foul.
  bool _isFoulEvent(dynamic eventRaw) {
    return eventRaw is String && eventRaw.startsWith('foul:');
  }

  /// Extract the numeric penalty from a foul event.
  int _foulPenalty(dynamic eventRaw) {
    if (eventRaw is String && eventRaw.startsWith('foul:')) {
      return _asInt(eventRaw.substring(5));
    }
    if (eventRaw is Map && eventRaw['type'] == 'foul') {
      return _asInt(eventRaw['penalty']);
    }
    return 0;
  }

  /// Extract the ball label key from a heterogeneous sequence event.
  String _eventBallKey(dynamic eventRaw) {
    if (eventRaw is String) return eventRaw;
    if (eventRaw is Map) return (eventRaw['ball'] ?? '').toString();
    return '';
  }

  /// Diff the latest stats snapshot against the previous one and append new events.
  void _updatePottedSequencesFromStats(Map<String, dynamic> rawStats) {
    final stats = _ensureScoreKeys(rawStats);
    final prev = _lastSeqStats;
    final currentPlayer = _asInt(stats['current_player']);
    final p1Score = _asInt(stats['player1_score']);
    final p2Score = _asInt(stats['player2_score']);

    // On turn switch, any opponent score increase ≥ 4 is treated as a foul penalty.
    if (prev != null) {
      final prevPlayer = _asInt(prev['current_player']);
      final prevP1Score = _asInt(prev['player1_score']);
      final prevP2Score = _asInt(prev['player2_score']);
      final p1Delta = p1Score - prevP1Score;
      final p2Delta = p2Score - prevP2Score;

      if (prevPlayer == 1 && currentPlayer == 2 && p2Delta >= 4) {
        _appendForPlayer(1, 'foul:$p2Delta');
      } else if (prevPlayer == 2 && currentPlayer == 1 && p1Delta >= 4) {
        _appendForPlayer(2, 'foul:$p1Delta');
      }
    }

    const balls = [
      'red-ball',
      'yellow-ball',
      'green-ball',
      'brown-ball',
      'blue-ball',
      'pink-ball',
      'black-ball',
    ];

    final remaining = {
      1: <String, int>{for (final b in balls) b: 0},
      2: <String, int>{for (final b in balls) b: 0},
    };

    for (final p in [1, 2]) {
      for (final b in balls) {
        final curr = _asInt(stats['p${p}_potted_$b']);
        final old = prev == null ? 0 : _asInt(prev['p${p}_potted_$b']);
        final d = curr - old;
        if (d > 0) remaining[p]![b] = d;
      }
    }

    final pottedBalls = (stats['potted_balls'] is List)
        ? List<dynamic>.from(stats['potted_balls'])
        : const <dynamic>[];

    for (final item in pottedBalls) {
      final b = item.toString();
      if (!balls.contains(b)) continue;

      int assigned = 0;
      if ((remaining[currentPlayer]?[b] ?? 0) > 0) {
        assigned = currentPlayer;
      } else if ((remaining[1]?[b] ?? 0) > 0) {
        assigned = 1;
      } else if ((remaining[2]?[b] ?? 0) > 0) {
        assigned = 2;
      }

      if (assigned != 0) {
        _appendForPlayer(assigned, b);
        remaining[assigned]![b] = (remaining[assigned]![b] ?? 1) - 1;
      }
    }

    for (final p in [1, 2]) {
      for (final b in balls) {
        final left = remaining[p]![b] ?? 0;
        for (int i = 0; i < left; i++) {
          _appendForPlayer(p, b);
        }
      }
    }

    _lastSeqStats = Map<String, dynamic>.from(stats);
  }

  /// Replay the full potted-sequence state up to [currentSeconds] for seek support.
  void _rebuildSequencesToTime(double currentSeconds) {
    _pottedSeqP1.clear();
    _pottedSeqP2.clear();
    _lastSeqStats = null;
    if (_statsTimeline == null || _statsTimeline!.isEmpty) return;

    for (final stat in _statsTimeline!) {
      double t = 0.0;
      if (stat.containsKey('timestamp')) {
        final v = stat['timestamp'];
        if (v is int) t = v.toDouble();
        if (v is double) t = v;
      }
      if (t <= currentSeconds) {
        _updatePottedSequencesFromStats(Map<String, dynamic>.from(stat));
      } else {
        break;
      }
    }
  }

  /// Listener called on every video tick; syncs the scoreboard to the current position.
  void _onVideoPositionChanged() {
    if (_videoController == null || !_videoController!.value.isInitialized) {
      return;
    }

    final double currentSeconds =
        _videoController!.value.position.inMilliseconds / 1000.0;

    if (_statsTimeline == null || _statsTimeline!.isEmpty) return;
    final isReplayOrSeekBack = currentSeconds + 0.05 < _lastVideoSeconds;

    Map<String, dynamic>? currentStats;
    int currentIndex = -1;

    // Walk the sorted timeline to find the latest entry ≤ currentSeconds.
    for (int i = 0; i < _statsTimeline!.length; i++) {
      final stat = _statsTimeline![i];
      double t = 0.0;
      if (stat.containsKey('timestamp')) {
        var val = stat['timestamp'];
        if (val is int) {
          t = val.toDouble();
        } else if (val is double) t = val;
      }

      if (t <= currentSeconds) {
        currentStats = stat;
        currentIndex = i;
      } else {
        break;
      }
    }

    if (currentStats != null) {
      if (isReplayOrSeekBack || currentIndex != _lastAppliedTimelineIndex) {
        final snapshot = Map<String, dynamic>.from(currentStats as Map);
        setState(() {
          // Rebuild in true chronological order up to the active playback time.
          _rebuildSequencesToTime(currentSeconds);
          final merged = _ensureScoreKeys(snapshot);
          // Timeline entries are intentionally lightweight and often omit
          // history. Preserve the latest known history so Action Log remains
          // visible after processing and during seek.
          final prevHistory = _counts == null ? null : _counts!['history'];
          if (!merged.containsKey('history') && prevHistory != null) {
            merged['history'] = prevHistory;
          }
          _counts = merged;
          _lastAppliedTimelineIndex = currentIndex;
        });
      }
    } else if (isReplayOrSeekBack && _lastAppliedTimelineIndex != -1) {
      // Rewound before the first sampled timeline entry: reset to an initial scoreboard.
      setState(() {
        _rebuildSequencesToTime(0.0);
        final reset = _ensureScoreKeys(
            Map<String, dynamic>.from(_timelineStartStats ?? const {}));
        final prevHistory = _counts == null ? null : _counts!['history'];
        if (!reset.containsKey('history') && prevHistory != null) {
          reset['history'] = prevHistory;
        }
        _counts = reset;
        _lastAppliedTimelineIndex = -1;
      });
    }

    _lastVideoSeconds = currentSeconds;
  }

  /// Ensure player-score keys exist, filling defaults for missing entries.
  Map<String, dynamic> _ensureScoreKeys(Map<String, dynamic> input) {
    final result = Map<String, dynamic>.from(input);
    result.putIfAbsent('player1_score', () => 0);
    result.putIfAbsent('player2_score', () => 0);
    result.putIfAbsent('current_player', () => 1);
    return result;
  }

  /// Format a [Duration] as HH:MM:SS (or MM:SS when < 1 hour).
  String _formatDuration(Duration duration) {
    String twoDigits(int n) => n.toString().padLeft(2, '0');
    final hours = duration.inHours;
    final minutes = duration.inMinutes.remainder(60);
    final seconds = duration.inSeconds.remainder(60);

    if (hours > 0) {
      return '${twoDigits(hours)}:${twoDigits(minutes)}:${twoDigits(seconds)}';
    }
    return '${twoDigits(minutes)}:${twoDigits(seconds)}';
  }

  /// Initialise the Chewie video player for the given file or URL.
  Future<void> _initializePlayer(
      {File? file, String? networkUrl, bool isMuted = false}) async {
    _disposeVideoControllers();

    if (networkUrl != null) {
      _videoController =
          VideoPlayerController.networkUrl(Uri.parse(networkUrl));
    } else if (file != null) {
      _videoController = VideoPlayerController.file(file);
    } else {
      return;
    }

    await _videoController!.initialize();

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
    if (mounted) setState(() {});
  }

  /// Show an error dialog explaining connectivity issues.
  void _showErrorDialog(String message, {String? endpoint}) {
    if (!mounted) return;
    final failingUrl = endpoint ?? serverUrl;
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Error'),
        content: Text("Failed to connect to server.\n"
            "URL: $failingUrl\n"
            "Base URL: $serverUrl\n\n"
            "Ensure:\n"
            "1. Server.py is running on PC.\n"
            "2. For Android emulator use 10.0.2.2; for real phone use your PC LAN IP.\n"
            "3. PC and phone are on the same Wi-Fi (for real device).\n"
            "4. Or override using --dart-define=SERVER_URL=http://<PC-IP>:5000.\n\n"
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

  Future<Map<String, dynamic>?> _fetchTimelineInChunks(String jobId) async {
    const int chunkSize = 600;
    int offset = 0;
    final List<dynamic> allTimeline = [];
    Map<String, dynamic>? firstSummary;

    for (int guard = 0; guard < 200; guard++) {
      final uri = Uri.parse('$serverUrl/job_timeline_chunk/$jobId')
          .replace(queryParameters: {
        'offset': offset.toString(),
        'limit': chunkSize.toString(),
      });

      final resp = await http.get(uri);
      if (resp.statusCode != 200 || resp.body.isEmpty) {
        return null;
      }

      final body = jsonDecode(resp.body);
      if (body is! Map) return null;
      final map = Map<String, dynamic>.from(body);

      if (offset == 0 && map['summary'] is Map) {
        firstSummary = Map<String, dynamic>.from(map['summary'] as Map);
      }

      final chunk = map['timeline'] is List
          ? List<dynamic>.from(map['timeline'] as List)
          : <dynamic>[];
      allTimeline.addAll(chunk);

      final hasMore = map['has_more'] == true;
      final total =
          map['total'] is num ? (map['total'] as num).toInt() : allTimeline.length;
      if (!hasMore || allTimeline.length >= total) {
        break;
      }

      offset = allTimeline.length;
    }

    return {
      'summary': firstSummary ?? <String, dynamic>{},
      'timeline': allTimeline,
    };
  }

  Future<void> _pickImageForAnalysis(ImageSource source) async {
    final pickedFile = await _picker.pickImage(source: source);
    if (pickedFile == null) return;
    setState(() {
      _imageFile = File(pickedFile.path);
      _resultImageBytes = null;
      _imageTapPoints.clear();
      _shotMessage = null;
      _shotDifficulty = null;
      _shotTargetId = null;
      _shotPocket = null;
    });
  }

  void _onImageAnalysisTap(TapUpDetails details) {
    if (_imageFile == null || _resultImageBytes != null || _isImageAnalyzing) {
      return;
    }
    if (_imageTapPoints.length >= 4) return;

    final box = _imageAnalysisKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return;
    final local = box.globalToLocal(details.globalPosition);

    setState(() {
      _imageTapPoints.add(local);
    });

    if (_imageTapPoints.length == 4) {
      _askTargetTypeDialog();
    }
  }

  void _askTargetTypeDialog() {
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => AlertDialog(
        title: const Text('Select Target'),
        content: const Text('Which ball type are you aiming for next?'),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              _sendImageToServer('red');
            },
            child: const Text('Red Balls', style: TextStyle(color: Colors.red)),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              _sendImageToServer('color');
            },
            child:
                const Text('Color Balls', style: TextStyle(color: Colors.blue)),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              setState(() => _imageTapPoints.clear());
            },
            style: TextButton.styleFrom(foregroundColor: Colors.grey),
            child: const Text('Retap Corners'),
          ),
        ],
      ),
    );
  }

  void _showImageErrorDialog(String message) {
    if (!mounted) return;
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('System Alert', style: TextStyle(color: Colors.red)),
        content: Text(message),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              setState(() => _imageTapPoints.clear());
            },
            child: const Text('OK'),
          )
        ],
      ),
    );
  }

  Future<void> _sendImageToServer(String targetType) async {
    if (_imageFile == null) return;
    final box = _imageAnalysisKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null || box.size.width <= 0 || box.size.height <= 0) return;

    final width = box.size.width;
    final height = box.size.height;

    setState(() {
      _isImageAnalyzing = true;
      _shotMessage = null;
    });

    try {
      final req = http.MultipartRequest(
        'POST',
        Uri.parse('$imageApiUrl/analyze_shot'),
      );
      req.files.add(await http.MultipartFile.fromPath('image', _imageFile!.path));

      final corners = _imageTapPoints.map((p) => [p.dx / width, p.dy / height]).toList();
      req.fields['corners'] = jsonEncode(corners);
      req.fields['target_type'] = targetType;

      final resp = await req.send().timeout(const Duration(seconds: 120));
      final bodyText = await resp.stream.bytesToString();
      final body = jsonDecode(bodyText);

      if (resp.statusCode == 200 && body['status'] == 'success') {
        setState(() {
          _resultImageBytes = base64Decode(body['image_base64'] as String);
          _shotMessage = (body['message'] ?? '').toString();
          _shotDifficulty = (body['difficulty_score'] ?? 'N/A').toString();
          _shotTargetId = (body['target_id'] ?? 'N/A').toString();
          _shotPocket = (body['pocket'] ?? 'N/A').toString();
          _imageTapPoints.clear();
        });
      } else {
        _showImageErrorDialog(
            (body['message'] ?? 'Shot analysis failed').toString());
      }
    } catch (e) {
      _showImageErrorDialog(
          'Connection failed. Check if Python is running and the image is not too large.');
    } finally {
      if (mounted) {
        setState(() {
          _isImageAnalyzing = false;
        });
      }
    }
  }

  Widget _buildImageDashboard() {
    if (_shotMessage == null) return const SizedBox.shrink();
    final bool isSafety = _shotDifficulty == 'N/A';

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      elevation: 4,
      color: isSafety ? Colors.orange[50] : Colors.green[50],
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  isSafety ? Icons.warning_amber_rounded : Icons.check_circle_outline,
                  color: isSafety ? Colors.orange : Colors.green,
                ),
                const SizedBox(width: 8),
                Text(
                  isSafety ? 'Safety Shot Required' : 'Shot Route Confirmed',
                  style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                ),
              ],
            ),
            const Divider(),
            Text(_shotMessage ?? '', style: const TextStyle(fontSize: 14)),
            if (!isSafety) ...[
              Text('🎯 Target Ball: Number $_shotTargetId',
                  style: const TextStyle(fontSize: 16)),
              const SizedBox(height: 4),
              Text('⛳ Target Pocket: $_shotPocket',
                  style: const TextStyle(fontSize: 16)),
              const SizedBox(height: 4),
              Text('🔥 Difficulty Score: $_shotDifficulty (Lower is easier)',
                  style: const TextStyle(fontSize: 16)),
            ] else ...[
              const Text(
                'All attacking routes are blocked. The system has calculated the closest ball to play a safe defensive shot.',
                style: TextStyle(fontSize: 14),
              ),
            ]
          ],
        ),
      ),
    );
  }

  Widget _buildImageBody() {
    if (_isImageAnalyzing) {
      return const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 16),
            Text('AI is calculating shot routes...'),
          ],
        ),
      );
    }

    Widget imageContent;
    if (_resultImageBytes != null) {
      imageContent = Image.memory(_resultImageBytes!);
    } else if (_imageFile != null) {
      imageContent = Image.file(_imageFile!);
    } else {
      imageContent = Container(
        height: 400,
        width: double.infinity,
        color: Colors.grey[300],
        child: const Center(
          child: Text(
            'Please select an image\nto begin tactical analysis.',
            textAlign: TextAlign.center,
          ),
        ),
      );
    }

    return Center(
      child: SingleChildScrollView(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(8.0),
              child: GestureDetector(
                onTapUp: _onImageAnalysisTap,
                child: Stack(
                  key: _imageAnalysisKey,
                  children: [
                    imageContent,
                    ..._imageTapPoints.map((point) => Positioned(
                          left: point.dx - 5,
                          top: point.dy - 5,
                          child: const Icon(Icons.circle,
                              color: Colors.greenAccent, size: 10),
                        )),
                  ],
                ),
              ),
            ),
            _buildImageDashboard(),
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }

  Widget _buildImageFloatingActions() {
    return Column(
      mainAxisAlignment: MainAxisAlignment.end,
      children: [
        FloatingActionButton.extended(
          heroTag: 'img_btn_camera',
          onPressed: () => _pickImageForAnalysis(ImageSource.camera),
          tooltip: 'Take a Photo',
          icon: const Icon(Icons.camera_alt),
          label: const Text('Camera'),
        ),
        const SizedBox(height: 16),
        FloatingActionButton.extended(
          heroTag: 'img_btn_upload',
          onPressed: () => _pickImageForAnalysis(ImageSource.gallery),
          tooltip: 'Upload Image',
          icon: const Icon(Icons.photo_library),
          label: const Text('Upload'),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(
          _selectedIndex == 2 ? 'Snooker AI Tactical System' : widget.title,
        ),
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
          BottomNavigationBarItem(icon: Icon(Icons.camera), label: 'Live Tab'),
          BottomNavigationBarItem(
              icon: Icon(Icons.image_search), label: 'Image'),
        ],
      ),
      body: _selectedIndex == 0
          ? _buildVideoBody()
          : (_selectedIndex == 1 ? _buildLiveBody() : _buildImageBody()),
        floatingActionButton:
          _selectedIndex == 2 ? _buildImageFloatingActions() : null,
    );
  }

  /// Build the Video tab layout.
  Widget _buildVideoBody() {
    bool hasPlayer = _chewieController != null &&
        _chewieController!.videoPlayerController.value.isInitialized;

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
          const Icon(Icons.video_camera_back_outlined,
              size: 64, color: Colors.white24),
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
          Card(
            margin: const EdgeInsets.symmetric(horizontal: 10.0, vertical: 5.0),
            elevation: 6,
            shape:
                RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
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
                  if (_isLoading)
                    Container(
                      color: Colors.black54,
                      width: double.infinity,
                      height: double.infinity,
                      child: Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            const CircularProgressIndicator(
                                color: Colors.white),
                            const SizedBox(height: 20),
                            SizedBox(
                                width: 200,
                                child: LinearProgressIndicator(
                                    value: _progressValue,
                                    color: Colors.green,
                                    backgroundColor: Colors.white24)),
                            const SizedBox(height: 10),
                            Text(
                                "Processing... ${(_progressValue * 100).toInt()}%",
                                style: const TextStyle(
                                    color: Colors.white,
                                    fontWeight: FontWeight.bold)),
                            if (_initialPoints != null) ...[
                              const SizedBox(height: 10),
                              const Text("Detected Maximum Info:",
                                  style: TextStyle(
                                      color: Colors.yellow,
                                      fontWeight: FontWeight.bold)),
                              Text(
                                  "Maximum Initial Points on Table: $_initialPoints",
                                  style: const TextStyle(
                                      color: Colors.yellowAccent,
                                      fontSize: 16)),
                            ],
                            const Padding(
                              padding: EdgeInsets.all(8.0),
                              child: Text(
                                  "Calibrating table state & tracking...",
                                  style: TextStyle(
                                      color: Colors.white70, fontSize: 12)),
                            ),
                            const SizedBox(height: 15),
                            ElevatedButton(
                              onPressed: _cancelProcessing,
                              style: ElevatedButton.styleFrom(
                                  backgroundColor: Colors.red.withOpacity(0.8),
                                  foregroundColor: Colors.white),
                              child: const Text("Cancel"),
                            )
                          ]),
                    )
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          _buildStats(_counts ?? {}),
          const SizedBox(height: 20),
          if (hasPlayer && !_isLoading) ...[
            ElevatedButton.icon(
              onPressed: () => _pickVideo(ImageSource.gallery),
              icon: const Icon(Icons.upload_file),
              label: const Text("Upload New Video"),
              style: ElevatedButton.styleFrom(
                padding:
                    const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
                minimumSize: const Size(220, 45),
              ),
            ),
          ],
          if (_lastVideoProcessDuration != null) ...[
            const SizedBox(height: 14),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14.0),
              child: Container(
                width: double.infinity,
                padding:
                    const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                decoration: BoxDecoration(
                  color: Colors.blueGrey.shade50,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: Colors.blueGrey.shade100),
                ),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(Icons.timer_outlined, size: 18),
                    const SizedBox(width: 8),
                    Text(
                      'Selected video processing time: ${_formatDuration(_lastVideoProcessDuration!)}',
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                  ],
                ),
              ),
            ),
          ],
          const SizedBox(height: 20),
        ],
      ),
    );
  }

  /// Build the scoreboard card with player scores, potted history, and action log.
  Widget _buildStats(Map<String, dynamic> counts) {
    int p1 = counts['player1_score'] ?? 0;
    int p2 = counts['player2_score'] ?? 0;
    int currentPlayer = counts['current_player'] ?? 1;

    return Card(
      margin: const EdgeInsets.all(12),
      elevation: 4,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            const Text("Snooker Scoreboard",
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const Divider(),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _buildPlayerScore(1, p1, currentPlayer == 1),
                Container(width: 1, height: 50, color: Colors.grey),
                _buildPlayerScore(2, p2, currentPlayer == 2),
              ],
            ),
            const SizedBox(height: 15),
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: Colors.green.shade50,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.green.shade200),
              ),
              child: Column(
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text("Points Ahead:",
                          style: TextStyle(
                              fontWeight: FontWeight.bold, fontSize: 16)),
                      Text("${(p1 - p2).abs()}",
                          style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              fontSize: 20,
                              color: Colors.indigo)),
                    ],
                  ),
                  const SizedBox(height: 5),
                  if (counts['potential_score'] != null)
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        const Text("Points Remaining:",
                            style: TextStyle(
                                fontWeight: FontWeight.bold, fontSize: 16)),
                        Text("${counts['potential_score']}",
                            style: const TextStyle(
                                fontWeight: FontWeight.bold,
                                fontSize: 20,
                                color: Colors.blue)),
                      ],
                    ),
                ],
              ),
            ),
            const SizedBox(height: 15),
            const Text("Potted (History)",
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const Divider(),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Column(
                    children: [
                      Text(player1Name,
                          style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              decoration: TextDecoration.underline)),
                      const SizedBox(height: 5),
                      _buildPottedSequenceColumn(1),
                    ],
                  ),
                ),
                Container(width: 1, height: 136, color: Colors.grey.shade300),
                Expanded(
                  child: Column(
                    children: [
                      Text(player2Name,
                          style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              decoration: TextDecoration.underline)),
                      const SizedBox(height: 5),
                      _buildPottedSequenceColumn(2),
                    ],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 15),
            const Text("Action Log (History)",
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const Divider(),
            _buildHistoryLog(),
          ],
        ),
      ),
    );
  }

  /// Build a tappable player score column with active/inactive styling.
  Widget _buildPlayerScore(int id, int score, bool isActive) {
    String playerName = (id == 1) ? player1Name : player2Name;

    return GestureDetector(
      onTap: () => _editPlayerName(id),
      child: Column(
        children: [
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(playerName,
                  style: TextStyle(
                      fontSize: 16,
                      fontWeight:
                          isActive ? FontWeight.bold : FontWeight.normal,
                      color: isActive ? Colors.black : Colors.grey)),
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
                boxShadow: isActive
                    ? [const BoxShadow(color: Colors.green, blurRadius: 5)]
                    : []),
            child: Text("$score",
                style: TextStyle(
                    fontSize: 24,
                    fontWeight: FontWeight.bold,
                    color: isActive ? Colors.white : Colors.black54)),
          ),
          if (isActive)
            const Padding(
              padding: EdgeInsets.only(top: 4.0),
              child: Text("Playing",
                  style: TextStyle(
                      fontSize: 10,
                      color: Colors.green,
                      fontWeight: FontWeight.bold)),
            )
        ],
      ),
    );
  }

  /// Show a dialog to rename a player.
  void _editPlayerName(int id) {
    TextEditingController controller =
        TextEditingController(text: (id == 1) ? player1Name : player2Name);
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

  /// Build a fixed potted-history grid: 10 balls per row, up to 4 rows.
  Widget _buildPottedSequenceColumn(int player) {
    final seq = player == 1 ? _pottedSeqP1 : _pottedSeqP2;
    if (seq.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 8.0),
        child: Text('-', style: TextStyle(fontSize: 16, color: Colors.grey)),
      );
    }

    const int itemsPerRow = 8;
    const int maxRows = 4;
    const double rowSpacing = 6.0;
    const double spacing = 8.0;
    const double dotSize = 15.0;

    final List<dynamic> visibleSeq = seq.length > itemsPerRow * maxRows
        ? seq.sublist(seq.length - (itemsPerRow * maxRows))
        : List<dynamic>.from(seq);

    final int rows = ((visibleSeq.length + itemsPerRow - 1) ~/ itemsPerRow)
        .clamp(1, maxRows);

    return LayoutBuilder(
      builder: (context, constraints) {
        const double tableWidth =
            (itemsPerRow * dotSize) + ((itemsPerRow - 1) * spacing);
        final double gridHeight = (rows * dotSize) + ((rows - 1) * rowSpacing);

        return SizedBox(
          height: gridHeight,
          child: Align(
            alignment: Alignment.center,
            child: SizedBox(
              width: tableWidth,
              height: gridHeight,
              child: GridView.builder(
                itemCount: visibleSeq.length,
                physics: const NeverScrollableScrollPhysics(),
                padding: EdgeInsets.zero,
                gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                  crossAxisCount: itemsPerRow,
                  mainAxisSpacing: rowSpacing,
                  crossAxisSpacing: spacing,
                  childAspectRatio: 1,
                ),
                itemBuilder: (context, index) {
                  final eventRaw = visibleSeq[index];
                  return Tooltip(
                    message: _isFoulEvent(eventRaw)
                        ? 'Foul -${_foulPenalty(eventRaw)}'
                        : _prettyBall(_eventBallKey(eventRaw)),
                    child: _isFoulEvent(eventRaw)
                        ? Container(
                            width: dotSize,
                            height: dotSize,
                            alignment: Alignment.center,
                            decoration: BoxDecoration(
                              color: Colors.white,
                              shape: BoxShape.circle,
                              border:
                                  Border.all(color: Colors.black, width: 1.5),
                            ),
                            child: FittedBox(
                              fit: BoxFit.scaleDown,
                              child: Text(
                                '-${_foulPenalty(eventRaw)}',
                                style: const TextStyle(
                                  fontSize: 11,
                                  fontWeight: FontWeight.bold,
                                  color: Colors.black,
                                ),
                              ),
                            ),
                          )
                        : Container(
                            width: dotSize,
                            height: dotSize,
                            decoration: BoxDecoration(
                              color: _ballToColor(_eventBallKey(eventRaw)),
                              shape: BoxShape.circle,
                            ),
                          ),
                  );
                },
              ),
            ),
          ),
        );
      },
    );
  }

  /// Build the scrollable action-log list.
  Widget _buildHistoryLog() {
    if (_counts == null || _counts!['history'] == null) {
      return const Padding(
          padding: EdgeInsets.all(16.0),
          child: Text("No history available yet.",
              style:
                  TextStyle(color: Colors.grey, fontStyle: FontStyle.italic)));
    }

    final history = List<dynamic>.from(_counts!['history']).reversed.toList();
    if (history.isEmpty) {
      return const Padding(
          padding: EdgeInsets.all(16.0),
          child: Text("Waiting for events...",
              style:
                  TextStyle(color: Colors.grey, fontStyle: FontStyle.italic)));
    }

    return Container(
      height: 250,
      decoration: BoxDecoration(
        color: Colors.grey.shade50,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.grey.shade300),
      ),
      child: ListView.builder(
        itemCount: history.length,
        itemBuilder: (context, index) {
          final item = history[index] as Map<String, dynamic>;
          final type = item['type'] as String? ?? 'info';
          final msg = item['msg'] as String? ?? '';

          Color textColor = Colors.black87;
          IconData icon = Icons.info_outline;
          Color iconColor = Colors.blue;

          if (type == 'pot') {
            textColor = Colors.green.shade800;
            icon = Icons.sports_baseball;
            iconColor = Colors.green;
          } else if (type == 'foul') {
            textColor = Colors.red.shade800;
            icon = Icons.warning_amber_rounded;
            iconColor = Colors.red;
          } else if (type == 'info') {
            textColor = Colors.indigo.shade800;
            icon = Icons.swap_horiz;
            iconColor = Colors.indigo;
          }

          return Container(
            decoration: BoxDecoration(
              border: Border(bottom: BorderSide(color: Colors.grey.shade200)),
            ),
            child: ListTile(
              dense: true,
              visualDensity: VisualDensity.compact,
              leading: Icon(icon, color: iconColor, size: 20),
              title: Text(msg,
                  style: TextStyle(
                      color: textColor,
                      fontWeight: FontWeight.w600,
                      fontSize: 13)),
            ),
          );
        },
      ),
    );
  }

  /// Capture a calibration frame, open the pocket-tap dialog, then start live polling.
  Future<void> _startLiveTracking() async {
    try {
      if (_cameraController == null || !_cameraController!.value.isInitialized) {
        return;
      }

      setState(() {
        _isInitializingCamera = true;
      });

      XFile frame0File = await _cameraController!.takePicture();

      Uint8List? frame0Bytes;
      Size? frameSize;
      try {
        var req = http.MultipartRequest(
            'POST', Uri.parse('$serverUrl/frame_detections'));
        req.files
            .add(await http.MultipartFile.fromPath('file', frame0File.path));
        final resp = await req.send();
        if (resp.statusCode == 200) {
          final body = jsonDecode(await resp.stream.bytesToString());
          if (body['image_base64'] != null) {
            frame0Bytes = base64Decode(body['image_base64'] as String);
            frameSize = Size(
              (body['width'] as num).toDouble(),
              (body['height'] as num).toDouble(),
            );
          }
        }
      } catch (e) {
        print('Live Frame 0 fetch error: $e');
      }

      try {
        await File(frame0File.path).delete();
      } catch (e) {}

      if (mounted) {
        setState(() {
          _isInitializingCamera = false;
        });
      }

      if (!mounted) return;
      final List<Offset>? tappedPoints = await _showPocketCalibrationDialog(
        frame0Bytes: frame0Bytes,
        frameSize: frameSize,
      );

      if (tappedPoints == null) return;

      var startReq =
          http.MultipartRequest('POST', Uri.parse('$serverUrl/live/start'));
      if (tappedPoints.length == 6) {
        final pocketJson = jsonEncode(
            tappedPoints.map((p) => {'x': p.dx, 'y': p.dy}).toList());
        startReq.fields['pocket_points'] = pocketJson;
      }

      var req = await startReq.send();
      if (req.statusCode == 200) {
        String respStr = await req.stream.bytesToString();
        var data = jsonDecode(respStr);
        setState(() {
          _liveSessionId = data['session_id'];
          _isLiveTracking = true;
          _liveStats = null;
          _cameraFrameResult = null;
        });

        // 100 ms (10 FPS) via HTTP + disk IO is the smooth ceiling on most devices;
        // 33 ms (30 FPS) causes camera preview lag.
        _liveTimer =
            Timer.periodic(const Duration(milliseconds: 100), (timer) async {
          if (!_isLiveTracking || !mounted) {
            timer.cancel();
            return;
          }
          if (_isFrameProcessing) return;
          if (_cameraController == null ||
              !_cameraController!.value.isInitialized) {
            return;
          }

          _isFrameProcessing = true;
          try {
            XFile file = await _cameraController!.takePicture();
            var request = http.MultipartRequest(
                'POST', Uri.parse('$serverUrl/live/frame'));
            request.fields['session_id'] = _liveSessionId!;
            request.files
                .add(await http.MultipartFile.fromPath('file', file.path));

            var resp = await request.send();

            // Delete temp image to prevent disk fill-up and IO lag.
            try {
              await File(file.path).delete();
            } catch (e) {
              print("Error deleting temp file: $e");
            }

            if (resp.statusCode == 200) {
              String respStr = await resp.stream.bytesToString();
              var body = jsonDecode(respStr);
              if (body['image'] != null && mounted) {
                var bytes = base64Decode(body['image']);
                setState(() {
                  if (body['stats'] != null) {
                    _updatePottedSequencesFromStats(
                        Map<String, dynamic>.from(body['stats']));
                  }
                  _cameraFrameResult = Image.memory(bytes,
                      gaplessPlayback: true, fit: BoxFit.contain);
                  _liveStats = body['stats'];
                });
              }
            }
          } catch (e) {
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

  /// Stop the live analysis timer and update UI.
  void _stopLiveTracking() {
    setState(() {
      _isLiveTracking = false;
      _liveTimer?.cancel();
      _liveTimer = null;
    });
  }

  /// Build the Live tab layout with camera preview and start/stop controls.
  Widget _buildLiveBody() {
    bool isCamReady =
        _cameraController != null && _cameraController!.value.isInitialized;

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
                  if (isCamReady &&
                      !_isLiveTracking &&
                      _cameraFrameResult == null)
                    CameraPreview(_cameraController!),
                  if (_cameraFrameResult != null)
                    SizedBox(
                        width: double.infinity,
                        height: double.infinity,
                        child: _cameraFrameResult!),
                  if (!isCamReady)
                    _isInitializingCamera
                        ? const Center(
                            child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              CircularProgressIndicator(),
                              SizedBox(height: 10),
                              Text("Loading Camera...",
                                  style: TextStyle(color: Colors.white)),
                            ],
                          ))
                        : Center(
                            child: ElevatedButton.icon(
                              onPressed: _initCamera,
                              icon: const Icon(Icons.camera_alt),
                              label: Text(cameraButtonText),
                              style: ElevatedButton.styleFrom(
                                backgroundColor: Colors.white,
                                foregroundColor: Colors.black,
                                padding: const EdgeInsets.symmetric(
                                    horizontal: 24, vertical: 12),
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
                            style: ElevatedButton.styleFrom(
                                backgroundColor: Colors.red,
                                foregroundColor: Colors.white),
                          )
                        : ElevatedButton.icon(
                            onPressed: isCamReady ? _startLiveTracking : null,
                            icon: const Icon(Icons.play_arrow),
                            label: const Text("Start Live Tracking"),
                            style: ElevatedButton.styleFrom(
                                backgroundColor: Colors.green,
                                foregroundColor: Colors.white),
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

// ─────────────────────────────────────────────────────────────────────────────
// Pocket Calibration Dialog
// ─────────────────────────────────────────────────────────────────────────────

/// Full-screen dialog for tapping 6 pocket positions on a frame-0 preview.
class _PocketCalibrationDialog extends StatefulWidget {
  final Uint8List? frame0Bytes;
  final Size? frameSize;

  const _PocketCalibrationDialog({this.frame0Bytes, this.frameSize});

  @override
  State<_PocketCalibrationDialog> createState() =>
      _PocketCalibrationDialogState();
}

class _PocketCalibrationDialogState extends State<_PocketCalibrationDialog> {
  final List<Offset> _taps = [];
  final GlobalKey _imageKey = GlobalKey();

  static const int _requiredPoints = 6;
  static const double _dotRadius = 14.0;
  // Server uses a 70 px radius in video-pixel space to decide if a ball is potted.
  static const double _pocketZoneVideoRadius = 70.0;

  /// Convert a widget-space tap to video pixel coordinates.
  Offset _toVideoCoords(Offset widgetTap) {
    if (widget.frameSize == null) return widgetTap;
    final box = _imageKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return widgetTap;
    final widgetSize = box.size;
    final scaleX = widget.frameSize!.width / widgetSize.width;
    final scaleY = widget.frameSize!.height / widgetSize.height;
    return Offset(widgetTap.dx * scaleX, widgetTap.dy * scaleY);
  }

  /// The pocket zone radius in widget-display pixels.
  double _pocketZoneWidgetRadius() {
    if (widget.frameSize == null) return _pocketZoneVideoRadius;
    final box = _imageKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return _pocketZoneVideoRadius;
    final widgetSize = box.size;
    // Average x/y scales; for snooker the aspect is fixed so they are close.
    // Use the smaller scale so the circle fits both axes under letterboxing.
    final scaleX = widget.frameSize!.width / widgetSize.width;
    final scaleY = widget.frameSize!.height / widgetSize.height;
    final scale = (scaleX + scaleY) / 2.0;
    return _pocketZoneVideoRadius / scale;
  }

  void _onTap(TapDownDetails details) {
    if (_taps.length >= _requiredPoints) return;
    final box = _imageKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return;
    final local = box.globalToLocal(details.globalPosition);
    setState(() => _taps.add(local));
  }

  void _undo() {
    if (_taps.isEmpty) return;
    setState(() => _taps.removeLast());
  }

  void _confirm() {
    if (_taps.length != _requiredPoints) return;
    final videoCoords = _taps.map(_toVideoCoords).toList();
    Navigator.of(context).pop(videoCoords);
  }

  @override
  Widget build(BuildContext context) {
    final bool hasImage = widget.frame0Bytes != null;
    final remaining = _requiredPoints - _taps.length;

    return Dialog(
      insetPadding: const EdgeInsets.all(12),
      backgroundColor: Colors.black,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              children: [
                const Text(
                  'Pocket Calibration',
                  style: TextStyle(
                      color: Colors.white,
                      fontSize: 18,
                      fontWeight: FontWeight.bold),
                ),
                const SizedBox(height: 4),
                Text(
                  remaining > 0
                      ? 'Tap each pocket corner ($remaining left)'
                      : '✓ All 6 pockets marked! Tap Confirm to proceed.',
                  style: TextStyle(
                    color: remaining == 0 ? Colors.greenAccent : Colors.white70,
                    fontSize: 13,
                  ),
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 4),
                const Text(
                  'Green circle = 70 px valid-pot zone',
                  style: TextStyle(color: Colors.white38, fontSize: 11),
                  textAlign: TextAlign.center,
                ),
              ],
            ),
          ),
          Flexible(
            child: Center(
              child: AspectRatio(
                aspectRatio: widget.frameSize != null
                    ? widget.frameSize!.width / widget.frameSize!.height
                    : 16 / 9,
                child: GestureDetector(
                  onTapDown: _onTap,
                  child: Stack(
                    children: [
                      hasImage
                          ? Image.memory(
                              widget.frame0Bytes!,
                              key: _imageKey,
                              fit: BoxFit.contain,
                              width: double.infinity,
                            )
                          : Container(
                              key: _imageKey,
                              height: 300,
                              width: double.infinity,
                              color: Colors.grey.shade900,
                              child: const Center(
                                child: Column(
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  children: [
                                    Icon(Icons.image_not_supported,
                                        color: Colors.white38, size: 48),
                                    SizedBox(height: 8),
                                    Text(
                                      'Preview unavailable.\nTap the 6 pocket corners on the real table.',
                                      style: TextStyle(color: Colors.white54),
                                      textAlign: TextAlign.center,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                      for (int i = 0; i < _taps.length; i++)
                        Positioned(
                          left: _taps[i].dx - _pocketZoneWidgetRadius(),
                          top: _taps[i].dy - _pocketZoneWidgetRadius(),
                          child: Container(
                            width: _pocketZoneWidgetRadius() * 2,
                            height: _pocketZoneWidgetRadius() * 2,
                            decoration: BoxDecoration(
                              color: Colors.greenAccent.withValues(alpha: 0.12),
                              shape: BoxShape.circle,
                              border: Border.all(
                                color:
                                    Colors.greenAccent.withValues(alpha: 0.70),
                                width: 1.5,
                              ),
                            ),
                          ),
                        ),
                      for (int i = 0; i < _taps.length; i++)
                        Positioned(
                          left: _taps[i].dx - _dotRadius,
                          top: _taps[i].dy - _dotRadius,
                          child: Container(
                            width: _dotRadius * 2,
                            height: _dotRadius * 2,
                            decoration: BoxDecoration(
                              color: Colors.greenAccent.withValues(alpha: 0.85),
                              shape: BoxShape.circle,
                              border:
                                  Border.all(color: Colors.white, width: 1.5),
                            ),
                            alignment: Alignment.center,
                            child: Text(
                              '${i + 1}',
                              style: const TextStyle(
                                color: Colors.black,
                                fontSize: 11,
                                fontWeight: FontWeight.bold,
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                TextButton(
                  onPressed: () => Navigator.of(context).pop(null),
                  child:
                      const Text('Cancel', style: TextStyle(color: Colors.red)),
                ),
                Row(
                  children: [
                    if (_taps.isNotEmpty)
                      TextButton.icon(
                        onPressed: _undo,
                        icon: const Icon(Icons.undo, color: Colors.orange),
                        label: const Text('Undo',
                            style: TextStyle(color: Colors.orange)),
                      ),
                    if (_taps.isNotEmpty)
                      TextButton.icon(
                        onPressed: () => setState(() => _taps.clear()),
                        icon: const Icon(Icons.refresh, color: Colors.white54),
                        label: const Text('Reset',
                            style: TextStyle(color: Colors.white54)),
                      ),
                    ElevatedButton(
                      onPressed:
                          _taps.length == _requiredPoints ? _confirm : null,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.green,
                        foregroundColor: Colors.white,
                      ),
                      child: const Text('Confirm'),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
