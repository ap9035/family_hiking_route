#!/usr/bin/env python3
"""Generate a small, static SVG hiking map from OpenStreetMap XML and JSON route config.

The script uses only the Python standard library. It fetches a small OSM map area once,
caches the XML under .cache/osm/, and exports a self-contained SVG with OSM attribution.
"""

from __future__ import annotations

import argparse
from datetime import date
from html import escape
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OSM_MAP_API = "https://api.openstreetmap.org/api/0.6/map"
OSM_COPYRIGHT = "https://www.openstreetmap.org/copyright"
USER_AGENT = "weekly-hiking-route-map/1.0 (+https://github.com/ap9035/family_hiking_route)"


def validate_bbox(bbox: list[float]) -> None:
    if len(bbox) != 4:
        raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError("bbox coordinates are invalid")
    if max_lon - min_lon > 0.25 or max_lat - min_lat > 0.25:
        raise ValueError("Keep OSM API bbox at or below 0.25 degrees per side")


def osm_cache_path(bbox: list[float]) -> Path:
    digest = hashlib.sha256(",".join(f"{n:.6f}" for n in bbox).encode()).hexdigest()[:16]
    return ROOT / ".cache" / "osm" / f"{digest}.osm"


def get_osm_xml(bbox: list[float], cache: Path, refresh: bool) -> tuple[bytes, str]:
    if cache.exists() and not refresh:
        return cache.read_bytes(), date.fromtimestamp(cache.stat().st_mtime).isoformat()
    # OSM API bbox order: left,bottom,right,top (lon,lat,lon,lat).
    query = urllib.parse.urlencode({"bbox": ",".join(str(n) for n in bbox)})
    request = urllib.request.Request(
        f"{OSM_MAP_API}?{query}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except Exception as exc:
        if cache.exists():
            print(f"OSM fetch failed; using cached data: {exc}")
            return cache.read_bytes(), date.fromtimestamp(cache.stat().st_mtime).isoformat()
        raise SystemExit(
            f"Unable to fetch OSM data: {exc}\n"
            "Retry later or pass --osm-xml with an OSM XML map extract."
        ) from exc
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data, date.today().isoformat()


def parse_osm(data: bytes):
    root = ET.fromstring(data)
    nodes = {
        node.attrib["id"]: (float(node.attrib["lat"]), float(node.attrib["lon"]))
        for node in root.findall("node")
    }
    ways = {}
    for way in root.findall("way"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        points = [nodes[nd.attrib["ref"]] for nd in way.findall("nd") if nd.attrib["ref"] in nodes]
        if len(points) > 1:
            ways[way.attrib["id"]] = {"tags": tags, "points": points}
    return nodes, ways


def project(point, bbox, transform):
    lat, lon = point
    min_lon, min_lat, max_lon, max_lat = bbox
    left, top, scale, cos_lat = transform
    x = left + (lon - min_lon) * 111320 * cos_lat * scale
    y = top + (max_lat - lat) * 111132 * scale
    return x, y


def svg_path(points, bbox, transform, close=False):
    coords = [project(point, bbox, transform) for point in points]
    if not coords:
        return ""
    result = f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"
    result += "".join(f" L {x:.1f} {y:.1f}" for x, y in coords[1:])
    return result + (" Z" if close else "")


def route_points(segment, ways):
    if "way_id" in segment:
        way = ways.get(str(segment["way_id"]))
        if not way:
            raise ValueError(f"OSM way {segment['way_id']} is outside the map bbox or missing")
        points = way["points"][:]
        if segment.get("reverse"):
            points.reverse()
        return points
    return [tuple(map(float, point)) for point in segment.get("points", [])]


def render(config: dict, osm_data: bytes, output: Path):
    nodes, ways = parse_osm(osm_data)
    bbox = list(map(float, config["bbox"]))
    if len(bbox) != 4:
        raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    min_lon, min_lat, max_lon, max_lat = bbox
    width = int(config.get("width", 1200))
    height = int(config.get("height", 1010))
    left, top = 55.0, 105.0
    map_width, map_height = 880.0, 770.0
    cos_lat = __import__("math").cos(__import__("math").radians((min_lat + max_lat) / 2))
    scale = min(
        map_width / ((max_lon - min_lon) * 111320 * cos_lat),
        map_height / ((max_lat - min_lat) * 111132),
    )
    used_width = (max_lon - min_lon) * 111320 * cos_lat * scale
    used_height = (max_lat - min_lat) * 111132 * scale
    left += (map_width - used_width) / 2
    top += (map_height - used_height) / 2
    transform = (left, top, scale, cos_lat)
    right, bottom = left + used_width, top + used_height
    clip = config.get("clip_id", "mapclip")

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(config["title"])}</title>',
        f'<desc id="desc">{escape(config["description"])}</desc>',
        "<defs>",
        f'<clipPath id="{clip}"><rect x="55" y="105" width="880" height="770"/></clipPath>',
        """<style>
text{font-family:sans-serif;fill:#27362e}.title{font-size:29px;font-weight:700}.subtitle{font-size:15px;fill:#58685e}.label{font-size:18px;font-weight:700;paint-order:stroke;stroke:#fff;stroke-width:1.8px;stroke-linejoin:round}.minor{font-size:14px;fill:#49574e;paint-order:stroke;stroke:#fff;stroke-width:1.3px;stroke-linejoin:round}.road{fill:none;stroke:#d2c9b9;stroke-width:5;stroke-linecap:round;stroke-linejoin:round}.localroad{fill:none;stroke:#e2d9c9;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.service{fill:none;stroke:#bcb6a9;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.footway{fill:none;stroke:#aaa99a;stroke-width:1.3;stroke-linecap:round;stroke-linejoin:round}.path{fill:none;stroke:#97988a;stroke-width:1.5;stroke-dasharray:4 2;stroke-linecap:round;stroke-linejoin:round}.building{fill:#ddd5c8;stroke:#c0b7a9;stroke-width:.8}.routeCase{fill:none;stroke:#fff;stroke-width:9;stroke-linecap:round;stroke-linejoin:round}.route{fill:none;stroke:#df4d3d;stroke-width:5.5;stroke-linecap:round;stroke-linejoin:round}.approx{fill:none;stroke:#e38a35;stroke-width:4;stroke-dasharray:9 7;stroke-linecap:round}.grid{stroke:#9ba89d;stroke-width:.7;stroke-dasharray:3 5;opacity:.42}.mapframe{fill:#f8f8f4;stroke:#7b877f;stroke-width:1.2}
</style>""",
        '<marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L9 4.5 L0 9z" fill="#df4d3d"/></marker>',
        '<marker id="approxArrow" markerWidth="8" markerHeight="8" refX="6.5" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8z" fill="#e38a35"/></marker>',
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="#f4f5ef"/>',
        f'<text x="54" y="48" class="title">{escape(config["title"])}</text>',
        f'<text x="56" y="75" class="subtitle">{escape(config.get("subtitle", ""))}</text>',
        f'<rect x="55" y="105" width="880" height="770" class="mapframe"/>',
        f'<g clip-path="url(#{clip})">',
    ]

    # Geographic grid.
    lon_step = config.get("grid_lon", 0.002)
    lat_step = config.get("grid_lat", 0.002)
    lon = (int(min_lon / lon_step) + 1) * lon_step
    while lon < max_lon:
        x, _ = project(((min_lat + max_lat) / 2, lon), bbox, transform)
        svg.append(f'<line x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" y2="{bottom:.1f}" class="grid"/>')
        lon += lon_step
    lat = (int(min_lat / lat_step) + 1) * lat_step
    while lat < max_lat:
        _, y = project((lat, min_lon), bbox, transform)
        svg.append(f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" class="grid"/>')
        lat += lat_step

    # Land use and buildings.
    for way in ways.values():
        tags, points = way["tags"], way["points"]
        if points[0] != points[-1]:
            continue
        fill = None
        if tags.get("landuse") == "cemetery": fill = "#eee8dc"
        elif tags.get("landuse") == "residential": fill = "#efeee7"
        elif tags.get("landuse") == "industrial": fill = "#eeeae4"
        elif tags.get("landuse") == "meadow": fill = "#e4eedc"
        elif tags.get("leisure") == "park": fill = "#e0edd8"
        elif tags.get("natural") == "water": fill = "#d9e8ec"
        if tags.get("building"):
            svg.append(f'<path d="{svg_path(points, bbox, transform, True)}" class="building"/>')
        elif fill:
            svg.append(f'<path d="{svg_path(points, bbox, transform, True)}" fill="{fill}" stroke="#c7c9bc" stroke-width=".7"/>')

    road_priority = {"motorway": 0, "trunk": 0, "primary": 1, "secondary": 1, "secondary_link": 1,
                     "tertiary": 2, "unclassified": 2, "residential": 2, "living_street": 2,
                     "service": 3, "track": 4, "path": 5, "footway": 5, "steps": 6}
    drawable = []
    for way in ways.values():
        tags, points = way["tags"], way["points"]
        hw = tags.get("highway")
        if hw and points[0] != points[-1]:
            drawable.append((road_priority.get(hw, 7), tags, points))
    for _, tags, points in sorted(drawable, key=lambda item: item[0]):
        hw = tags.get("highway")
        if hw in ("motorway", "trunk", "primary", "secondary", "secondary_link"): cls = "road"
        elif hw in ("tertiary", "unclassified", "residential", "living_street"): cls = "localroad"
        elif hw in ("service", "track"): cls = "service"
        elif hw in ("path", "steps"): cls = "path"
        else: cls = "footway"
        svg.append(f'<path d="{svg_path(points, bbox, transform)}" class="{cls}"/>')
    svg.append("</g>")

    # Overlay route segments and approximate connectors in configured order.
    route_segments = [
        (segment, route_points(segment, ways))
        for segment in config.get("route_segments", [])
    ]
    route_segments = [(segment, points) for segment, points in route_segments if points]
    for index, (segment, points) in enumerate(route_segments):
        d = svg_path(points, bbox, transform)
        style = "approx" if segment.get("style") == "approx" else "route"
        svg.append(f'<path d="{d}" class="routeCase"/>')
        arrow = ' marker-end="url(#arrow)"' if index == len(route_segments) - 1 else ""
        svg.append(f'<path d="{d}" class="{style}"{arrow}/>')
    for connector in config.get("connectors", []):
        points = [tuple(point) for point in connector["points"]]
        svg.append(f'<path d="{svg_path(points, bbox, transform)}" class="approx" marker-end="url(#approxArrow)"/>')

    for marker in config.get("markers", []):
        x, y = project(tuple(marker["coordinates"]), bbox, transform)
        color = marker.get("color", "#c94436")
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7.5" fill="#fff" stroke="{color}" stroke-width="3"/>')
        anchor = marker.get("anchor", "start")
        dx = float(marker.get("dx", 10 if anchor == "start" else -10))
        dy = float(marker.get("dy", -10))
        text_anchor = marker.get("text_anchor", anchor)
        svg.append(f'<text x="{x+dx:.1f}" y="{y+dy:.1f}" text-anchor="{text_anchor}" class="label">{escape(marker["label"])}</text>')
        if marker.get("detail"):
            svg.append(f'<text x="{x+dx:.1f}" y="{y+dy+17:.1f}" text-anchor="{text_anchor}" class="minor">{escape(marker["detail"])}</text>')

    # Map labels configured from mapped OSM feature names/coordinates.
    for label in config.get("map_labels", []):
        x, y = project(tuple(label["coordinates"]), bbox, transform)
        svg.append(f'<text x="{x+label.get("dx", 0):.1f}" y="{y+label.get("dy", 0):.1f}" text-anchor="{label.get("anchor", "middle")}" class="minor">{escape(label["text"])}</text>')

    # North arrow, legend, scale bar and attribution.
    svg.append('<g transform="translate(1000 145)"><path d="M0 45 L18 0 L36 45 L18 35 Z" fill="#31473b"/><text x="18" y="-8" text-anchor="middle" class="label">N</text></g>')
    svg.append('<g transform="translate(980 250)">')
    svg.append('<text x="0" y="0" class="label">圖例</text>')
    svg.append('<line x1="0" y1="25" x2="54" y2="25" class="route"/><text x="66" y="30" class="minor">OSM步道疊繪路線</text>')
    svg.append('<line x1="0" y1="54" x2="54" y2="54" class="approx"/><text x="66" y="59" class="minor">步道接駁</text>')
    svg.append('<line x1="0" y1="83" x2="54" y2="83" class="localroad"/><text x="66" y="88" class="minor">道路</text>')
    svg.append('<line x1="0" y1="112" x2="54" y2="112" class="path"/><text x="66" y="117" class="minor">其他步徑</text>')
    for i, line in enumerate(config.get("map_info", [])):
        svg.append(f'<text x="0" y="{168+i*20}" class="minor">{escape(line)}</text>')
    svg.append("</g>")
    bar_width = float(config.get("scale_bar_m", 500)) * scale
    svg.append(f'<g transform="translate(80 {bottom-40:.1f})"><rect x="0" y="0" width="{bar_width/2:.1f}" height="7" fill="#26372e"/><rect x="{bar_width/2:.1f}" y="0" width="{bar_width/2:.1f}" height="7" fill="#fff" stroke="#26372e"/><text x="{bar_width/2:.1f}" y="-7" text-anchor="middle" class="minor">{config.get("scale_bar_m", 500)} m</text></g>')
    attribution = escape(config.get("attribution", "© OpenStreetMap contributors (ODbL)"))
    svg.append(f'<text x="56" y="930" class="minor">底圖資料：{attribution}；資料日期 {escape(config.get("data_date", date.today().isoformat()))}</text>')
    svg.append(f'<text x="56" y="953" class="minor">路線資料：{escape(config.get("route_source", "使用者提供路線資料"))}</text>')
    svg.append("</svg>")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(svg), encoding="utf-8")
    # Parse the output before reporting success.
    ET.parse(output)
    print(f"Generated {output} ({output.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="JSON route-map configuration")
    parser.add_argument("--refresh", action="store_true", help="fetch fresh OSM XML instead of using the local cache")
    parser.add_argument("--osm-xml", type=Path, help="use an existing OSM XML file; do not access the network")
    parser.add_argument("--output", type=Path, help="override output SVG path")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    bbox = list(map(float, config["bbox"]))
    validate_bbox(bbox)
    if args.osm_xml:
        osm_data = args.osm_xml.read_bytes()
        config.setdefault("data_date", date.today().isoformat())
    else:
        cache = osm_cache_path(bbox)
        osm_data, data_date = get_osm_xml(bbox, cache, args.refresh)
        config["data_date"] = data_date
    output = args.output or (args.config.parent / config.get("output", "route-map.svg"))
    render(config, osm_data, output)


if __name__ == "__main__":
    main()
