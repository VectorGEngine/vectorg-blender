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
  "engine": {
    "torqueFactor": 1.0,
    "idleRPM": 1000,
    "redlineRPM": 7000,
    "revLimit": 7900,
    "maxRPM": 8000,
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
      "maxDegreesOfRotation": 540.0,
      "antiRollBars": {
        "front": 15.0,
        "rear": 15.0
      },
      "absLevel": 5,
      "escLevel": 0,
      "tractionControlLevel": 5,
      "brakeBias": 0.6,
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

Increment **Package Version** intentionally whenever exported package contents
change. Importing an existing car manifest preserves its `packageVersion`.
Engine RPM values are required and must satisfy
`idleRPM < redlineRPM <= revLimit <= maxRPM`.
Each preset exports its final drive and individual ratios under `gearing`.
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

Wheel object selections, axes, radius, and steering behavior are shared by
every preset. Car presets contain steering limits, steering-wheel rotation,
anti-roll, driver-assist levels, brake bias, and per-axle tire, suspension,
braking and grip configuration. Each assist level is an
integer from zero through the car-wide `driverAssists.<assist>.maxLevel`, and
runtime strength is `level / maxLevel`. New exporter configurations default
every maximum level to 5. The current game uses the first preset. Tire type is
`soft`, `medium`, or `hard`, with `medium` as the default. Suspension offset is
a signed change in metres to the calculated suspension rest length. Positive
values move the wheel farther down from the mount; negative values move it
toward the mount. The mount position and maximum suspension travel remain
unchanged. Applying the same offset to every wheel raises or lowers the chassis.
Each preset wheel's `gripFactor` multiplies its pressure-derived grip;
`2.0` doubles grip and `0.5` halves it.

**Max Brake Force (kg)** is the equivalent braking force available at each
wheel. Vehicle manifest version 8 exports this value in newtons using
`kg * 9.81`; the engine converts that force to a timestep-scaled impulse.
Each axle's **Estimate Brake Force** button estimates a value that exceeds peak
tire grip by 15 percent at maximum speed on dry tarmac with ABS off. The estimate
uses a grip coefficient of 1 along with collider mass, wheel and center-of-mass
positions, maximum downforce, and brake bias. Existing numeric brake values are
not converted when opening older Blender files.

Caster is expressed in degrees. Positive caster tilts the top of the steering
and suspension axis toward the rear of the car; negative caster tilts it toward
the front. The game creates this pivot at runtime at the wheel spin center, so
the exported model does not need an additional caster object.

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

Wheel objects should be direct children of their suspension objects.

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
Transmission On
Transmission Off
On Low / High
Off Low / High
Limiter
Turbo
```

**Pitch Offset (cents)** adjusts all loaded and off-throttle engine samples for
the vehicle. It defaults to `0`; positive values raise pitch and negative values
lower it. The exporter writes it as `sounds.pitchOffset` next to the logical
sample slots.

Each sound has an enabled checkbox that defaults on. An enabled sound with an
assigned file exports that custom sample. An enabled sound without a file is
omitted from the vehicle manifest so the game uses its default sample. A
disabled sound is written as `null`, which explicitly disables the game default.

Assigned and enabled files are copied into `sounds/`. Disabled files are not
packaged.

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

## Lights

Assign the Headlights, Brake Lights, and Reverse Lights materials in the
exporter. Each material must be used by an exported mesh and have its emission
color or texture configured in Blender. The game keeps emission intensity at
`0` while inactive and sets it to `10` while active. Headlights toggle with `E`
on keyboard or `R1` on a gamepad.

The selected material names are exported as:

```json
{
  "lights": {
    "headlights": { "material": "headlight_emission" },
    "brakeLights": { "material": "brake_emission" },
    "reverseLights": { "material": "reverse_emission" }
  }
}
```

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

## Export

Use `Validate Car` first, then `Export Car Zip`.

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

The GLB export uses Blender's built-in glTF exporter with:

```text
export_format="GLB"
use_selection=False
export_apply=True
export_unused_images=False
```
