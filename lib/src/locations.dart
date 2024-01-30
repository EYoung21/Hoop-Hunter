import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:json_annotation/json_annotation.dart';

part 'locations.g.dart';

@JsonSerializable()
class BasketballCourt {
  BasketballCourt({
    required this.Prop_ID,
    required this.Name,
    required this.Location,
    required this.Num_of_Courts,
    required this.Accessible,
    required this.lat,
    required this.lon,
  });

  factory BasketballCourt.fromJson(Map<String, dynamic> json) =>
      _$BasketballCourtFromJson(json);
  Map<String, dynamic> toJson() => _$BasketballCourtToJson(this);

  final String Prop_ID;
  final String Name;
  final String Location;
  final String? Num_of_Courts;
  final String Accessible;
  final double lat;
  final double lon;
}

Future<List<BasketballCourt>> getBasketballCourts() async {
  final String jsonString = await rootBundle.loadString('assets/NYCcourts.json');
  final List<dynamic> jsonList = json.decode(jsonString);

  List<BasketballCourt> basketballCourts = [];
  for (var json in jsonList) {
    try {
      // Cast json to Map<String, dynamic> to access its properties
      Map<String, dynamic> jsonMap = json as Map<String, dynamic>;

      // Parse lat and lon as doubles
      double lat = double.parse(jsonMap['lat']);
      double lon = double.parse(jsonMap['lon']);

      // Create a new Map with updated lat and lon values
      Map<String, dynamic> updatedJson = {
        ...jsonMap,
        'lat': lat,
        'lon': lon,
      };

      // Create a BasketballCourt object from the updated JSON map
      basketballCourts.add(BasketballCourt.fromJson(updatedJson));
    } catch (e) {
      print('Error parsing basketball court data: $e');
      continue;
      // Handle the error here, such as logging it or skipping the invalid data
    }
  }

  return basketballCourts;
}