# VectorG Track Exporter

The track exporter creates a ZIP containing:

```text
<track_id>.glb
manifest.json
hdr/env.hdr or hdr/env.exr
maps/<layout_id>.svg
routes/<layout_id>.json
ideal-lines/<layout_id>.json (when an Ideal Line is assigned)
preview.jpg (when a Preview Image is assigned)
lightmaps/<scope>_<n>.png (after Bake Shadows)
lightmaps/<scope>_grass.png (after Bake Shadows, for a scope with grass)
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
scope's `TREES` Empty. Instances must be meshes without children. Apply
modifiers before creating the linked duplicates when every instance uses the
same evaluated geometry.

Every shared and layout `VISUALS` root contains three behavior roots:

- `PBR` uses the regular lit track-material path.
- `FOLIAGE_CARDS` uses the regular lit material path and casts shadows, but does
  not receive shadows. Authored sidedness and alpha settings are preserved.
- `DECALS` holds skid marks, painted lines, and other meshes lying just above a
  surface. They use the regular lit material path, never cast shadows, and are
  not baked: each takes the baked lighting of the surface under it (see
  Lightmaps).

Every `FOLIAGE_CARDS` root contains two groups, and every foliage object goes
in one of them:

- `TREES` holds trees, bushes and any other plant (role `foliage_trees`). The
  game shades everything in it with one tree shader: parts whose base color
  texture has cut-out holes are leaf cards lit by their normal map, solid
  parts are bark lit by their real surface. Each model's crown outline is
  measured from its geometry, and every part darkens toward the trunk and
  brightens toward the branch tips; this inner shade is full at the crown's
  bottom and fades out toward its top, and the sun reaching the crown falls
  off toward its bottom. A leaf card without a
  normal map gets ambient light only. Leaf textures put each twig's base at
  the image's bottom and its outer end at the top; the normal map's green
  tilt toward the top lights the card, read in the texture's own frame, so
  the same map lights a card the same way whether it stands, slants or hangs.
- `GRASS` holds grass (role `foliage_grass`). Grass is not baked: it takes the
  baked lighting of the ground under it (see Baked shadows).

Validation fails for an object placed directly under `FOLIAGE_CARDS`.

## Merged static meshes

With **Merge Same-Material Meshes** on (the default, next to JPEG Quality),
export joins the static meshes under every `PBR` root into as few nodes as the
data allows, because the game pays per draw call, not per triangle. Meshes join
when they share the materials their faces use, lightmap atlas, shadow casting
(Ray Visibility > Shadow), UV layer count and color layers, and lie in the same
300 m ground cell of the track, so distant cells still cull. UV map and color
layer names do not matter. The joined node takes the group's lightmap
and shadow settings; modifiers are applied and world transforms baked in. The
Blender scene is not changed: the joined objects exist only during the export.

Left as their own nodes: linked duplicates (they export as GPU instances
already), meshes with children, dynamic collider targets, and everything
outside `PBR` (foliage cards, decals,
obstacles, colliders, events, spawn points). Objects with actions merge in their
current pose; the track GLB carries no animation. The Blender console prints
how many meshes were merged.

## Workflow

1. Select **Create Track Structure**.
2. Set the track ID and name.
3. Add one or more layouts.
4. Draw or assign an optional Bezier or Poly map curve for each layout.
5. Move regular visual objects under `PBR`, decals under `DECALS`, trees and
   bushes under `FOLIAGE_CARDS/TREES`, and grass under `FOLIAGE_CARDS/GRASS`.
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
the add-on to add the new surface groups to an older track. Refresh also moves
surface groups that carry a renamed surface ID (`curb` → `kerb`, `wet_curb` →
`wet_kerb`) onto the current ID: the group is relabeled and renamed, or, when a
group for the current ID already exists, its contents move into that group with
their world transforms preserved and the old group is removed. Refresh creates
any missing `DECALS` root under every `VISUALS` root, and any missing `TREES`
and `GRASS` group under every `FOLIAGE_CARDS` root and moves
objects placed directly under `FOLIAGE_CARDS` into `TREES` with their world
transforms preserved; move the grass into `GRASS` afterwards. Missing collision
roots and naming conflicts must be corrected before refreshing.
Changing its display **Name**
only changes player-facing metadata.

Choose **Route Type** per layout. Circular routes use one `start_finish` event;
point-to-point routes use separate `start` and `finish` events. Freeform routes
may use an open or cyclic map curve and require no race events. At least one
spawn point remains required for every route type.

Choose the supported vehicle **Classes** per layout from the class toggles
(K Kart, C Street, B Sport, A GT, R Rally, F Formula). At least one class is
required. The layout exports them as `vehicleClasses`; multiplayer rooms and
leaderboards exist only for these classes, using the class cars whose track
types match the layout's **Track Types**.

Collision roots contain `tarmac`, `concrete`, `kerb`, `grass`, `gravel`,
`dirt`, `mud`, `sand`, `snow`, `ice`, `wet_tarmac`, `wet_concrete`, `wet_kerb`,
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

Visual meshes cast live shadows in the game unless their shadow visibility is
off in Blender (Object Properties > Visibility > Ray Visibility > Shadow). Export
writes `vectorg_cast_shadow: false` into those meshes' glTF extras; the property
exists only during export. Turn it off on ground surfaces such as road, grass,
and kerbs, which receive shadows but never cast them onto anything. Meshes
under `DECALS` always export with `vectorg_cast_shadow: false`.

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

**Smooth Ideal Line** (shown once a line is assigned) removes kinks left by hand
edits: it recomputes a minimum-curvature line that stays within 25 cm of the
current path, re-snaps every point to the road surface, and refits the editable
Bezier. Use it after moving control points so the in-game speed profile has no
local curvature spikes (stray blue patches inside braking zones). It cannot widen
a corner drawn too tight; regenerate for that. Supports Blender Undo.

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

## Track preview image

The **Preview** section, above **Layouts**, holds the track-selection background
image. The thumbnail at the top shows the current **Preview Image**; its picker
and open button accept any PNG or JPEG in place of a render.

**Add Render Camera** only adds `<track_id>_preview_camera` to a `PREVIEW`
empty outside the track root (created if missing). The 35 mm camera sits 1 m
above the first layout's first spawn point, level and facing the spawn point's
-Y axis (the direction a car spawned there faces); without a spawn point in the
first layout it is placed at the 3D cursor facing the cursor's -Y axis. The X
button deletes a camera the exporter created (and the `PREVIEW` empty once it is
empty) and only clears a camera you assigned.

**Set Up Preview Scene** is available once a Render Camera is set. It moves the
Render Camera under the `PREVIEW` empty (keeping its placement) and adds
`<track_id>_preview_sun` to the `PREVIEW` empty if
it has no sun lamp yet (an existing one is kept as edited). The lamp has
strength 6 and shadows, and points from the brightest texel in the upper half of
the track HDR, or from 45 degrees elevation without an HDR. It enables rendering
only for Shared and the selected layout's VISUALS, the Render Camera, and the
`PREVIEW` empty, and disables it for every other object in the scene (MAP
curves, ideal lines, collisions, events, other layouts), so Blender's own Render
Image matches the preview; click it again after selecting another layout. It feeds the track
HDR into the scene world's Background node through an Environment Texture node
(reusing one already connected there, and creating the world, Background, and
output nodes if missing). It then makes the Render Camera the scene camera and
applies, each time it is clicked: EEVEE at
1920x1080, Khronos PBR Neutral view transform at +1 exposure, 64 samples,
screen-traced ray tracing, 2 shadow rays, overscan, and world Sun Threshold 0 so
EEVEE does not extract a second sun from the HDR.
These settings stay on the scene. It needs Blender 4.2 or newer.

**Render Preview** starts Blender's interactive render with the scene's current
render settings, shown wherever **Render Image** shows renders. When the render
finishes, the result is packed into the .blend as `<track_id>_preview`,
replacing the previous render. Only Shared, the selected layout's VISUALS, and
the `PREVIEW` empty render; other objects' render visibility, the scene camera,
and the output file format are restored when the render finishes or is
cancelled. A cancelled render leaves the preview image unchanged.

Export writes the image as `preview.jpg` at the package JPEG quality, scaled to
at most 1920 px, and adds `preview: "preview.jpg"` to the manifest root.
Validation warns when the track has no preview or one narrower than 1280 px.

## Baked shadows

The **Lighting** section, between **Preview** and **Layouts**, bakes static
shadows into lightmaps with Cycles. The game draws no live shadows from track
geometry; baked lightmaps shade the track and only cars cast live shadows.

1. Click **Set Up Preview Scene**. The bake uses its preview sun lamp: its
   direction and its Angle (shadow softness). Edit the lamp before baking.
2. Set **Texel Size (m)** (default 0.25 m per lightmap pixel), **Atlas Size
   (px)** (2048 or 4096), and **Bake Samples** (default 256).
3. Click **Bake Shadows**. Esc cancels between steps; a cancelled or failed bake
   clears all lightmaps.

Every mesh under a scope's `PBR` root receives a lightmap. Meshes under `TREES`
and linked duplicates (meshes sharing their data) are skipped, because instances
cannot hold unique lightmap UVs.

The bake honours each object's **Object Properties › Visibility › Ray
Visibility › Shadow**: a receiver with it off casts neither shadow nor ambient
occlusion onto anything, and still receives both from every other object.

Meshes under `DECALS` are not baked and cast nothing in the bake. Each scope's
decals get their own `Lightmap` UV map, unwrapped like a receiver's, in decal
atlases `lightmaps/<scope>_decals_<n>.png`. Those take the finest texel size of
the receiver atlases the decals sit on, at the smallest atlas size that holds
them. After the bake, every decal texel copies the ambient occlusion and sun
shadow of the receiver surface behind it: the nearest receiver of its own scope
or of Shared along the decal face's normal, within 0.5 m either side. Lines and
skid marks copy the road under them, and a window decal copies the wall it is
on. Texels off the decals take the nearest copied texels. The bake fails, naming
the decal, when a decal shares its mesh data, has four other UV maps, has
modifiers that add or remove geometry, or has no receiver behind it.

Meshes under `GRASS` are not baked either. After the bake, each scope's grass
gets one top-down map, `lightmaps/<scope>_grass.png`: a square centred on its
grass at up to 0.5 m per texel, 64 px or larger, capped at the atlas size. Every
texel within 3 texels of grass casts a ray straight down from 1 m above the
highest grass around it onto the receivers of its own scope and of Shared, and
copies the ambient occlusion and sun shadow of the receiver's lightmap where it
lands; texels with no receiver under them take the nearest copied texels. Each
grass mesh gets a `Lightmap` UV map that maps every corner straight down onto
that map, so grass shows the baked shade of the ground it stands on: tree,
house, and rock shadows included. The bake fails, naming the mesh, when a grass
mesh shares its mesh data, has four other UV maps, has modifiers that add or
remove geometry, or has no receiver under it. Grass with Shadow ray visibility
on casts into the bake like any other object, onto the ground and so onto
itself; turn it off on grass.

Each receiver gets a fresh `Lightmap` UV map in
its last UV slot; glTF exports UV maps in slot order, so the lightmap is the
mesh's last `TEXCOORD_<n>` set, and the game reads the last UV set as the
lightmap. Existing UV maps and their render/active flags are kept. The game reads
at most four UV sets, so a receiver may have at most three other UV maps.
Modifiers that add or remove geometry (Array, Mirror, Solidify, Boolean,
Subdivision, and similar) must be applied first, because generated faces would
repeat the lightmap UVs of their source faces; deforming modifiers are fine.

Shared meshes bake with only Shared visuals casting shadows. A layout's meshes
bake with Shared and that layout's visuals, so objects of one layout never
shadow another. Each scope fills atlases with neighbouring receivers up to the
area one atlas holds at the texel size; the last atlas shrinks to the smallest
power of two (at least 512 px) that holds its receivers. A receiver larger than
one atlas, such as outer terrain, gets an atlas of its own at the finest texel
size that fits, with a warning.

Unwrapping starts from Smart UV Project. Its islands are cut at world-space cells
256 texels wide, so a road or kerb loop becomes short pieces instead of one
track-sized ring. Objects whose island borders would need more padding than
their own area, such as high-poly tyre stacks built from thousands of loose
parts, are projected instead onto the world plane each face faces most: six
charts per cell. Each chart is turned to its tightest rectangle and all charts
are packed in rows with 8 px between them, at the finest scale that fits the
atlas. The bake uses the GPU when Cycles has a compute device enabled in
Preferences, otherwise the CPU; each atlas bakes as one temporary joined mesh,
because Cycles repeats its scene setup for every selected object.

Each atlas is an 8-bit PNG packed into the .blend: red is ambient occlusion
(5 m distance), green is sun visibility (1 = lit). Sun visibility is the ratio
of two direct-diffuse passes, with the sun's shadows on and off, lit only by the
preview sun under a black world; texels facing away from the sun are 0. The
bake gives the sun a 2° angular size, so shadows of high occluders such as tree
crowns get soft edges wider than a texel, and lets a ray pass through up to 128
cut-out surfaces, so leaf cards deep in a crown stay see-through.
See-through surfaces such as fences and painted lines bake as solid where they
are opaque: the bake measures each texel's opacity and divides it out of the
ambient occlusion, which Cycles darkens by opacity. Fully transparent texels,
which receive no light, are filled from the opaque texels around them, one ring
at a time, as lightmappers dilate invalid texels. They still cast see-through
shadows. Texels buried inside another solid part, where parts intersect such
as trim sunk into a wall, are refilled the same way: every texel darker than
0.1 ambient occlusion casts 16 hemisphere rays against the solid meshes around
it (foliage cards and decals excluded), and one whose rays mostly hit back faces
counts as buried. Chart margins are rebuilt from the refilled texels, so no
black spreads from buried texels onto the visible surface. The bake restores the
scene's world, render engine, transparency limit, and sun settings afterwards.

Props that Smart UV Project would shred into thousands of islands are unwrapped
by box projection, one chart per side. Faces pointing the same way are layered
front to back so that faces behind one another never share a texel.

Bake Shadows replaces the previous atlases but keeps existing `Lightmap` UV maps.
An atlas is unwrapped again only when its members change: an object added,
removed, or renamed; a mesh whose vertex count, face corners, or world surface
area changed (editing or scaling it); or a changed **Texel Size** or **Atlas
Size**. Moving the sun or changing **Bake Samples** reuses every unwrap. The
trash button beside Bake Shadows deletes the atlases, lightmap references, and
`Lightmap` UV maps.

**Preview Lightmaps** shows every mesh in the scene unlit white, darkened only by
its baked lightmap: ambient occlusion, and the sun shadow at 40 % brightness.
Meshes without a lightmap show plain white. It shows in Material Preview and
Rendered viewport shading. It swaps in preview materials through object-linked
material slots and keeps each mesh's own materials; turning it off restores the
slots and deletes the preview materials. Bake Shadows, Clear Lightmaps, Render
Preview, and export turn it off first.

Export writes the atlases to `lightmaps/shared_<n>.png` and
`lightmaps/layout_<layout_id>_<n>.png`, the grass maps to
`lightmaps/<scope>_grass.png`, each lightmapped mesh's atlas path as the
`vectorg_lightmap` glTF extra, and two manifest root fields:

```json
{
    "lightmaps": ["lightmaps/layout_gp_0.png", "lightmaps/shared_0.png"],
    "sun": {"direction": [0.42, 0.76, -0.49]}
}
```

`sun.direction` is the unit direction toward the baked sun in game coordinates;
the game lights the track from it so live car shadows match the bake. An
unbaked track exports neither field. Validation fails when a baked track no
longer matches its bake: a receiver, decal, or grass mesh without a current
lightmap, a lightmapped object that is no longer a receiver, or a moved or
removed preview sun. It cannot detect geometry edits; bake again after changing
receiver, caster, or grass meshes.

Validation commands from the repository root:

```text
python -m unittest discover -s tests -v
blender --background --factory-startup --python-exit-code 1 --python tests/test_surface_refresh.py -- --blender
blender --background --factory-startup --python-exit-code 1 --python tests/test_lightmaps.py -- --blender
```
