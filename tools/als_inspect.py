#!/usr/bin/env python3
"""
Inspect the device chains of an Ableton Live set (.als) without opening Live.

A .als file is gzip-compressed XML, so a set that crashes Live on load can still
be read here. Useful for auditing what plugins live on a group's tracks when
something in that chain is destabilising the session.

Usage:
    ./als_inspect.py "Hard Times.als"                 # list every track
    ./als_inspect.py "Hard Times.als" --group Vox     # just the Vox group
    ./als_inspect.py "Hard Times.als" --group Vox --json
"""

import argparse
import gzip
import json
import os
import sys
import xml.etree.ElementTree as ET

TRACK_TAGS = ("MidiTrack", "AudioTrack", "GroupTrack", "ReturnTrack", "MasterTrack")

#--------------------------------------------------------------------------------
# Element tags for third-party plugin wrappers. These are the usual suspects when
# a set crashes on load: Live has to instantiate external binary code to open them.
#--------------------------------------------------------------------------------
PLUGIN_TAGS = ("PluginDevice", "AuPluginDevice", "Vst3PluginDevice")

#--------------------------------------------------------------------------------
# Devices that nest further device chains inside <Branches>.
#--------------------------------------------------------------------------------
RACK_TAGS = ("AudioEffectGroupDevice", "InstrumentGroupDevice",
             "MidiEffectGroupDevice", "DrumGroupDevice")


def load_xml(path):
    """Parse a .als (gzipped XML) or a plain .xml dump of one."""
    try:
        with gzip.open(path, "rb") as fd:
            data = fd.read()
    except (OSError, gzip.BadGzipFile):
        with open(path, "rb") as fd:
            data = fd.read()
    return ET.fromstring(data)


def value_of(element, path):
    """Read the Value attribute of a descendant, or None if absent."""
    if element is None:
        return None
    found = element.find(path)
    if found is None:
        return None
    return found.get("Value")


def device_name(device):
    """
    Best-effort display name for a device element.

    Live stores a user-assigned name in UserName; when that's blank it falls back
    to the device's own name, and for plugins to the name reported by the plugin.
    """
    for path in ("UserName",
                 "PluginDesc/VstPluginInfo/PlugName",
                 "PluginDesc/Vst3PluginInfo/Name",
                 "PluginDesc/AuPluginInfo/Name",
                 "Name"):
        name = value_of(device, path)
        if name:
            return name
    return device.tag


def plugin_path(device):
    """Filesystem path of a plugin binary, where the set records one."""
    for path in ("PluginDesc/VstPluginInfo/Path",
                 "PluginDesc/Vst3PluginInfo/Path",
                 "PluginDesc/AuPluginInfo/Path",
                 "PluginDesc/VstPluginInfo/FileName",
                 "PluginDesc/Vst3PluginInfo/Uid"):
        found = value_of(device, path)
        if found:
            return found
    # Some versions nest the path under a FileRef instead.
    for ref in device.iter("FileRef"):
        for path in ("Path", "Name", "RelativePath"):
            found = value_of(ref, path)
            if found:
                return found
    return None


def is_enabled(device):
    enabled = value_of(device, "On/Manual")
    if enabled is None:
        return True
    return enabled.lower() == "true"


def collect_devices(container, depth=0):
    """
    Walk a <Devices> element, returning each device in chain order.

    Racks are recursed into so that plugins buried inside an Audio Effect Rack
    are reported too, tagged with the branch that holds them.
    """
    devices = []
    if container is None:
        return devices

    for device in list(container):
        entry = {
            "tag": device.tag,
            "name": device_name(device),
            "enabled": is_enabled(device),
            "is_plugin": device.tag in PLUGIN_TAGS,
            "depth": depth,
            "path": plugin_path(device) if device.tag in PLUGIN_TAGS else None,
            "branches": [],
        }

        if device.tag in RACK_TAGS:
            branches = device.find("Branches")
            for branch in list(branches) if branches is not None else []:
                branch_name = value_of(branch, "Name/EffectiveName") or \
                              value_of(branch, "Name/UserName") or "(unnamed branch)"
                nested = []
                for sub in branch.iter("Devices"):
                    nested.extend(collect_devices(sub, depth + 1))
                entry["branches"].append({"name": branch_name, "devices": nested})

        devices.append(entry)

    return devices


