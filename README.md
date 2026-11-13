# VectorG Blender Exporters

This folder contains the car exporter and track exporter. Track exporter usage
is documented in [TRACK_EXPORTER.md](TRACK_EXPORTER.md).

## Car Exporter

VectorG Car Exporter creates vehicle packages for the VectorG driving simulator.
Install the `vectorg_car_exporter` folder as a Blender add-on, then open
`View3D > Sidebar > VectorG`.

The addon exports a zip with:

```text
<car_id>.glb
manifest.json
sounds/
```

The package matches the game loader convention:

```text
src/files/models/vehicles/<car_id>/<car_id>.glb
src/files/models/vehicles/<car_id>/manifest.json
src/files/models/vehicles/<car_id>/sounds/
```

Camera `fov` values in `manifest.json` are vertical angles in degrees. The
exporter derives them from the Blender camera and the scene render aspect ratio.

`manifest.json` identifies the exported model explicitly:

```json
{
  "version": 8,
  "id": "<car_id>",
  "packageVersion": "1",
  "model": "<car_id>.glb",
  "class": "A",
  "engine": {
    "torqueFactor": 1.0,
    "idleRPM": 1000,
    "redlineRPM": 7000,
    "revLimit": 7900,
    "maxRPM": 8000,
    "inertia": 0.2,
    "engineBraking": 0.2,
    "autoBlip": true
  },
  "steeringWheel": {
    "obj": "steering_wheel",
    "spinLocalAxis": [0, 1, 0]
  },
  "driverAssists": {
    "abs": { "maxLevel": 5 },
    "esc": { "maxLevel": 5 },
    "tractionControl": { "maxLevel": 5 }
  },
  "presets": [
    {
      "id": "default",
      "name": "Default",
      "maxSteeringAngle": 50.0,
      "roadWheelCurve": 0.5,
      "ackermann": 1.0,
      "maxDegreesOfRotation": 540.0,
      "antiRollBars": {
        "front": 15.0,
        "rear": 15.0
      },
      "absLevel": 5,
      "escLevel": 0,
      "tractionControlLevel": 5,
      "brakeBias": 0.6,
      "differential": {
        "frontAccelLock": 0.0,
        "frontDecelLock": 0.0,
        "rearAccelLock": 0.0,
        "rearDecelLock": 0.0,
        "centerBalance": 0.5,
        "centerLock": 0.0
      },
      "gearing": {
        "finalDriveRatio": 5.0,
        "gearRatios": {
          "-1": -3.57,
          "0": 0,
          "1": 4.08
        }
      },
      "wheels": {
        "front": {
          "l": {
            "tireType": "medium",
            "pressure": 2.0,
            "camber": -4.0,
            "caster": 6.0,
            "toe": -0.15,
            "suspensionOffset": 0.0,
            "bumpTravel": 0.1,
            "droopTravel": 0.1,
            "suspensionStiffness": 80.0,
            "dampingRelaxation": 2.6,
            "dampingCompression": 2.0,
            "maxBrakeForce": 1000.0,
            "gripFactor": 1.0
          }
        }
      }
    }
  ]
}
```

`class` is required and must be one of the vehicle class codes: `K` Kart,
`C` Street, `B` Sport, `A` GT, `R` Rally, or `F` Formula. The exporter's
**Class** menu mirrors the game's registry in `vectorg/vehicleClasses.js`;
add new classes to both. Manifest import rejects missing or unknown classes.

Change **Package Version** only when explicitly requested by the user; changes
to exported contents do not authorize a version bump. Importing an existing
car manifest preserves its `packageVersion`.
Engine RPM values are required and must satisfy
`idleRPM < redlineRPM <= revLimit <= maxRPM`.
Engine Braking Factor follows Engine Inertia and exports the required, finite,
nonnegative `engine.engineBraking` value (default `0.2`). It is unitless: `0.2`
applies 20% of peak engine torque at maximum RPM with zero throttle, decreasing
toward idle or as throttle increases.
Each preset exports its final drive and individual ratios under `gearing`.
The separate Differential section edits the selected car preset and exposes
acceleration and deceleration locks on driven axles, plus front torque share and
center lock for AWD. FWD exports zero rear locks, `centerBalance: 1` (all front)
and `centerLock: 0`; RWD exports zero front locks, `centerBalance: 0` (all rear)
and `centerLock: 0`. Values export under `differential` as numbers from 0 to 1.
Zero lock is open; full axle lock enforces equal wheel angular speeds, and full
center lock enforces equal front and rear mean wheel angular speeds (a rigid
shaft, as in Rally1/Rally2 cars). Center balance sets the torque split while the
center can slip; a fully locked center lets grip decide the split instead. The
handbrake releases the center lock to disconnect the rear drive.
Every manifest preset must contain all six valid fields. The game reads its
factory differential settings from these per-car presets; it has no global
fallback differential setup.
The Torque Curve section exports `engine.torqueFactor`, which scales drive and
engine-braking torque before tire-force limits are applied.
The game applies auto blip only when both its gameplay setting and the vehicle's
`engine.autoBlip` capability are enabled.

