"""OSM tag rules shared by the regional and nationwide steps.

* ``is_basketball``: the feature's ``sport`` value contains "basketball"
  (``basketball``, ``basketball;tennis``, ``3x3_basketball`` ...).
* ``is_outdoor``: not indoor / covered / a building / a sports hall or arena.
* ``court_like``: the feature *is* a court (``leisure=pitch`` or a bare
  ``sport=basketball`` area), not a park, school or centre that merely has one.
* ``indoor_kind``: membership in the indoor-court layer.
* ``context_kinds``: hard-negative and scan-mask categories.
"""

from __future__ import annotations

from collections.abc import Mapping

EXCLUDED_LEISURE = {"sports_centre", "stadium", "sports_hall", "fitness_centre"}

# Features that can *contain* a court somewhere inside them; their polygon is not the court.
CONTAINER_LEISURE = {
    "park", "playground", "recreation_ground", "sports_centre", "stadium", "sports_hall", "fitness_centre",
    "schoolyard", "common", "garden", "nature_reserve", "resort", "water_park", "camp_site", "summer_camp",
    "marina", "golf_course", "track", "dog_park",
}
CONTAINER_KEYS = ("amenity", "landuse", "building", "shop", "club", "office", "tourism", "healthcare", "military")

# Small pitches that look like courts from above (used for "pitch_other" hard negatives).
MULTI_SPORT_VALUES = {"multi", "multisport", "multi-sport"}

# Scan-mask kinds (where public courts usually are) and hard-negative kinds.
MASK_KINDS = ("park", "school", "college", "worship", "community_centre", "recreation_ground", "sports_centre",
              "apartments")
NEGATIVE_KINDS = ("pitch_tennis", "pitch_other", "parking", "pool", "playground", "park", "school")
# Kinds whose polygons are dropped in the nationwide context layer (only the center is needed).
POINT_ONLY_KINDS = {"parking"}


def sport_values(tags: Mapping[str, str]) -> set[str]:
    return {s.strip().lower() for s in str(tags.get("sport", "")).split(";") if s.strip()}


def is_basketball(tags: Mapping[str, str]) -> bool:
    return "basketball" in str(tags.get("sport", "")).lower()


def is_indoorish(tags: Mapping[str, str]) -> bool:
    return (
        tags.get("indoor") in ("yes", "room", "area")
        or tags.get("covered") == "yes"
        or "building" in tags
        or tags.get("location") in ("indoor", "underground")
    )


def is_outdoor(tags: Mapping[str, str]) -> bool:
    if is_indoorish(tags):
        return False
    return tags.get("leisure") not in EXCLUDED_LEISURE


def court_like(tags: Mapping[str, str]) -> bool:
    leisure = tags.get("leisure")
    if leisure == "pitch":
        return True
    if leisure in CONTAINER_LEISURE:
        return False
    return not any(k in tags for k in CONTAINER_KEYS)


def indoor_kind(tags: Mapping[str, str]) -> str | None:
    """Kind of indoor basketball venue, or None. Only meaningful for basketball features."""
    leisure, amenity, building = tags.get("leisure"), tags.get("amenity"), tags.get("building")
    if leisure in ("sports_centre", "fitness_centre", "sports_hall"):
        return leisure
    if amenity == "community_centre":
        return "community_centre"
    if building in ("sports_hall", "sports_centre"):
        return "sports_hall"
    if leisure == "pitch" and is_indoorish(tags):
        return "indoor_pitch"
    return None


def context_kinds(tags: Mapping[str, str], basketball: bool | None = None) -> list[str]:
    """Hard-negative / scan-mask categories of a feature (may be several)."""
    if basketball is None:
        basketball = is_basketball(tags)
    kinds: list[str] = []
    leisure, amenity, landuse = tags.get("leisure"), tags.get("amenity"), tags.get("landuse")
    indoorish = is_indoorish(tags)
    if leisure == "pitch" and not basketball and not indoorish:
        sports = sport_values(tags)
        if sports and not (sports & MULTI_SPORT_VALUES):  # multi-sport pads often have hoops: not a safe negative
            kinds.append("pitch_tennis" if "tennis" in sports else "pitch_other")
    if amenity == "parking" and tags.get("parking") != "underground" and "building" not in tags:
        kinds.append("parking")
    if (leisure == "swimming_pool" or amenity == "swimming_pool") and not indoorish:
        kinds.append("pool")
    if leisure == "playground":
        kinds.append("playground")
    if leisure == "park":
        kinds.append("park")
    if amenity == "school":
        kinds.append("school")
    if amenity in ("college", "university"):
        kinds.append("college")
    if amenity == "place_of_worship":
        kinds.append("worship")
    if amenity == "community_centre":
        kinds.append("community_centre")
    if landuse == "recreation_ground" or leisure == "recreation_ground":
        kinds.append("recreation_ground")
    if leisure == "sports_centre":
        kinds.append("sports_centre")
    if tags.get("residential") == "apartments" and landuse in ("residential", None):
        kinds.append("apartments")
    return kinds


# osmium tags-filter expressions that keep everything the rules above can match.
OSMIUM_FILTERS = [
    "nwr/sport",
    "nwr/leisure=pitch,park,playground,recreation_ground,sports_centre,sports_hall,fitness_centre,swimming_pool",
    "nwr/amenity=parking,school,college,university,place_of_worship,community_centre,swimming_pool",
    "nwr/landuse=recreation_ground",
    "nwr/residential=apartments",
]
