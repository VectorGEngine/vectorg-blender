# VectorG Track Exporter

The track exporter creates a ZIP containing:

```text
<track_id>.glb
manifest.json
hdr/env.hdr or hdr/env.exr
maps/<layout_id>.svg
routes/<layout_id>.json
ideal-lines/<layout_id>.json (when an Ideal Line is assigned)
```

Install or enable `vectorg-blender/addons/vectorg_track_exporter` the same way as the
car exporter. The panel is under `View3D > Sidebar > VectorG`.

The optional **HDR** field selects an `.hdr` or `.exr` image texture already
loaded in the Blender file. Packed image textures are supported. The HDR is
written only to `hdr/env.hdr` or `hdr/env.exr`; it is not embedded in the GLB
and must not be used by an exported track material.

**Maximum Texture Size** limits the longest side of exported material textures
without modifying source images. The default track limit is `4096`. When
**Compress Opaque Color Textures** is enabled, textures used only by Principled
BSDF base-color or emission inputs are exported as JPEG at the selected quality.
Alpha, normal, metallic, roughness, mask, and ambiguous textures remain
in their original format and embedded in the GLB.

Set **Package Version** to the asset revision being exported and increment it
intentionally whenever package contents change. It is written as
`packageVersion` and is separate from the manifest schema `version`.

## Mesh instances

The exporter enables glTF GPU instancing when the installed Blender glTF
exporter supports it. For repeated props such as trees, create linked duplicates
with `Alt+D`, give them identical materials, and parent them directly to the
scope's `FOLIAGE_CARDS` Empty. Instances must be meshes without children. Apply
modifiers before creating the linked duplicates when every instance uses the
same evaluated geometry.

Every shared and layout `VISUALS` root contains two behavior roots:

- `PBR` uses the regular lit track-material path.
- `FOLIAGE_CARDS` uses unlit, double-sided alpha cutouts without cast or receive
  shadows.

## Workflow

1. Select **Create Track Structure**.
2. Set the track ID and name.
3. Add one or more layouts.
4. Draw or assign an optional Bezier or Poly map curve for each layout.
5. Move regular visual objects under `PBR` and foliage cards under
   `FOLIAGE_CARDS`.
6. Parent driving collision meshes under the appropriate generated surface.
7. Parent walls, barriers, fences, and props under `OBSTACLES`.
8. Add at least one spawn point. For racing layouts, also add the required
   start/finish volumes and ordered checkpoints.
9. Position and rotate the generated objects in the viewport. Local `-Y` is
   the forward crossing direction.
10. Select **Validate Track**, then **Export Track Zip**.

Use the up and down controls beside the layout list to set their player-facing
order. The exporter writes the manifest `layouts` array in this order, and the
game uses that order in its track-selection interfaces.

Picking a layout map curve moves it under the generated `<layout_id>_MAP` node
while preserving its world transform. The exporter calculates layout length from
that curve, projects it onto world XY for `maps/<layout_id>.svg`, and adaptively
samples its full 3D shape for `routes/<layout_id>.json`. Route samples have a
maximum spacing of 5 metres and become denser around corners and elevation
changes. Route format version 3 stores cumulative distance in metres, world
position, forward direction, up direction, and full road `width` in metres at
every sample. The frame uses
parallel transport for stable orientation and applies the map curve control
points' tilt as road banking. Set the curve tilt to match the road on banked
sections.

The Map Curve's visible thickness defines the legal road width. In Curve Data
Properties, use **3D** and **Geometry > Bevel > Round**, then set **Depth** to
half the base road width. For a 10 m road, use Depth `5 m` and point Radius `1`.
In Edit Mode, select points and use **Alt+S** or the Sidebar's **Radius** field
to vary the width. The exporter calculates `width = 2 * bevel_depth * radius`
in the existing route sampling pass. If curve radius scaling is disabled,
width is the constant bevel diameter. Bezier radius interpolation follows the
curve's Linear, Ease, Cardinal, or B-Spline setting; Poly radii interpolate
linearly. Nonlinear width changes add route samples as needed to keep linear
width interpolation within 1 cm of the authored curve.

Keep the curve and its parents at scale `(1, 1, 1)`, without shear or reflection.
Road curves require positive width, full-length Round bevel, no taper object,
and zero Geometry Offset/Extrude. Validation reports unsupported settings.
The Map Curve must follow the middle of the legal road; thickness expands both
sides equally. Its bevel is for authoring only and remains excluded from the
GLB with the MAP hierarchy. Increment **Package Version** when exporting new
widths. Route version 3 requires the matching game loader.

