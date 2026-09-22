# SchematicPositionsToPCB_v9.py
# KiCad v9 Action Plugin: place PCB footprints at the same XY (and rotation) as their schematic symbols.
#
# How it works
# 1) Finds your project’s main .kicad_sch (same basename as the .kicad_pcb).
# 2) Parses every (symbol ...) block to read:
#      - Reference (from: (property "Reference" "R1" ...))
#      - Position & rotation (from: (at x y [angle]))
#    (Units in schematic files v6+ are mm; angle in degrees.)
# 3) For each footprint with a matching reference in the PCB, sets:
#      - position to (x + dx, y + dy) in mm (dx, dy configurable below)
#      - rotation to the schematic angle (optional)
#
# Notes
# - This is intended for the FIRST rough placement. It won’t route or respect DRC—just puts parts down.
# - Hierarchical sheets are fine—the positions are absolute within the schematic page; we just copy them over.
# - If your schematic has multiple sheets with overlapping coordinates, consider applying an offset (dx/dy).
#
# Tested with KiCad 9.x Python API.
#
# MIT License.

import os
import re
import pcbnew
import wx   # add this near the other imports

PLUGIN_NAME = "Schematic → PCB: Copy Positions"
PLUGIN_CATEGORY = "Placement"
PLUGIN_DESCRIPTION = "Places footprints at the same XY (and rotation) as their schematic symbols."
PLUGIN_SHOW_TOOLBAR_BUTTON = True

# --- User options ---
APPLY_ROTATION = True      # Set footprint rotation to schematic symbol rotation
DX_MM = 0.0                # Global X offset (mm) to apply on PCB
DY_MM = 0.0                # Global Y offset (mm) to apply on PCB
NORMALIZE_TO_ORIGIN = True  # If True, subtracts the min (x,y) from all placements before applying DX/DY

# Global default (often 0 or 90)
GLOBAL_ROT_OFFSET_DEG = 0

# Offsets by reference prefix (e.g., WP1, WP2… for waveplates)
ROT_OFFSET_BY_REF_PREFIX = {
    # "AP": -15,      # Aperture: 15°
    # "A": -90,       # AOM: -90°
    # "D": 90,        # Detector: 90°
    # "L": -90,      # Lens: -90°
    # "M": -90-45,   # Mirror: -90-45°
    # "PBS": 90,       # PBS: 90°
    # # "S": -180,    # Shutter: -180°
    # # "WPR": 90,      # Waveplate: 90°
    # "WPr": 90,      # Wedge Prism: 90°
}

# Offsets by footprint name (matches substring of lib:footprint)
ROT_OFFSET_BY_FOOTPRINT = {
    "ApertureLeft": 15,                 # footprint name match
    "ApertureRight": -15,                 # footprint name match
    "CleaningCube": 90,                 # footprint name match
    "G&H_AOM_+xMHz": -90,                 # footprint name match
    "G&H_AOM_-xMHz": -90,                 # footprint name match
    "Lens3mmNormal": -90,                 # footprint name match
    "Lens6mmNormal": -90,                 # footprint name match
    "Mirror3mm": -90-45,                 # footprint name match
    "Mirror3mmLeft": -90-45,                 # footprint name match
    "Mirror3mmRight": -90-45,                 # footprint name match
    "Mirror6mm": -90-45,                 # footprint name match
    "Mirror6mmNormal": -90,                 # footprint name match
    "PBS": 90,                 # footprint name match
    "PBS_Flipped": 90,                 # footprint name match
    "Wedge_Prism_12.7mm": 90,     # footprint name match
}

# Absolute overrides by exact reference (rarely needed)
ROT_ABSOLUTE_BY_REF = {
    # "WP1": 0,
}

# Offsets by footprint name (matches substring of Footprint:footprint)
X_OFFSET_BY_FOOTPRINT = {
    "ApertureLeft": -2.7,     # Aperture
    "ApertureRight": 2.7,     # Aperture
    # "C": 4.5,      # Collimator: 4.5 mm
}