## Body Physics Helpers

Collider mass is entered and exported in kilograms.

Create the center of mass from **Body Physics > Add Center of Mass**. The addon
creates a sphere Empty at the 3D cursor, parents it to the car root, and exports
it as the `body.centerOfMass` node used by the game. Only its location is
editable.

Create aerodynamic load points with **Add Downforce Point**. Each point has its
own editable name and maximum downforce entered in kilograms, representing the
equivalent weight added at maximum speed. The addon converts kilograms to
newtons using `kg * 9.81` when writing the manifest. The name is shown in the
Body Physics panel and written to the manifest. The arrow is fixed to car local
`-Z`, matching game chassis local `-Y`; its rotation and scale are intentionally
locked. Downforce helpers are authoring-only and are not written to the GLB.
Their car-local positions are exported in metres:

```json
"downForcePoints": [
  {
    "name": "Front",
    "position": [0.0, 0.35, -1.2],
    "maxForce": 1800.0
  }
]
```

At runtime each point independently reaches `maxForce` at the vehicle's geared
maximum speed, following a capped square-law speed curve. Point placement
controls the resulting pitch moment; point rotation does not affect force
direction.

**Body Physics > Drag per Downforce** controls the extra drag generated by
downforce for this car, shared by all its points. It exports the required finite,
nonnegative `body.dragPerDownforce` number, and is restored on manifest import.
The default is `0.2`: 1000 N of actual downforce adds 200 N of drag opposing
longitudinal motion. Zero disables this extra drag; `body.airDrag` still applies.
Higher aero settings increase both tire loading and drag through the same speed
curve. This improves high-speed grip but reduces high-speed acceleration and
drag-limited top speed. Bundled F2021 uses `0.1`; the other bundled cars use `0.2`.

Set each shared wheel's **Width (m)** manually before export. This is the full tire
width along its rolling axle, centered on the Spin origin. The exporter writes
`spin.width`; the game requires a finite positive value and does not measure or
guess missing widths. Existing configurations show zero until a width is entered.
The engine sweeps a cylinder with this width and the authored radius through
suspension travel. This detects front and side tire contact; it is not a sweep
along the car's movement between simulation ticks.

Wheel object selections, axes, radius, width, and steering behavior are shared by
every preset. Car presets contain steering limits, steering-wheel rotation,
anti-roll, driver-assist levels, brake bias, and per-axle tire, suspension,
braking and grip configuration. Each assist level is an
integer from zero through the car-wide `driverAssists.<assist>.maxLevel`, and
runtime strength is `level / maxLevel`. New exporter configurations default
every maximum level to 5. The current game uses the first preset. Tire type is
`soft`, `medium`, `hard`, `wet`, or `snow`, with `medium` as the default.
Wet tires specialize in wet roads; Snow tires represent studless winter tires
for snow and ice. Grip tables are configured in the game.

**Bump Travel** and **Droop Travel** are required, positive distances in metres
measured from the modeled Joint position along the suspension length (the
Mount → Joint distance). The wheel compresses by at most Bump Travel before a
rigid bump stop and extends by at most Droop Travel to full droop, where an
airborne wheel hangs. Both limits are fixed to the chassis. The game preloads
each spring with its share of the car's weight, split by the center of mass,
so the parked car sits at its rest position whatever the stiffness; stiffness
only changes how firmly the suspension responds.

Suspension offset is a signed change in metres to that rest position. Positive
values move the wheel farther down from the mount; negative values move it
toward the mount. It must stay between `-bumpTravel` and `droopTravel`, so
lowering the car uses up bump travel. Applying the same offset to every wheel
raises or lowers the chassis.
Each preset wheel's `gripFactor` multiplies its pressure-derived grip;
`2.0` doubles grip and `0.5` halves it.

**Max Brake Force (kg)** is the equivalent braking force available at each
wheel. Vehicle manifest version 8 exports this value in newtons using
`kg * 9.81`; the engine converts that force to a timestep-scaled impulse.
Each axle's **Estimate Brake Force** button estimates a value that exceeds peak
tire grip by 15 percent at maximum speed on dry tarmac with ABS off. The estimate
uses a grip coefficient of 1 along with collider mass, wheel and center-of-mass
positions in Blender world space (Z up, -Y forward), maximum downforce, and brake
bias. Parent rotation and scale are included in those positions. Existing numeric brake values are
not converted when opening older Blender files.

