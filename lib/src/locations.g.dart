// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'locations.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

BasketballCourt _$BasketballCourtFromJson(Map<String, dynamic> json) =>
    BasketballCourt(
      Prop_ID: json['Prop_ID'] as String,
      Name: json['Name'] as String,
      Location: json['Location'] as String,
      Num_of_Courts: json['Num_of_Courts'] as String?,
      Accessible: json['Accessible'] as String,
      lat: (json['lat'] as num).toDouble(),
      lon: (json['lon'] as num).toDouble(),
    );

Map<String, dynamic> _$BasketballCourtToJson(BasketballCourt instance) =>
    <String, dynamic>{
      'Prop_ID': instance.Prop_ID,
      'Name': instance.Name,
      'Location': instance.Location,
      'Num_of_Courts': instance.Num_of_Courts,
      'Accessible': instance.Accessible,
      'lat': instance.lat,
      'lon': instance.lon,
    };