# Offsets by footprint name (matches substring of Footprint:footprint)
Y_OFFSET_BY_FOOTPRINT = {
    # "AP": 9.5,     # Aperture
    "S&KCollimator_V1": 4.5,      # Collimator: 4.5 mm
}
# ---------------------

def _read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _find_main_schematic_path(board_path):
    """
    Try to locate the main .kicad_sch using the PCB filename's stem.
    e.g. /foo/bar/myproj.kicad_pcb -> /foo/bar/myproj.kicad_sch
    """
    stem, _ = os.path.splitext(board_path)
    cand = stem + ".kicad_sch"
    if os.path.exists(cand):
        return cand
    # Fallback: search in the same folder for a single .kicad_sch
    folder = os.path.dirname(board_path)
    schs = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".kicad_sch")]
    return schs[0] if len(schs) == 1 else None

def _iter_symbol_blocks(sch_text):
    """
    Yield full text of each (symbol ... ) S-expression block.
    We do a lightweight S-expression scan (depth counter).
    """
    i = 0
    n = len(sch_text)
    while True:
        start = sch_text.find("(symbol", i)
        if start == -1:
            return
        depth = 0
        j = start
        while j < n:
            c = sch_text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    # end of this (symbol ...) block
                    yield sch_text[start:j+1]
                    i = j + 1
                    break
            j += 1
        else:
            # malformed, bail out
            return

def _parse_symbol_block(block):
    """
    Extract (ref, optics, x_mm, y_mm, rot_deg) from a (symbol ...) block.
    - Reference from: (property "Reference" "R1" ...)
    - Optics from: (symbol "Optics:Lens" ...)
    - Position from:   (at x y [angle])
    Returns None on failure.
    """
    # Reference
    mref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block)
    if not mref:
        return None
    ref = mref.group(1).strip()
    # _msg(f"{mref}")

    # Optics
    moptics = re.search(r'\(property\s+"Footprint"\s+"([^"]+)"', block)
    # moptics = re.search(r'\(symbol\s+"([^"]+)"', block)
    if not moptics:
        return None
    # Extract the part after the colon if it exists, otherwise use the full string
    optics = moptics.group(1).strip().split(":")[-1]
    # _msg(f"{optics}")
    
    # At: (at x y [rot])
    # There can be multiple '(at ...)' lines; prefer the one directly under (symbol ...)
    # but as a heuristic, just take the first (at ...) inside the block.
    mat = re.search(r'\(at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)(?:\s+(-?\d+(?:\.\d+)?))?\)', block)
    if not mat:
        return None
    x = float(mat.group(1))
    y = float(mat.group(2))
    rot = float(mat.group(3)) if mat.group(3) is not None else 0.0

    # return ref, x, y, rot
    return ref, optics, x, y, rot

def _collect_schematic_positions(sch_path):
    """
    Parse the schematic and return:
      positions: dict{ ref -> (x_mm, y_mm, rot_deg) }
      bbox_min: (minx, miny)  (for normalization if requested)
    """
    text = _read_file(sch_path)
    positions = {}
    minx = float("inf")
    miny = float("inf")

    for block in _iter_symbol_blocks(text):
        # _msg(f"{block}")
        parsed = _parse_symbol_block(block)
        # _msg(f"{parsed}")
        # break
        if not parsed:
            continue
        # ref, x, y, rot = parsed
        ref, optics, x, y, rot = parsed
        # positions[ref] = (x, y, rot)
        positions[ref] = (optics, x, y, rot)
        if x < minx: minx = x
        if y < miny: miny = y

    if minx == float("inf"):
        minx, miny = 0.0, 0.0

    return positions, (minx, miny)