def track_devices(track):
    """
    Devices on a track's own chain, excluding any nested inside racks.

    Tracks are siblings in the XML rather than nested, so every <Devices> under a
    track element belongs to it -- but rack branches contain their own <Devices>,
    which collect_devices already handles. Take only the top-level chain here.
    """
    inner = {}
    for parent in track.iter():
        for child in parent:
            if child.tag == "Devices":
                inner[id(child)] = (parent, child)

    # A rack branch's Devices sits under a DeviceChain whose ancestor is a
    # rack Branches element. Identify top-level chains by checking that no
    # ancestor between the track and the Devices node is a Branches element.
    parent_map = {id(c): p for p in track.iter() for c in p}

    def under_branches(element):
        current = parent_map.get(id(element))
        while current is not None:
            if current.tag == "Branches":
                return True
            current = parent_map.get(id(current))
        return False

    for _, (_, devices_element) in inner.items():
        if not under_branches(devices_element):
            return collect_devices(devices_element)
    return []


def parse_tracks(root):
    tracks = []
    for track in root.iter():
        if track.tag not in TRACK_TAGS:
            continue
        tracks.append({
            "id": track.get("Id"),
            "tag": track.tag,
            "name": value_of(track, "Name/EffectiveName") or
                    value_of(track, "Name/UserName") or "(unnamed)",
            "group_id": value_of(track, "TrackGroupId"),
            "devices": track_devices(track),
        })
    return tracks


def members_of_group(tracks, group_name):
    """The group track itself plus every track nested under it, recursively."""
    matches = [t for t in tracks
               if t["tag"] == "GroupTrack" and t["name"].lower() == group_name.lower()]
    if not matches:
        return None, []

    group = matches[0]
    members, frontier = [], [group["id"]]
    while frontier:
        parent_id = frontier.pop()
        for track in tracks:
            if track["group_id"] == parent_id:
                members.append(track)
                frontier.append(track["id"])
    return group, members


def print_devices(devices, indent="    "):
    if not devices:
        print("%s(no devices)" % indent)
        return

    for index, device in enumerate(devices):
        marker = "" if device["enabled"] else "  [OFF]"
        kind = "  <- %s" % device["tag"] if device["is_plugin"] else ""
        print("%s%2d. %s%s%s" % (indent, index, device["name"], kind, marker))
        if device["path"]:
            print("%s      path: %s" % (indent, device["path"]))
        for branch in device["branches"]:
            print("%s      branch: %s" % (indent, branch["name"]))
            print_devices(branch["devices"], indent + "        ")


def flatten_plugins(devices, track_name, out):
    for device in devices:
        if device["is_plugin"]:
            out.append((track_name, device["name"], device["path"]))
        for branch in device["branches"]:
            flatten_plugins(branch["devices"], track_name, out)
    return out


def main():
    parser = argparse.ArgumentParser(description="Inspect device chains in a .als file")
    parser.add_argument("file", help="Path to the .als set")
    parser.add_argument("--group", help="Only report this group track and its members")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print("No such file: %s" % args.file, file=sys.stderr)
        return 1

    root = load_xml(args.file)
    tracks = parse_tracks(root)

    if args.group:
        group, members = members_of_group(tracks, args.group)
        if group is None:
            print("No group track named %r. Groups in this set:" % args.group)
            for track in tracks:
                if track["tag"] == "GroupTrack":
                    print("  - %s" % track["name"])
            return 1
        selected = [group] + members
    else:
        selected = tracks

    if args.json:
        print(json.dumps(selected, indent=2))
        return 0

    version = root.get("Creator") or "unknown"
    print("Set: %s" % os.path.basename(args.file))
    print("Saved by: %s" % version)
    print("Tracks in set: %d\n" % len(tracks))

    plugins = []
    for track in selected:
        label = "GROUP" if track["tag"] == "GroupTrack" else track["tag"].replace("Track", "")
        print("[%s] %s" % (label, track["name"]))
        print_devices(track["devices"])
        flatten_plugins(track["devices"], track["name"], plugins)
        print()

    print("-" * 60)
    print("Third-party plugins in this selection: %d" % len(plugins))
    if plugins:
        counts = {}
        for track_name, name, path in plugins:
            counts[name] = counts.get(name, 0) + 1
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print("  %-40s x%d" % (name, count))
        print("\nThese are the load-time crash candidates: Live must instantiate")
        print("external binary code for each. Built-in Live devices very rarely")
        print("crash a set on open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