Caster is expressed in degrees. Positive caster tilts the top of the steering
and suspension axis toward the rear of the car; negative caster tilts it toward
the front. The game creates this pivot at runtime at the wheel spin center, so
the exported model does not need an additional caster object.

Suspension travel, kingpin inclination, and tire camber can be authored independently.
The game uses the **Mount → Joint positions** to calculate travel direction and the
full distance between them as the rest length. The existing **Up Local Axis**
selection is shared: Joint's orientation makes it the kingpin axis, and Spin's
orientation makes it the tire's up axis. **Spin** defines the tire's
orientation, center, and rolling axle. Rotating Spin to make the tire upright
does not straighten the kingpin or suspension travel.

For example, Mount tilted inward 10°, Joint counter-rotated 5°, and Spin
counter-rotated another 5° produce 10° suspension travel (when Mount → Joint
follows Mount's axis), a 5° kingpin, and an upright tire. Zero preset alignment
offsets preserve that authored pose. Caster rotates the entire wheel assembly
around the tire center. Toe applies a neutral steering rotation about Joint,
moving an offset tire center with it; positive toe points both wheel fronts
inward and negative toe points them outward. Camber adjusts Spin at the tire center.
The game measures the resulting neutral wheel toe/camber relative to the authored
alignment; steering can subsequently change camber around the inclined kingpin.

The shared selection stays in the existing `spin.upLocalAxis` manifest field.
For example, F2021 uses local -Z (`[0, 0, -1]` in GLB, Blender -Y) for both
Joint and Spin. Different object rotations still give them independent inclinations.

Preset adjustments are edited once for the front axle and once for the rear
axle. Exported manifests still contain separate `l` and `r` wheel entries, with
the corresponding axle values copied into both entries.

## Scripts Path Installation

In Blender, open `Edit > Preferences > File Paths` and add this Scripts path:

```text
/Users/firatkiral/Repo/vectorg/vectorg-blender
```

Blender loads add-ons from the repository's `addons/` directory. This add-on is
located at:

```text
/Users/firatkiral/Repo/vectorg/vectorg-blender/addons/vectorg_car_exporter
```

Restart Blender, open `Edit > Preferences > Add-ons`, and enable:

```text
VectorG Car Exporter
```

## Zip Installation

To create an installable archive, run from the repository root:

```bash
cd addons && zip -r ../vectorg_car_exporter.zip vectorg_car_exporter
```

Then use Blender's `Install from Disk` action and select
`vectorg_car_exporter.zip`.

## Required Scene Objects

Default object names are based on `src/files/models/vehicles/byakko_gtr/manifest.json`:

```text
body
body_collider
centerOfMass
steering_wheel
dashboard_screen

suspension_fl
suspension_fr
suspension_rl
suspension_rr

wheel_fl
wheel_fr
wheel_rl
wheel_rr

chase_cam
cockpit_cam
hood_cam
roof_cam
```

`dashboard_screen` is optional. Assign it in the Dashboard section when the car
has an in-cockpit racing display.

Use the hierarchy **Mount → Joint → Spin**. Additional parent nodes are supported.
Use separate Joint and Spin objects to author the kingpin and wheel orientation independently.

### Suspension arm pivot

A wheel can optionally swing on a rigid arm, like a trailing arm or an off-road
axle link, instead of sliding straight along Mount → Joint. Assign an
**Arm Pivot** object fixed to the chassis, outside the Joint hierarchy. The Joint
then travels on a circle around the pivot, rotating about the axis
`cross(Mount − Pivot, Joint − Pivot)`; the pivot must not lie on the line through
Mount and Joint. Suspension lengths, Bump Travel and Droop Travel remain
Mount → Joint distances, so the spring still spans Mount to Joint while the arm
keeps the Pivot → Joint distance.

With **Tilt With Arm** enabled, the Joint and wheel rotate with the arm as it
swings, so camber or caster changes with travel; otherwise the wheel only moves.
The exporter rejects travel the arm cannot follow: every length between the bump
stop and full droop must be reachable, with at least 0.1 m of spring length change
per metre of wheel travel. The manifest records the pivot as:

```json
"pivot": { "obj": "Pivot_RL", "tilt": false }
```

## Direction Rules

The addon validates these conventions:

```text
car local -Y = forward
car local +X = left
left wheels are on car +X
right wheels are on car -X
wheel local +X aligns with car left/right axle
steering wheel local -Y faces car forward
steering wheel local +Z aligns with car up
```

Orientation failures are warnings because some source models may need artist-side correction or intentional overrides.

## Persistent Config

All editable values are stored in `Scene.car_exporter`, so values are saved inside the `.blend` file and restored when the UI is reopened.

## Audio

Audio uses fixed logical slots instead of free-form files:

```text
Idle
Off Low / Mid / High
On Low / Mid / High
Transmission Off
Transmission On
Limiter
Turbo
Backfire
Traction Control
Engine Start
Gear Grinding
Gear Change
Brake Squeal
```

The game decides which samples loop. Engine Start, Gear Grinding, Gear Change,
Turbo, and Backfire play once; every other slot loops. The exporter writes no `loop` flag, and
manifest import ignores one. The engine samples and Traction Control have an RPM
reference; every other slot has enabled and volume controls only.

Backfire plays exhaust pops on high-rev throttle lifts, loaded upshifts, and the
rev limiter. A custom Backfire sample must hold a weak pop within its first
0.24 s followed by a strong pop; the game plays either pop alone or both.
Traction Control loops while traction control cuts power. Its pitch and speed
follow the vehicle's normalized engine RPM around its RPM reference, so one
sample fits every rev range.
Gear Change plays each time the gear changes, including into neutral and reverse.

**Pitch Offset (cents)** adjusts all loaded and off-throttle engine samples for
the vehicle. It defaults to `0`; positive values raise pitch and negative values
lower it. The exporter writes it as `sounds.pitchOffset` next to the logical
sample slots.

Each sound selects a Blender Sound datablock from the current file and has an
enabled checkbox that defaults on. An enabled sound with an assigned datablock
exports that custom sample. An enabled sound without a datablock is
omitted from the vehicle manifest so the game uses its default sample. A
disabled sound is written as `null`, which explicitly disables the game default.

Assigned and enabled sounds are written into `sounds/` using their slot name
and original file extension. If the stored path has no extension, the exporter
uses the datablock name or recognizes WAV, Ogg, FLAC, AIFF, and ID3-tagged MP3
headers. Unrecognized audio requires its original extension in the datablock
name instead of exporting a file without an extension.
Packed sounds export directly from the data stored
in the `.blend`, even if their original file is missing. Unpacked sounds export
from the file referenced by their datablock, resolving linked-library paths.
Disabled sounds are not packaged. Manifest import loads custom samples into
Blender Sound datablocks and assigns them to their slots.

Idle, On Mid (`on_mid`), and Off Mid (`off_mid`) are optional looping samples.
Their datablock, enabled, RPM, and volume controls follow the same rules as Low and
High. The shared sound profile currently supplies no idle or mid recordings.

Engine bands peak at 0% (idle), 15% (low), 50% (mid), and 100% (high) of the
vehicle's idle-to-redline RPM range. The game crossfades between neighboring
available bands separately for throttle-on and throttle-off, then blends those
two sides by engine load. Idle is a single shared loop, without a volume boost
at partial throttle. A missing band is skipped; the nearest available band
covers RPM outside the available range. A side with no samples stays silent.
These band positions are shared game constants; each sample's RPM control
sets its pitch reference, not its blend position. Driving and replay use the
same band mixer.

For every engine loop, RPM is the actual engine speed represented by the
recording and must be positive. Playback uses the ratio of actual engine RPM
to that reference: matching RPM contributes zero detune, half RPM contributes
one octave down, and double RPM contributes one octave up. The game then adds
the configured Pitch Offset; redline RPM does not add a pitch shift. On High also receives
a 4.5 dB presence boost at 2600 Hz (Q 0.8); cockpit filtering still applies.

## Body Colors

Add selectable paint materials under **Body > Body Colors**. Each entry has a
display name and a Blender material. Entry `0` is the default; use the list
arrows to change the order. The default material must be assigned to geometry
inside the car hierarchy. The exporter embeds configured alternative materials
in the GLB even when they are not currently assigned to visible geometry.

Body colors are optional. When configured, their order is exported as:

```json
{
  "body": {
    "colors": [
      { "name": "White", "material": "Body_White" },
      { "name": "Red", "material": "Body_Red" }
    ]
  }
}
```

The game can use the exported names for color selection. Runtime color
selection is not configured by the Blender addon.

## Armature

Enable **Armature**, select the armature object, and press **Add Joint**.
Each entry maps an existing bone; the button does not create or reparent bones.

- **Bone:** searchable list of bones in the selected armature.
- **Base Attachment:** required dropdown using the existing car assignments.
- **Tip Attachment:** the same dropdown, plus **None** for follow-only mode.
- **Stretch:** shown when Tip is selected; disabled by default.

Attachment identifiers are `root`, `mount_fl`, `mount_fr`, `mount_rl`, `mount_rr`,
`joint_fl`, `joint_fr`, `joint_rl`, `joint_rr`, `spin_fl`, `spin_fr`, `spin_rl`,
and `spin_rr`. They resolve through the normal car's configured root and wheel
objects, so no duplicate assignments are needed. Joint follows suspension and
steering; Spin additionally inherits wheel rolling.

Author the bone in **Edit Mode**: its head is the base mounting point and its
tail is the tip mounting point. These points can be offset from the attachment
object origins. The game derives the base relationship from the bone's GLB rest
transform. Export saves the tail offset, including parent transforms, because
GLB bone nodes do not store Blender bone tails. It does not sample posed bone
positions or Blender constraints.

| Setup | Intended game behaviour |
| --- | --- |
| Base only | Follow base position and rotation, preserving the authored bone offset and orientation. |
| Base and Tip, Stretch off | Position the head at Base and aim toward Tip; preserve rest length. |
| Base and Tip, Stretch on | Also scale lengthwise to reach Tip; preserve thickness. |

With Stretch off, the tail need not reach the moving tip attachment. Removing
Tip exports follow-only mode even if Stretch was previously checked.

Keep the rig and normal wheel objects in independent branches below Car Root:

```text
CarRoot
|-- Body
|-- Mount_FL
|   `-- Joint_FL
|       `-- Spin_FL
|           `-- Wheel_FL
|-- Other wheel hierarchies
`-- SuspensionRig (Armature)
    `-- SuspensionGeometry (Mesh with Armature modifier)
```

For example, map `hub_fl` to Base `joint_fl` with no Tip. Map `rod_fl` to Base
`root`, Tip `joint_fl`, and enable Stretch. Place its head at the chassis mounting
point and its tail at the wheel-side mounting point. Weight the entire rod to
`rod_fl` for a rigid cross-section with lengthwise stretching.

Each driven bone must be unique, have non-zero rest length, and have
**Connected** disabled so it can translate. Authored bone parenting is preserved.
The attachment objects
must exist in the exported car. Wheel/armature parenting must not create feedback.

The enabled section adds this optional field to manifest version 8:

```json
{
  "armature": {
    "obj": "SuspensionRig",
    "joints": [
      {
        "bone": "hub_fl",
        "base": "joint_fl",
        "tip": null,
        "stretch": false,
        "tipOffset": null
      },
      {
        "bone": "rod_fl",
        "base": "root",
        "tip": "joint_fl",
        "stretch": true,
        "tipOffset": [0.05, 0.02, 0]
      }
    ]
  }
}
```

The example offsets are illustrative; export computes them from the scene.
`tipOffset` is the bone tail in Tip's local GLB coordinates. The base offset is
not exported: the game caches `inverse(baseRestWorld) * boneRestWorld` before
wheel alignment, then follows `baseCurrentWorld * cachedOffset`. Position conversion is
`[Blender X, Blender Z, -Blender Y]`, matching existing position exports.

For game integration, cache bone and attachment rest transforms from the GLB
before applying wheel animation or alignment. Use the bone origin and transformed
tip offset to recover the rest segment, its length, and its direction in the
bone's local frame. No additional axis selector or manifest axis field is needed.
Preserve authored roll, convert driven poses into their bone parent's local
space, and process driven parents before driven children.

Unchecking **Armature** omits the field while preserving selections and exported
geometry. Importing an enabled section replaces the mapping list. Importing no
section disables following and preserves selections. Import restores mapping
selections only; re-export derives offsets from the current Blender rest geometry.
The old `armature.wheels` contract is replaced by `armature.joints`; recreate old
mappings with **Add Joint**. No automatic migration or version bump is performed.

With Armature enabled, GLB export explicitly preserves skins, all bones, the
armature object and hierarchy, and uses the armature rest pose. Animation clips
are not exported in this mode: the intended game behavior is to drive the bones
from the attachment roles while preserving their authored relative offsets. Blender
constraints and drivers do not become live game constraints.

The game applies following, aiming and stretching after updating wheel objects,
including during replay and ghosts. The export setup targets Blender 4.5.

## LODs

Name each mesh `<part>_LOD<n>` to make it level `n` of that part, for example
`Body_LOD0`, `Body_LOD1` and `Body_LOD2`. A mesh without the suffix shows at every
level. Keep each part's LOD meshes under the node that moves it, so they follow
the wheel's Mount, Joint and Spin, the steering wheel or a door:

```text
Car Root
|-- Body_LOD0, Body_LOD1, ...
`-- Mount_FL
    `-- Joint_FL
        |-- Caliper_FL_LOD0, Caliper_FL_LOD1, ...
        `-- Spin_FL
            |-- Wheel_FL_LOD0, Wheel_FL_LOD1, ...
            `-- Rotor_FL_LOD0, Rotor_FL_LOD1, ...
```

Check **LODs** in the panel header to export levels. The section lists one row
per level found under Car Root:

- The arrow lists the level's meshes and the node each sits under.
- The eye shows or hides the level in the viewport only; Ctrl+click shows that
  level alone. Hidden LOD meshes still export.
- The checkbox exports the level; an unticked level is skipped and its meshes stay
  in the `.blend`. LOD0 always exports.
- **Show Above Screen Height (%)**: the level shows while the car's bounding
  sphere spans at least this share of the screen height. Each level needs a value
  smaller than the level before; below the last exported level's value the car is
  not drawn.

Each level shows only its own meshes: a part without a mesh at a level is not
drawn there, so a coarse level may be a single mesh for the whole car. With
**LODs** unchecked only LOD0 and meshes without the suffix export. **Merge Meshes** never joins meshes of different levels.

The manifest lists the exported levels, finest first, with their screen height as
a fraction, the ghost default level's position among them, and the level positions
every exported LOD node shows at, by the name it exports under. It is `null` with
LODs unchecked:

```json
"lods": {
  "levels": [
    { "level": 0, "screenHeight": 0.6 },
    { "level": 1, "screenHeight": 0.25 },
    { "level": 2, "screenHeight": 0.08 }
  ],
  "ghost": 1,
  "objects": {
    "MERGED_Body_LOD1": [1, 1],
    "Wheel_FL_LOD1": [1, 2]
  }
}
```

The player car and watched replays show LOD0 only.

## Ghost

Ghosts and multiplayer opponents are the car itself. **Ghost Default LOD** is the
finest level they show; coarser levels still take over as they get smaller on
screen. Finer levels are left out of a ghost when it loads.

**Shadow** and **Shadow Plane** each have a refresh button that builds the object
under Car Root from the meshes shown at the ghost default level, replaces what it
built before and hides it in the viewport:

- **Shadow** (`GHOST_SHADOW`) joins those meshes, without the wheels, into one
  mesh without materials at 10% of their triangles. The ghost casts its live
  shadow from it at its default level, and it never renders.
- **Shadow Plane** (`GHOST_SHADOW_PLANE`) is a flat, single-sided quad on the
  floor, at the lowest point (where the tyres touch the ground), textured with the
  top-down silhouette, wheels included: a packed PNG, white, with the blurred
  silhouette as alpha. The game takes only the alpha and darkens the ground under
  it by each track's own shadow tint, so edit the PNG's alpha to change the shape.
  Its dark blue-grey colour shows in Blender only. At coarser levels it lies on the
  track under the ghost in place of the shadow, aligned to the road and shifted
  away from the sun; Today's Track shows it at every level.

Both can also point at hand-made childless meshes under Car Root. Like the shadow
mesh and AO plane, they may sit in a group under Car Root, such as `shadows`, but
not under a wheel or other node the game moves. With more than one exported level both are required. A ghost casts its own
shadow mesh in place of the car's **Custom Shadow** mesh and drops the AO plane;
past 100 m from the player nothing of a ghost casts.

```json
"ghost": {
  "shadow": { "obj": "GHOST_SHADOW" },
  "shadowPlane": { "obj": "GHOST_SHADOW_PLANE" }
}
```

`ghost` is `null` when both are empty. Run **Validate Car** after import to check
the selections against the scene. Preserve **Package Version** unless the user
explicitly requests a change.

## Custom Shadow

Open **Custom Shadow**, check the box next to the title, and pick a **Shadow
Mesh**, or click the refresh button at the end of its field to generate one. In
the game the shadow mesh alone
casts the car's live shadow: it is drawn only into the sun's shadow map, never
on screen or in the mirror, and every other part stops casting while still
receiving shadows. Shadow cost is per draw call, so one shadow mesh replaces
the car's many parts in the shadow pass. With the section off, every part casts
as before, and an assigned shadow mesh is left out of the export.

The shadow mesh must be a mesh with faces under Car Root, directly or in a group
outside wheels and other moving nodes, without children, and not used as a wheel, camera, collider, ghost or other reference.

Every shadow refresh button (shadow mesh, AO plane, ghost **Shadow** and **Shadow
Plane**) builds its object in the group the object it replaces was in, or directly
under Car Root.

The refresh button joins the evaluated geometry of every mesh under Car
Root into one object named `SHADOW_MESH`, without materials, UV maps or color
layers, shown as wireframe. It uses the meshes shown at LOD0 and leaves out
the wheel mount hierarchies, the ghost shadows, colliders and helpers. Generating again
replaces the previously generated mesh; a picked mesh is never deleted. The
result is an ordinary object: decimate or edit it to lower its cost further.

The manifest records it as `"shadow": { "obj": "SHADOW_MESH" }`, or `null`.
A ghost casts from its own ghost **Shadow** mesh when it has one.

### AO Plane

Below **Shadow Mesh**, the refresh button at the end of **AO Plane** builds the
car's ambient shadow, `AMBIENT_SHADOW`, inside the `.blend` in a few seconds,
without baking, and replaces what an earlier refresh made.

- It copies every mesh shown at LOD0 under Car Root, wheels included, leaving
  out the ghost shadows, colliders, helpers and the shadow mesh, at 2% of their triangles, and
  draws their outline seen from straight above into a 128 px image, blurred by
  0.5 m on each side with a 0.6 m clear border.
- `AMBIENT_SHADOW` is a flat, single-sided quad under Car Root, at
  the car's lowest point (where the tyres touch the ground), hidden in the
  viewport. Its packed PNG is black with the blurred outline as alpha, and its
  material shows exactly that, so Blender and the game look the same.

In the game the plane stays on the ground straight under the car, turned with
it, on every surface the car drives over. Black at the outline's opacity
darkens the ground by the sky light the car blocks, so it needs no colour per
track. It fades out as the car rises above its resting height and is gone 1 m
up. The player car and replays show it; ghosts and the garage preview do not.
It never casts or receives shadows.

The plane can also be a hand-made mesh with the same layout: under Car Root,
directly or in a group outside wheels and other moving nodes, without children, not used as any other reference, with a material
whose image texture holds the outline in alpha. Its height under Car Root is
read as the ground at rest. With **Custom Shadow** off, an assigned plane is
left out of the export like the shadow mesh.

The manifest records it as `"ambientShadow": { "obj": "AMBIENT_SHADOW" }`, or
`null` when **AO Plane** is empty or **Custom Shadow** is off.

## Lights

Lights are optional. Leave all four lamp materials empty to export
`"lights": null`.

Each light function has its own lamp material. The game turns the material's
emission up and down by the function, so every mesh using it lights together,
every LOD level included:

| Material | Glows at its Full Emission |
| --- | --- |
| **Headlights** | with the light switch on |
| **Tail Lights** | with the light switch on (its **Tail Lights** emission) |
| **Brake Lights** | while braking |
| **Reverse Lights** | in reverse gear |

The Tail Lights material has a second emission, **Brake Lights**, added to its
glow while braking, for tail lamps that double as brake lamps. It starts at 0,
which leaves the tail lamps to the light switch. With the lights on while
braking the tail lamps glow at the sum of both.

Lamps on any other material (indicators, fog lights) never light. A function
without a material is allowed; Validate Car warns about it.

1. Assign the four materials to the lamp meshes, on every LOD level, and pick
   them in the Lights panel. Each material needs an **Emission Strength** above
   0 and its emission colour or texture; the game multiplies it by **Full
   Emission**.
2. Click **+** beside **Front Lamp** to add an arrow empty at the 3D cursor
   beneath Car Root, then move it onto one headlamp. Its arrow (local +Z) is
   the way the headlight shines; it starts straight ahead, so tilt it slightly
   down. Click **+** beside **Rear Lamp** and move it onto one tail lamp. The
   game mirrors both markers across the car's centre line; any empty beneath
   Car Root can be picked instead.
3. Set each function's **Full Emission**, and the tail lamps' **Brake Lights**
   emission if they also light while braking.

The markers are authoring helpers and are left out of the GLB. The game lights
the road with one shaped spotlight on the car's centre line at the front
marker's height, 0.9 m behind it, aimed straight ahead at the marker's pitch;
its strength follows how dark the track is. On Today's Track each lamp also
gets a glow halo at night, at the mirrored markers, on every car including
ghosts.

```json
{
  "lights": {
    "materials": { "headlights": "Lamp_Head", "tail": "Lamp_Tail", "brake": "Lamp_Brake", "reverse": "Lamp_Reverse" },
    "emission": { "headlights": 10, "tail": 2, "tailBrake": 0, "brake": 4, "reverse": 4 },
    "front": { "position": [0.645, 0.552, 1.7], "direction": [0, -0.06, 0.998] },
    "rear": { "position": [0.52, 0.743, -1.85] }
  }
}
```

Positions are car-local game coordinates `(Blender X, Blender Z, -Blender Y)`;
`front.direction` uses the same basis, is normalized and must aim forward.
Manifest import restores the materials, the full emissions and both markers.
Preserve `packageVersion` when re-exporting unless the user explicitly requests
a change.

## Dashboard Screen

Create and position a dedicated plane in the dashboard, parent it inside the
car root, and assign it as **Dashboard > Screen**. The plane must use local X
for width and local Z for length, with local Y as its normal. It must have one
dedicated material and an active UV map covering the complete 0-1 texture area.
Do not share its material with another mesh. Any positive aspect ratio is
accepted.

Only the selected object is exported:

```json
{
  "dashboard": {
    "screen": {
      "obj": "dashboard_screen"
    }
  }
}
```

The runtime measures the plane's physical X:Z ratio, including hierarchy scale.
Canvas height is always 1024 pixels and width is calculated from that ratio.
For example, a 2:1 plane creates a 2048 x 1024 canvas.

The display shows five mirrored shift-light pairs across ten LEDs at the top.
They light from the outside edges toward the center as RPM rises: green, green,
amber, amber, then red. At the rev limiter all ten LEDs flash together. Gear is
centered, lap timing is at the bottom left, and speed is at the bottom right.

## Gauge Needles

**Dashboard > RPM Needle** and **Dashboard > Speed Needle** each take an action
that sweeps a needle from zero at the action's first frame to the car's maximum
at its last frame: **Max RPM** for the tachometer, and the top-gear speed at
Max RPM for the speedometer. Both are optional; empty pickers export nothing.

The action must be the active action, or the only unmuted strip on an NLA track,
of an object under Car Root. The objects it moves always export and anchor mesh
merging like other moving nodes. Only the picked actions export, each as one
glTF animation named after the action and sampled every frame; every other
action, the armature's included, stays out of the GLB.

```json
{
  "dashboard": {
    "rpmNeedle": { "animation": "needle_rpm" },
    "speedNeedle": { "animation": "needle_speed" }
  }
}
```

The player's car and replays smooth RPM and speed, then pose each needle at the
reading's share of that maximum, held at both ends. The speedometer's top mark
follows the selected setup's gearing. Ghosts and multiplayer cars leave the
needles still.

## Export

Use `Validate Car` first, then `Export Car Zip`.

Objects under Car Root that are hidden in the viewport (the eye, the monitor or
a hidden collection) are left out of the GLB, so the game never draws them;
their visible children export where they are. **Validate Car** lists them as a
warning. Nodes the manifest names or the game drives (Car Root, wheels,
cameras, colliders, the shadow mesh, the AO plane and the like), LOD meshes,
whose eyes hide them in the viewport only, and the objects the refresh buttons
generate and hide always export. Hidden meshes without a LOD suffix are also
left out of mesh merging, the shadow mesh, the AO plane and the ghost shadows.

When the configured car root or any object below it has non-unit scale, export
asks whether to apply hierarchy scales. Leaving **Apply scales** checked
updates those Blender objects to `(1, 1, 1)` while preserving their
transformed geometry. Unchecking it exports without modifying their scales,
and **Cancel** stops the export.

**Maximum Texture Size** limits the longest side of every exported material
texture without modifying the source image. The default car limit is `2048`.
When **Compress Opaque Color Textures** is enabled, textures used only by
Principled BSDF base-color or emission inputs are exported as JPEG at the
selected quality. Textures used for alpha, normals, metallic, roughness, masks,
or ambiguous node graphs are not converted to JPEG. Textures are embedded in
the GLB.

**Merge Same-Material Meshes** (on by default) joins meshes during export
because the game pays per draw call, not per triangle. Meshes join when their
faces use the same materials, they have the same UV layer count and color
layers and the same LOD level range, under the same nearest anchor: the car root or a node the manifest
names or the game moves (wheel mount, joint, pivot and spin, steering wheel,
cameras, dashboard screen, ghost shadow and shadow plane, shadow mesh). Each
wheel therefore merges on its own spin node and moving parts keep moving.
Colliders, skinned meshes, referenced objects, meshes with children and the
body color carrier are never merged. Modifiers are applied and transforms baked
into the joined node. The Blender scene is not changed: the joined objects
exist only during the export, and the Blender console prints the merge count.

The GLB export uses Blender's built-in glTF exporter with:

```text
export_format="GLB"
use_selection=False
export_apply=True
export_unused_images=False
```