The route file also stores the projected distance of the start, finish, and
checkpoint events. Circular routes are rebased so the start/finish event is
distance zero. Point-to-point start and finish events must project within 10
metres of their respective curve endpoints. Freeform routes keep the curve's
natural first point as distance zero and require no race events. Race events
are projected onto the route without a distance cap. Checkpoint distance must
increase in checkpoint order.

SVG maps automatically rotate their principal axis horizontally unless the
layout is nearly square. Draw the curve in driving direction. Circular layouts
require one cyclic spline; point-to-point layouts require one open spline. MAP
hierarchies are excluded from the GLB. Layouts without a map curve remain valid
and use the game's placeholder map and spawn-point reset fallback.

The generated route data has this shape:

```json
{
  "version": 3,
  "closed": true,
  "length": 1234.5,
  "maxSpacing": 5.0,
  "frame": "parallel_transport",
  "samples": [
    {
      "s": 0.0,
      "position": [0.0, 0.0, 0.0],
      "forward": [0.0, 0.0, 1.0],
      "up": [0.0, 1.0, 0.0],
      "width": 10.0
    }
  ],
  "events": [
    {
      "object": "gp_start_finish",
      "type": "start_finish",
      "s": 0.0
    },
    {
      "object": "gp_checkpoint_01",
      "type": "checkpoint",
      "s": 400.0,
      "order": 1
    }
  ]
}
```

Changing a layout's **ID** renames its generated hierarchy nodes, addon-created
spawn points, route events, and layout box colliders. The refresh icons beside
**Track ID** and layout **ID** perform the same operation: create missing surface
groups in Shared and every configured layout, then normalize every layout's
generated object names. Refresh also assigns checkpoint order from their order
in the `EVENTS` hierarchy. Existing geometry and surface groups are preserved;
repeated refreshes do not create duplicates. Use either button after updating
the add-on to add the new surface groups to an older track. Missing collision
roots and naming conflicts must be corrected before refreshing.
Changing its display **Name**
only changes player-facing metadata.

Choose **Route Type** per layout. Circular routes use one `start_finish` event;
point-to-point routes use separate `start` and `finish` events. Freeform routes
may use an open or cyclic map curve and require no race events. At least one
spawn point remains required for every route type.

Collision roots contain `tarmac`, `concrete`, `curb`, `grass`, `gravel`,
`dirt`, `mud`, `sand`, `snow`, `ice`, `wet_tarmac`, `wet_concrete`, `wet_curb`,
and `OBSTACLES` as direct children.

The `wet_` groups work like every other surface group. Geometry placed there
stays wet in both Dry and Wet races. In a Wet race, the game also resolves normal
surfaces to their configured `wet_` counterpart when one exists; otherwise it
keeps the authored surface. Snow and ice are persistent surfaces too.

All meshes under a drivable surface group are colliders for that surface.
`OBSTACLES` may contain any organizational hierarchy. Obstacle meshes do not
define a driving surface.
Use **Create Static Box Collider** in the Shared or Layout section to create a
cube Empty under that scope's `OBSTACLES` root. With meshes selected, it matches
their combined world-space bounds; with no meshes selected, it creates a unit
box at the 3D cursor.

With exactly one visual mesh selected, use **Create Dynamic Box Collider** to
create a box matching its bounds under the same scope's `OBSTACLES` root. The
exporter keeps a Blender object pointer to the visual mesh and writes
`vectorg_body = "dynamic"`, `vectorg_target`, `vectorg_mass`, and
`vectorg_shape = "box"` into glTF extras. Mass is calculated from the generated
box volume using a density of `10 kg/m³`, with a minimum mass of `1 kg`.

Collision meshes without a `vectorg_shape` property are treated as trimesh
colliders. Validation and export automatically apply non-negative, non-unit
local scale on collision meshes to their mesh data. Negative collision mesh
scale remains a validation error.

Dynamic collider target objects may retain Blender's dotted duplicate suffixes,
such as `.001`. The exporter temporarily replaces dots with underscores in the
GLB node name and matching `vectorg_target` metadata, then restores the Blender
object name after export. Validation reports a conflict if the converted name
is already used by another object.

