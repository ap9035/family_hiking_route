#!/usr/bin/env python3
"""Generate overview and printable contour maps from a destination osm-map.json."""

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config",type=Path,help="Destination osm-map.json")
    parser.add_argument("--refresh",action="store_true",help="Refresh cached OSM and NLSC map data")
    parser.add_argument("--scale",type=int,help="Contour print scale denominator, e.g. 10000")
    args=parser.parse_args()
    config=args.config if args.config.is_absolute() else (Path.cwd()/args.config)
    overview=[sys.executable,str(ROOT/"scripts"/"generate_osm_route_map.py"),str(config)]
    contours=[sys.executable,str(ROOT/"scripts"/"generate_contour_maps.py"),str(config)]
    if args.refresh:
        overview.append("--refresh")
        contours.append("--refresh")
    if args.scale:
        contours.extend(["--scale",str(args.scale)])
    subprocess.run(overview,check=True)
    subprocess.run(contours,check=True)


if __name__=="__main__":
    main()
