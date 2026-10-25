#!/usr/bin/env python3
"""Create one A4 printable contour map from official 20 m DTM data.

Calculates 10 m contour intervals from the MOI 20 m DTM XYZ grids and overlays
route geometry from the same osm-map.json used by generate_osm_route_map.py.
Produces a self-contained SVG and, if rsvg-convert is installed, a PDF.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
from html import escape
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import zipfile

from generate_osm_route_map import ROOT, get_osm_xml, osm_cache_path, parse_osm, route_points


DTM_INDEX_URL = "https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/B6219841-E247-4743-958D-77FB61067092/resource/D788C60B-33CB-4A84-A438-EC6E8B7060F1/download"
DTM_INDEX_PAGE = "https://data.gov.tw/dataset/178729"
DTM_LICENSE = "https://data.gov.tw/licenses"
DTM_SPACING_M = 20
CONTOUR_INTERVAL_M = 10
USER_AGENT = "weekly-hiking-contour-map/1.0 (+https://github.com/ap9035/family_hiking_route)"


def fetch(url: str, cache: Path, refresh: bool) -> bytes:
    if cache.exists() and not refresh:
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "180",
         "--user-agent", USER_AGENT, url],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    cache.write_bytes(result.stdout)
    return result.stdout


def get_dtm_archive(region: str, refresh: bool) -> tuple[Path, str]:
    index_path = ROOT / ".cache" / "dtm" / "index-2026.csv"
    index_data = fetch(DTM_INDEX_URL, index_path, refresh)
    rows = csv.DictReader(io.StringIO(index_data.decode("utf-8-sig")))
    match = next((row for row in rows if region in row.get("圖資名稱", "") and "20MDEM" in row.get("圖資名稱", "")), None)
    if not match:
        raise SystemExit(f"No 2026 20 m DTM archive for region {region!r} in official index.")
    url = match["連結網址"].replace(":443", "")
    archive = ROOT / ".cache" / "dtm" / f"{region}-20m-2026.zip"
    data = fetch(url, archive, refresh)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise SystemExit(f"Official DTM download for {region} was not a ZIP archive.")
    return archive, "2026"


def extract_dtm_grid(archive: Path, bbox) -> dict[tuple[int, int], float]:
    """Read only 20 m XYZ tiles intersecting map bounds, with a one-cell margin."""
    min_e, min_n, max_e, max_n = bbox
    grid = {}
    with zipfile.ZipFile(archive) as zf:
        names = {Path(name).stem.lower(): name for name in zf.namelist() if name.lower().endswith(".grd")}
        for stem, grd_name in names.items():
            hdr_name = next((n for n in zf.namelist() if Path(n).stem.lower() == stem and n.lower().endswith(".hdr")), None)
            if not hdr_name:
                continue
            try:
                fields = zf.read(hdr_name).decode("big5", errors="replace").splitlines()
                spacing = int(float(fields[5]))
                columns, rows = int(fields[8]), int(fields[9])
                origin_e, origin_n = int(float(fields[10])), int(float(fields[11]))
            except (IndexError, ValueError):
                continue
            tile_min_e, tile_max_e = origin_e, origin_e + (columns - 1) * spacing
            # XYZ rows increase northward; the header gives the southwest origin.
            tile_min_n, tile_max_n = origin_n, origin_n + (rows - 1) * spacing
            if tile_max_e < min_e-spacing or tile_min_e > max_e+spacing or tile_max_n < min_n-spacing or tile_min_n > max_n+spacing:
                continue
            with zf.open(grd_name) as stream:
                for raw in stream:
                    try:
                        east_s, north_s, elev_s = raw.decode("ascii").split()[:3]
                        east, north, elevation = int(float(east_s)), int(float(north_s)), float(elev_s)
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if min_e-spacing <= east <= max_e+spacing and min_n-spacing <= north <= max_n+spacing and elevation > -9000:
                        grid[(east, north)] = elevation
    if not grid:
        raise SystemExit("No DTM samples intersect the print extent; verify print_map.dtm_region and map scale.")
    return grid


def contour_segments(grid: dict[tuple[int, int], float], interval=CONTOUR_INTERVAL_M):
    """Marching-squares contours over the official 20 m regular XYZ grid."""
    by_north = {}
    for (east, north), elevation in grid.items():
        by_north.setdefault(north, {})[east] = elevation
    northings = sorted(by_north, reverse=True)
    eastings = sorted({east for row in by_north.values() for east in row})
    contours = []
    for north, south in zip(northings, northings[1:]):
        if north - south != DTM_SPACING_M:
            continue
        north_row, south_row = by_north[north], by_north[south]
        for west, east in zip(eastings, eastings[1:]):
            if east-west != DTM_SPACING_M:
                continue
            vals = [north_row.get(west), north_row.get(east), south_row.get(east), south_row.get(west)]
            if any(value is None or value <= -9000 for value in vals):
                continue
            z0,z1,z2,z3 = vals
            low, high = min(vals), max(vals)
            first_level = (math.floor(low / interval) + 1) * interval
            last_level = math.floor(high / interval) * interval
            for level in range(first_level, last_level + 1, interval):
                corners = [(west,north,z0),(east,north,z1),(east,south,z2),(west,south,z3)]
                points=[]
                for i in range(4):
                    ax,ay,az=corners[i]
                    bx,by,bz=corners[(i+1)%4]
                    if (az < level <= bz) or (bz < level <= az):
                        t=(level-az)/(bz-az)
                        points.append((ax+t*(bx-ax),ay+t*(by-ay)))
                if len(points)==2:
                    contours.append((level,points[0],points[1]))
                elif len(points)==4:
                    # Deterministic saddle-case split; the source grid remains 20 m.
                    contours.extend(((level,points[0],points[1]),(level,points[2],points[3])))
    return contours


def wgs84_to_twd97(point: tuple[float, float]) -> tuple[float, float]:
    """Convert (lat, lon) to Taiwan TM2 zone 121 (EPSG:3826), metres."""
    lat_deg, lon_deg = point
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = 2 * f - f * f
    ep2 = e2 / (1 - e2)
    k0 = 0.9999
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    lon0 = math.radians(121.0)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    tan_lat = math.tan(lat)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = ep2 * cos_lat * cos_lat
    aa = (lon - lon0) * cos_lat
    e4, e6 = e2 * e2, e2 * e2 * e2
    m = a * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat)
        - (35 * e6 / 3072) * math.sin(6 * lat)
    )
    east = 250000 + k0 * n * (
        aa + (1 - t + c) * aa**3 / 6
        + (5 - 18*t + t*t + 72*c - 58*ep2) * aa**5 / 120
    )
    north = k0 * (
        m + n * tan_lat * (
            aa**2 / 2 + (5 - t + 9*c + 4*c*c) * aa**4 / 24
            + (61 - 58*t + t*t + 600*c - 330*ep2) * aa**6 / 720
        )
    )
    return east, north


def get_center(config: dict) -> tuple[float, float]:
    print_config = config.get("print_map", {})
    if "center" in print_config:
        return tuple(map(float, print_config["center"]))
    min_lon, min_lat, max_lon, max_lat = map(float, config["bbox"])
    return ((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)


def make_wms_bbox(config: dict):
    p = config.get("print_map", {})
    scale = int(p.get("scale_denominator", 10000))
    map_width_mm = float(p.get("map_width_mm", 277))
    map_height_mm = float(p.get("map_height_mm", 165))
    center = get_center(config)
    center_e, center_n = wgs84_to_twd97(center)
    ground_width = map_width_mm * scale / 1000
    ground_height = map_height_mm * scale / 1000
    bbox = (
        center_e - ground_width / 2,
        center_n - ground_height / 2,
        center_e + ground_width / 2,
        center_n + ground_height / 2,
    )
    # Preserve the physical A4 extent and its configured print scale.
    width = round(map_width_mm)
    height = round(map_height_mm)
    return bbox, width, height, scale, map_width_mm, map_height_mm


def map_xy(point, bbox, frame):
    east, north = wgs84_to_twd97(point)
    min_e, min_n, max_e, max_n = bbox
    x, y, width, height = frame
    return (
        x + (east - min_e) / (max_e - min_e) * width,
        y + (max_n - north) / (max_n - min_n) * height,
    )


def projected_xy(point, bbox, frame):
    min_e, min_n, max_e, max_n = bbox
    x, y, width, height = frame
    east, north = point
    return (x + (east-min_e)/(max_e-min_e)*width,
            y + (max_n-north)/(max_n-min_n)*height)


def svg_line(points, bbox, frame):
    coords = [map_xy(tuple(point), bbox, frame) for point in points]
    if not coords:
        return ""
    return f'M {coords[0][0]:.2f} {coords[0][1]:.2f}' + "".join(
        f" L {x:.2f} {y:.2f}" for x, y in coords[1:]
    )


def build_sheet(config, osm_ways, contours, bbox, scale, data_date: str) -> str:
    p = config.get("print_map", {})
    page_w, page_h = 2970, 2100  # A4 landscape, coordinates in 0.1 mm units.
    frame = (
        100,
        int(float(p.get("top_margin_mm", 21)) * 10),
        int(float(p.get("map_width_mm", 277)) * 10),
        int(float(p.get("map_height_mm", 165)) * 10),
    )
    title = p.get("title", f"{config['title']}｜等高線地圖")
    title = escape(title)
    subtitle = escape(f"A4 橫式｜1:{scale:,}｜DTM 網格 20 m｜等高距 10 m｜TWD97 TM2（EPSG:3826）")
    min_e, min_n, max_e, max_n = bbox
    x, y, frame_w, frame_h = frame
    grid_spacing = int(p.get("grid_spacing_m", 500))
    map_lines = []
    # TWD97 graticule with coordinate values around the map frame.
    start_e = math.ceil(min_e / grid_spacing) * grid_spacing
    easting = start_e
    while easting <= max_e:
        px = x + (easting - min_e) / (max_e - min_e) * frame_w
        map_lines.append(f'<line x1="{px:.2f}" y1="{y}" x2="{px:.2f}" y2="{y+frame_h}" class="grid"/>')
        map_lines.append(f'<text x="{px:.2f}" y="{y-12}" text-anchor="middle" class="coord">{easting:.0f}</text>')
        easting += grid_spacing
    start_n = math.ceil(min_n / grid_spacing) * grid_spacing
    northing = start_n
    while northing <= max_n:
        py = y + (max_n - northing) / (max_n - min_n) * frame_h
        map_lines.append(f'<line x1="{x}" y1="{py:.2f}" x2="{x+frame_w}" y2="{py:.2f}" class="grid"/>')
        map_lines.append(f'<text x="{x+8}" y="{py-7:.2f}" class="coord">N {northing:.0f}</text>')
        northing += grid_spacing

    contour_paths = {}
    for level, start, end in contours:
        x1,y1=projected_xy(start,bbox,frame)
        x2,y2=projected_xy(end,bbox,frame)
        contour_paths.setdefault(level,[]).append(f"M{x1:.2f},{y1:.2f}L{x2:.2f},{y2:.2f}")
    contour_svg=[]
    for level, paths in sorted(contour_paths.items()):
        klass="index-contour" if level % 50 == 0 else "contour"
        contour_svg.append(f'<path class="{klass}" d="{"".join(paths)}"/>')

    roads_svg=[]
    for way in osm_ways.values():
        highway=way["tags"].get("highway")
        if highway in {"motorway","trunk","primary","secondary","tertiary","unclassified","residential","service","footway","path","track","steps"}:
            d=svg_line(way["points"],bbox,frame)
            roads_svg.append(f'<path class="road {escape(highway)}" d="{d}"/>')

    route_svg = []
    combined = []
    for segment in config.get("route_segments", []):
        pts = route_points(segment, osm_ways)
        if segment.get("style") == "approx":
            if combined:
                route_svg.append(f'<path d="{svg_line(combined,bbox,frame)}" class="route-outline"/><path d="{svg_line(combined,bbox,frame)}" class="route"/>')
                combined = []
            route_svg.append(f'<path d="{svg_line(pts,bbox,frame)}" class="connector-outline"/><path d="{svg_line(pts,bbox,frame)}" class="connector"/>')
            continue
        if combined and pts and combined[-1] == pts[0]:
            combined.extend(pts[1:])
        else:
            combined.extend(pts)
    if combined:
        d = svg_line(combined,bbox,frame)
        route_svg.append(f'<path d="{d}" class="route-outline"/><path d="{d}" class="route"/>')
    for connector in config.get("connectors", []):
        d = svg_line(connector["points"],bbox,frame)
        route_svg.append(f'<path d="{d}" class="connector-outline"/><path d="{d}" class="connector"/>')
    for marker in config.get("markers", []):
        mx,my = map_xy(tuple(marker["coordinates"]),bbox,frame)
        anchor = marker.get("anchor","start")
        dx = float(marker.get("dx", 12 if anchor=="start" else -12))
        dy = float(marker.get("dy", -12))
        text_anchor = marker.get("text_anchor",anchor)
        route_svg.append(f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="12" class="marker"/>')
        route_svg.append(f'<text x="{mx+dx:.2f}" y="{my+dy:.2f}" text-anchor="{text_anchor}" class="label">{escape(marker["label"])}</text>')
        if marker.get("detail"):
            route_svg.append(f'<text x="{mx+dx:.2f}" y="{my+dy+17:.2f}" text-anchor="{text_anchor}" class="small">{escape(marker["detail"])}</text>')

    # True scale bar in the projected map frame.
    bar_m = int(p.get("scale_bar_m", 500))
    bar_w = bar_m / (max_e-min_e) * frame_w
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="297mm" height="210mm" viewBox="0 0 {page_w} {page_h}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title><desc id="desc">A4 printable contour map at scale 1:{scale:,}, with TWD97 coordinate grid.</desc>
<defs>
 <clipPath id="map-clip"><rect x="{x}" y="{y}" width="{frame_w}" height="{frame_h}"/></clipPath>
 <style>text{{font-family:sans-serif;fill:#202820}} .title{{font-size:62px;font-weight:700}} .subtitle{{font-size:31px;fill:#46564b}} .grid{{stroke:#496c8266;stroke-width:2;stroke-dasharray:7 7}} .coord{{font-size:23px;fill:#394c55;paint-order:stroke;stroke:white;stroke-width:5}} .contour{{fill:none;stroke:#997444;stroke-width:2.2;opacity:.85}} .index-contour{{fill:none;stroke:#76512c;stroke-width:4;opacity:.92}} .road{{fill:none;stroke:#aaa08d;stroke-width:5;stroke-linecap:round;stroke-linejoin:round}} .road.footway,.road.path,.road.steps,.road.track{{stroke:#8f8674;stroke-width:3;stroke-dasharray:11 8}} .route-outline{{fill:none;stroke:white;stroke-width:25;stroke-linecap:round;stroke-linejoin:round}} .route{{fill:none;stroke:#e23c2f;stroke-width:15;stroke-linecap:round;stroke-linejoin:round}} .connector-outline{{fill:none;stroke:white;stroke-width:22;stroke-dasharray:24 18;stroke-linecap:round}} .connector{{fill:none;stroke:#ed8c20;stroke-width:12;stroke-dasharray:22 18;stroke-linecap:round}} .marker{{fill:white;stroke:#bd3328;stroke-width:8}} .label{{font-size:34px;font-weight:700;paint-order:stroke;stroke:white;stroke-width:9;stroke-linejoin:round}} .small{{font-size:28px;paint-order:stroke;stroke:white;stroke-width:8;stroke-linejoin:round}} .frame{{fill:none;stroke:#35433b;stroke-width:3}}</style>
 <marker id="north" markerWidth="12" markerHeight="12" refX="6" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M6 0 L12 12 L6 9 L0 12 Z" fill="#202820"/></marker>
</defs>
<rect width="2970" height="2100" fill="#fff"/>
<text id="title" x="100" y="104" class="title">{title}</text>
<text x="100" y="155" class="subtitle">{subtitle}</text>
<rect x="{x}" y="{y}" width="{frame_w}" height="{frame_h}" fill="#f7f3e6"/>
<g clip-path="url(#map-clip)">
{''.join(roads_svg)}
{''.join(contour_svg)}
<g>{''.join(map_lines)}</g>
{''.join(route_svg)}
</g>
<rect x="{x}" y="{y}" width="{frame_w}" height="{frame_h}" class="frame"/>
<g transform="translate({x+frame_w-95} {y+115})"><text x="0" y="-32" class="label" text-anchor="middle">N</text><path d="M0 0 L0 75" stroke="#202820" stroke-width="5" marker-end="url(#north)"/></g>
<g transform="translate({x+50} {y+frame_h-55})"><rect width="{bar_w/2:.1f}" height="16" fill="#202820"/><rect x="{bar_w/2:.1f}" width="{bar_w/2:.1f}" height="16" fill="#fff" stroke="#202820" stroke-width="3"/><text x="{bar_w/2:.1f}" y="-16" class="small" text-anchor="middle">{bar_m} m</text></g>
<text x="100" y="1970" class="small">高程資料：<a href="{DTM_INDEX_PAGE}">內政部 20 m 網格 DTM</a>｜等高距 10 m｜資料集版本 2026（各圖幅測製年份不同）｜<a href="{DTM_LICENSE}">政府資料開放授權第1版</a></text>
<text x="100" y="2010" class="small">步道／道路：© OpenStreetMap contributors（ODbL）｜格網 {grid_spacing} m｜製圖日 {escape(data_date)}</text>
</svg>'''
    return svg


def write_outputs(config_path: Path, config: dict, contours, osm_data: bytes, bbox, scale, dtm_version: str):
    output_dir = config_path.parent
    prefix = config.get("print_map", {}).get("output_prefix", config_path.parent.name)
    data_date = date.today().isoformat()
    _, ways = parse_osm(osm_data)
    pdf_converter = shutil.which("rsvg-convert")
    created=[]
    svg = build_sheet(config,ways,contours,bbox,scale,data_date)
    svg = svg.replace("資料集版本 2026", f"資料集版本 {dtm_version}")
    svg_path = output_dir / f"{prefix}_等高線圖.svg"
    svg_path.write_text(svg,encoding="utf-8")
    # Validate XML so malformed SVGs are caught before printing.
    import xml.etree.ElementTree as ET
    ET.parse(svg_path)
    created.append(svg_path)
    if pdf_converter:
        pdf_path = output_dir / f"{prefix}_等高線圖.pdf"
        subprocess.run([pdf_converter,"-f","pdf","-o",str(pdf_path),str(svg_path)],check=True)
        created.append(pdf_path)
    return created


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config",type=Path,help="Destination osm-map.json")
    parser.add_argument("--refresh",action="store_true",help="Refresh cached OSM and DTM data")
    parser.add_argument("--scale",type=int,help="Printed scale denominator (default from print_map.scale_denominator, or 10000)")
    args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding="utf-8"))
    p=config.setdefault("print_map",{})
    if args.scale:
        p["scale_denominator"]=args.scale
    scale=int(p.get("scale_denominator",10000))
    if scale <= 0:
        raise SystemExit("Scale denominator must be positive")
    bbox,_,_,scale,_,_=make_wms_bbox(config)
    region=p.get("dtm_region")
    if not region:
        raise SystemExit('Set print_map.dtm_region to an official DTM region name, e.g. "臺北市" or "新北市".')
    archive,dtm_version=get_dtm_archive(region,args.refresh)
    samples=extract_dtm_grid(archive,bbox)
    contours=contour_segments(samples)
    if not contours:
        raise SystemExit("No 10 m contour segments were calculated inside the print extent.")
    osm_bbox=list(map(float,config["bbox"]))
    osm_cache=osm_cache_path(osm_bbox)
    osm_data,_=get_osm_xml(osm_bbox,osm_cache,args.refresh)
    files=write_outputs(args.config,config,contours,osm_data,bbox,scale,dtm_version)
    for file in files:
        print(f"Generated {file} ({file.stat().st_size:,} bytes)")
    if not shutil.which("rsvg-convert"):
        print("rsvg-convert is unavailable; an SVG contour map was generated. Open it in a browser or SVG viewer to print.")


if __name__=="__main__":
    main()