Event sensors and box colliders use their exported node transforms. Their world
scale is interpreted as box half-extents, so scale `(5, 1, 2)` produces a box
with size `(10, 2, 4)`. Cube Empties must keep display size `1`; display size is
not exported. The addon locks display size to `1` on newly created cube Empties;
use object scale to change their dimensions.
The exporter writes `vectorg_surface`, `vectorg_shape`, event type, checkpoint
order, and hierarchy roles into glTF extras. Collision meshes are still present
in the GLB and must be hidden by the runtime after physics creation.

Removing a layout from the addon only removes its configuration entry. It does
not delete Blender objects.

## Editable ideal line

In a layout's **Ideal Line** section, set **Edge Clearance (m)** (default 1.5),
then click **Generate Ideal Line** in Object
Mode. Clearance is measured from the line to each road edge: include half the
reference vehicle width plus a safety margin. Road width comes from the Map
Curve's sampled thickness; there is no separate Road Width parameter. A 10 m
wide section with 1.5 m clearance allows offsets of 3.5 m either side of the
Map Curve. Each planning point uses its local width, and any section too narrow
for the requested clearance fails validation/generation.

Generation minimizes a discrete integrated squared-curvature objective inside
that corridor using internal points about 6 m apart. It then fits a simpler
editable 3D Bezier curve, removing unnecessary controls on straights and keeping
more around bends and elevation changes. The fitted curve stays within 0.1 m
of the dense generated curve, preserving open endpoints and tangent continuity
through joins and the closed seam. Export sampling remains independent of the
number of editable controls. Generation preserves route elevation and banking
and attempts to place controls on static collision surfaces. It is a suggested geometric line, not a
vehicle-specific minimum-time solution. A warning identifies an unfinished
optimization or points that could not be placed on a surface.

The selected curve is assigned to **Ideal Line** and parented directly under
the layout's existing MAP node:

```text
layout_gp
  gp_MAP
    gp_map_curve
    gp_ideal_line
```

Use Edit Mode to adjust its control points and handles. You can also assign an
existing Bezier or Poly curve in the Ideal Line picker; parenting preserves its
world transform. A Map Curve cannot also be an Ideal Line, and an ideal-line
object belongs to only one layout. Layout renaming includes the ideal line.

**Circular** layouts require a closed spline; **Point to Point** layouts require
an open spline. **Freeform** uses the Map Curve's open/closed state. Generation
pins the endpoints of open lines. Curves must contain exactly one supported
spline, follow the route's driving direction, and have modifiers applied.

Changing Map Curve thickness or clearance does not alter an existing ideal line. **Regenerate Ideal
Line** explicitly replaces its shape and supports Blender Undo. Export never
regenerates the line and never modifies your control points.

At export, samples are projected along **world Z**, choosing the nearest static
road collision surface above or below the curve within **Surface Search (m)**
(default 2 m in each direction). Only surface-group meshes in Shared and the
selected layout are considered; obstacle meshes and other layouts are excluded.
This handles edits slightly above or below the road. Keep the search distance
small around bridges: the nearest eligible surface is chosen, not a semantic
guess about which road level you intended. Missing hits fail validation/export.
Normals are oriented upwards, and near-vertical faces are rejected.

Distances and orientation frames are calculated after projection, including any
new start/finish seam sample. Circular lines start at their projected start/finish
event; open lines retain both endpoints. The original map, route, and declared
layout length continue to use the Map Curve. Samples outside its local
width/clearance corridor produce warnings for review of hand-edited lines.

The layout manifest gains `idealLine: "ideal-lines/<layout_id>.json"`. The
separate file has `version: 1`, `closed`, `length`, `maxSpacing`,
`frame: "surface_normal"`, `edgeClearance`, `referenceRoute`,
`samples`, and projected `events`. Samples contain `s`, `position`, `forward`,
`up`, and `routeS` (distance on the original route, which can wrap at its seam).
Coordinates use the existing Blender-to-game conversion and distances are in
metres. `up` is the hit normal orthogonalized against the sampled line tangent.
Generated curves also carry the last explicit generation settings in `generation`.
Neither speed targets nor colors are baked into this file. The MAP hierarchy,
including the preview curve, remains excluded from the GLB. Player ribbon
rendering is a subsequent game change.

Validation commands from the repository root:

```text
python -m unittest discover -s tests -v
blender --background --factory-startup --python-exit-code 1 --python tests/test_surface_refresh.py -- --blender
```
