import 'package:flutter/material.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:google_maps_in_flutter/src/locations.dart' as locations;

void main() {
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({Key? key}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: Colors.green[700],
      ),
      home: const BasketballCourtsMap(),
    );
  }
}

class BasketballCourtsMap extends StatefulWidget {
  const BasketballCourtsMap({Key? key}) : super(key: key);

  @override
  _BasketballCourtsMapState createState() => _BasketballCourtsMapState();
}

class _BasketballCourtsMapState extends State<BasketballCourtsMap> {
  final Map<String, Marker> _markers = {};

  @override
  void initState() {
    super.initState();
    _loadBasketballCourts();
  }

  Future<void> _loadBasketballCourts() async {
    final basketballCourts = await locations.getBasketballCourts();
    setState(() {
      _markers.clear();
      for (final court in basketballCourts) {
        final marker = Marker(
          markerId: MarkerId(court.Name),
          position: LatLng(court.lat, court.lon),
          infoWindow: InfoWindow(
            title: court.Name,
            snippet: court.Location,
          ),
        );
        _markers[court.Name] = marker;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Basketball Courts in NYC'),
        elevation: 2,
      ),
      body: GoogleMap(
        initialCameraPosition: const CameraPosition(
          target: LatLng(40.7128, -74.0060), // NYC coordinates
          zoom: 12,
        ),
        markers: _markers.values.toSet(),
      ),
    );
  }
}