def _set_fp_pos_rot(fp, optics, x_mm, y_mm, srot_deg):
    pos = pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))
    fp.SetPosition(pos)

    if APPLY_ROTATION:
        rot = _compute_rot(srot_deg, fp, optics)
        try:
            fp.SetOrientationDegrees(rot)
        except AttributeError:
            fp.SetOrientation(int(rot * 10))

def _compute_rot(srot, fp, optics):
    # start with global
    rot = (srot + GLOBAL_ROT_OFFSET_DEG) % 360

    ref = fp.GetReference()
    for prefix, off in ROT_OFFSET_BY_REF_PREFIX.items():
        if ref.startswith(prefix):
            rot = (srot + off) % 360
            break

    # try:
    #     # _msg(f"fp: {fp.GetFPID()}")
    #     fpid = fp.GetFPID()
    #     fname = fpid.Format() if hasattr(fpid, "Format") else str(fpid)
    # except Exception:
    #     fname = ""
    for key, off in ROT_OFFSET_BY_FOOTPRINT.items():
        # if key and key in fname:
        if key == optics:
            rot = (srot + off) % 360
            break

    if ref in ROT_ABSOLUTE_BY_REF:
        rot = ROT_ABSOLUTE_BY_REF[ref] % 360

    return rot


def _msg(text):
    try:
        wx.MessageBox(text, PLUGIN_NAME)
    except Exception:
        # fallback (e.g., headless): print to scripting console
        print(f"[{PLUGIN_NAME}] {text}")

class SchematicToPCBPositionsPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = PLUGIN_NAME
        self.category = PLUGIN_CATEGORY
        self.description = PLUGIN_DESCRIPTION
        self.show_toolbar_button = PLUGIN_SHOW_TOOLBAR_BUTTON
        self.icon_file_name = ""  # leave blank or point to a .png if you want a toolbar icon

    def Run(self):
        board = pcbnew.GetBoard()
        if not board:
            _msg("No board loaded.")
            return

        pcb_path = board.GetFileName()
        if not pcb_path:
            _msg("Board has no filename yet. Please save the PCB first.")
            return

        sch_path = _find_main_schematic_path(pcb_path)
        if not sch_path or not os.path.exists(sch_path):
            _msg(
                "Could not find a .kicad_sch next to the PCB.\n"
                "Save your project as <name>.kicad_pcb and <name>.kicad_sch in the same folder."
            )
            return

        positions, (minx, miny) = _collect_schematic_positions(sch_path)
        if not positions:
            _msg("No symbol positions found in schematic.")
            return

        # Prepare normalization
        ox = -minx if NORMALIZE_TO_ORIGIN else 0.0
        oy = -miny if NORMALIZE_TO_ORIGIN else 0.0

        # Map PCB footprints by reference
        fp_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
        _msg(f"Footprints mapped by reference: {list(fp_by_ref.keys())}")

        moved = 0
        missing = []
        for ref, (optics, sx, sy, srot) in positions.items():
            fp = fp_by_ref.get(ref)
            if not fp:
                missing.append(ref)
                continue
            x_mm = sx + ox + DX_MM
            y_mm = sy + oy + DY_MM
            for prefix, off in X_OFFSET_BY_FOOTPRINT.items():
                # _msg(f"Checking X offset for ref '{ref}' with prefix '{prefix}, optics '{optics}'")
                if optics == prefix:
                    x_mm = x_mm + off
                    break
            for prefix, off in Y_OFFSET_BY_FOOTPRINT.items():
                if optics == prefix:
                    y_mm = y_mm + off
                    break
            _set_fp_pos_rot(fp, optics, x_mm, y_mm, srot)
            moved += 1

        pcbnew.Refresh()

        msg = f"Placed {moved} footprints from schematic positions."
        # if missing:
        #     msg += f"\nMissing on PCB (skipped): {', '.join(sorted(missing)[:20])}"
        #     if len(missing) > 20:
        #         msg += f", … (+{len(missing)-20} more)"
        _msg(msg)

# Register the plugin with KiCad
SchematicToPCBPositionsPlugin().register()
